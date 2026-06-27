"""账号池：多账号轮换 + 冷却管理 + 永久禁用。

CLIProxyAPI 的核心调度思路在这里，并吸收了 cockpit-tools 的「永久禁用」机制：
  - 每个 backend 维护一个账号列表
  - Round-robin 选号，跳过冷却中 / 永久禁用的账号
  - 请求失败时按错误类型分三档处理：
    * 认证失效（invalid_grant / 401 / token 撤销）→ 永久禁用并落盘（重启不再死磕）
    * 配额/频率限制 → 长冷却（默认 60s，配额会自己恢复）
    * 瞬时错误（超时/5xx）→ 短冷却（默认 15s）
  - 半开探测：禁用账号在 RECOVERY_PROBE_AFTER 秒后允许放行一次试探，
    成功（含刷新成功）则自动解禁，对齐 cockpit 的 invalid_grant 自动恢复。
  - 全部账号不可用时抛 RuntimeError（调用方可重试或报错）
"""
from __future__ import annotations

import threading
import time
from typing import Iterator, Optional

from .account import Account, load_accounts

# 冷却时长（秒）
COOLDOWN_QUOTA = int(60)     # 配额耗尽 / 限速
COOLDOWN_TRANSIENT = int(15) # 瞬时错误（超时、5xx）

# 永久禁用账号多久后允许半开探测一次（秒）。token 可能被外部重新登录修复，给个复活机会。
RECOVERY_PROBE_AFTER = int(600)

# 判断是否配额/限速错误的关键词
_QUOTA_KEYWORDS = frozenset(
    ["quota", "rate limit", "429", "resource_exhausted", "too many requests", "exceeded"]
)
_TRANSIENT_KEYWORDS = frozenset(
    ["timeout", "500", "502", "503", "504", "connection", "unavailable", "deadline"]
)
# 认证彻底失效 → 永久禁用（光重试别的时间没用，得换 token / 重新登录）
_AUTH_KEYWORDS = frozenset(
    ["invalid_grant", "invalid_token", "unauthorized", "401",
     "token expired", "token has expired", "revoked", "no refresh_token"]
)


def _is_auth_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in _AUTH_KEYWORDS)


def _is_quota_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in _QUOTA_KEYWORDS)


def _is_transient_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in _TRANSIENT_KEYWORDS)


class AccountPool:
    """线程安全的账号池，支持多账号轮换与冷却。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # backend → list[Account]（按加载顺序，round-robin 用 index）
        self._accounts: dict[str, list[Account]] = {}
        self._index: dict[str, int] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            for acc in load_accounts():
                self._accounts.setdefault(acc.backend, []).append(acc)
                self._index.setdefault(acc.backend, 0)
            self._loaded = True

    def reload(self) -> None:
        """重新从磁盘加载账号（添加账号后调用）。"""
        with self._lock:
            self._accounts.clear()
            self._index.clear()
            self._loaded = False
        self._ensure_loaded()

    def accounts(self, backend: Optional[str] = None) -> list[Account]:
        """返回指定 backend（或全部）的账号列表。"""
        self._ensure_loaded()
        if backend:
            return list(self._accounts.get(backend, []))
        return [acc for accs in self._accounts.values() for acc in accs]

    def pick(self, backend: str) -> Optional[Account]:
        """选出一个可用账号：主备号（priority）+ 组内加权轮换（weight）。

        - 先在可用账号里取 priority 最小的「主号组」，组内按 weight 加权轮换；
          主号组全部不可用（冷却/禁用）时自然溢出到下一 priority 组（备号）。
        - 都不可用时，对「禁用够久」的账号做一次半开探测放行，让 token 被外部
          修复 / 刷新成功的账号自动复活。全无可放行账号返回 None。
        """
        self._ensure_loaded()
        with self._lock:
            accs = self._accounts.get(backend, [])
            if not accs:
                return None
            available = [a for a in accs if a.is_available]
            if available:
                return self._weighted_pick(backend, available)
            # 半开探测——禁用超过 RECOVERY_PROBE_AFTER 的账号放行一次试探
            now = time.time()
            n = len(accs)
            start = self._index.get(backend, 0)
            for offset in range(n):
                idx = (start + offset) % n
                acc = accs[idx]
                if (
                    acc.is_disabled
                    and not acc.is_cooling
                    and acc.disabled_at
                    and now - acc.disabled_at >= RECOVERY_PROBE_AFTER
                ):
                    self._index[backend] = (idx + 1) % n
                    return acc
        return None  # 全部不可用

    def _weighted_pick(self, backend: str, available: list[Account]) -> Account:
        """在可用账号中按 priority 分组 + 组内 weight 加权轮换选一个。须在持锁下调用。"""
        min_priority = min(a.priority for a in available)
        group = [a for a in available if a.priority == min_priority]  # 保持加载顺序
        total_weight = sum(max(1, a.weight) for a in group)
        cursor = self._index.get(backend, 0)
        self._index[backend] = cursor + 1
        slot = cursor % total_weight
        for acc in group:
            w = max(1, acc.weight)
            if slot < w:
                return acc
            slot -= w
        return group[-1]  # 兜底（理论不可达）

    def mark_success(self, account: Account) -> None:
        account.reset()
        # 半开探测成功 / 刷新后恢复：自动解除 invalid_grant 类禁用并落盘
        if account.is_invalid_grant_disabled:
            account.clear_disabled()
            account.persist()

    def mark_failed(self, account: Account, exc: Optional[BaseException] = None) -> None:
        """按错误类型决定：永久禁用（落盘）还是临时冷却。"""
        if exc is not None and _is_auth_error(exc):
            # token 彻底失效：永久禁用并落盘，重启不再死磕
            reason = "invalid_grant" if "invalid_grant" in str(exc).lower() else "auth_error"
            account.disable(f"{reason}: {str(exc)[:200]}")
            account.persist()
        elif exc is not None and _is_quota_error(exc):
            account.cool_down(COOLDOWN_QUOTA)
        else:
            account.cool_down(COOLDOWN_TRANSIENT)

    def status(self) -> dict:
        """返回所有 backend 的账号状态，供管理 API 展示。"""
        self._ensure_loaded()
        result: dict[str, list[dict]] = {}
        for backend, accs in self._accounts.items():
            result[backend] = [a.to_dict() for a in accs]
        return result

    def find(self, backend: str, account_id: str) -> Optional[Account]:
        """按 backend + id 定位账号（供管理操作）。"""
        self._ensure_loaded()
        for acc in self._accounts.get(backend, []):
            if acc.id == account_id:
                return acc
        return None

    def set_enabled(self, backend: str, account_id: str, enabled: bool) -> Optional[Account]:
        """手动启用 / 禁用某账号并落盘。返回被操作的账号，找不到返回 None。

        - 启用：清除禁用态 + 重置冷却（等价于人工确认该号已修复）。
        - 禁用：标记人工禁用原因（与 invalid_grant 自动禁用区分）。
        """
        acc = self.find(backend, account_id)
        if acc is None:
            return None
        if enabled:
            acc.clear_disabled()
            acc.reset()
        else:
            acc.disable("manual: 由管理面板禁用")
        acc.persist()
        return acc

    def reset_account(self, backend: str, account_id: str) -> Optional[Account]:
        """清除某账号的冷却 / 错误计数（不动禁用态）。找不到返回 None。"""
        acc = self.find(backend, account_id)
        if acc is None:
            return None
        acc.reset()
        return acc


# 全局单例（server.py 和 runner.py 共用）
_pool: Optional[AccountPool] = None
_pool_lock = threading.Lock()


def get_pool() -> AccountPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = AccountPool()
    return _pool
