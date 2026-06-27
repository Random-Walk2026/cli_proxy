"""账号模型与认证文件管理。

认证文件存放在 AUTH_DIR（默认 ~/.cli_proxy_api/）下，格式与 CLIProxyAPI 兼容：

    claude_work.json        → Claude 账号，文件名随意（backend 字段决定类型）
    codex_personal.json     → Codex 账号
    copilot_main.json       → Copilot 账号

每个文件的 JSON 结构：

    {
        "type": "claude",                    # backend 名称
        "email": "user@example.com",         # 账号标识（可选，仅用于显示）
        "token": "sk-ant-xxx...",            # 访问令牌 / OAuth token
        "refresh_token": "...",              # 可选，自动刷新用
        "enabled": true                      # false 则跳过此账号
    }

也可直接在 .env 里配置单账号（无需文件）：

    CLAUDE_CODE_OAUTH_TOKEN=sk-ant-xxx...   # cli_proxy 会自动读取
    COPILOT_GITHUB_TOKEN=ghp_xxx...
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import load_project_env

# token 提前刷新余量（秒）：expiry 距现在不足这个值就当作需要刷新。
# 对齐 cockpit-tools 的 ensure_fresh_token（300s 余量），比只在过期瞬间刷新更稳。
REFRESH_SKEW_SECONDS = 300

load_project_env()


def _auth_dir_from_env() -> Path:
    configured = os.environ.get("CLI_PROXY_AUTH_DIR", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".cli_proxy_api"


AUTH_DIR = _auth_dir_from_env()

# token 型后端：CLI 直接认一个环境变量里的 token，注入不同 token 即切换账号。
# （codex / antigravity 不在此列——它们没有「一个 token 变量搞定」的入口，靠 _HOME_ENV 目录隔离。）
_TOKEN_ENV: dict[str, str] = {
    "claude": "CLAUDE_CODE_OAUTH_TOKEN",
    "grok": "GROK_API_KEY",
    "copilot": "COPILOT_GITHUB_TOKEN",
}

# 目录型后端：CLI 把登录态存在一个目录里，给每个账号一份独立 profile 目录，
# 调用时用这个环境变量把 CLI 指过去，实现 subprocess 级账号隔离（每个子进程独立 env，线程安全）。
#   codex       → CODEX_HOME       （官方支持，默认 ~/.codex）
#   claude      → CLAUDE_CONFIG_DIR（可选；claude 多用 token 注入更简单）
#   antigravity → HOME             （agy 登录态在 ~/.gemini/，靠 HOME 重定向；前提是 token 文件型而非钥匙串）
_HOME_ENV: dict[str, str] = {
    "codex": "CODEX_HOME",
    "claude": "CLAUDE_CONFIG_DIR",
    "antigravity": "HOME",
}

# .env 里的兜底 token（无账号文件时使用）——只有 token 型后端能从 env 兜底。
_ENV_FALLBACK: dict[str, str] = {
    "claude": "CLAUDE_CODE_OAUTH_TOKEN",
    "grok": "GROK_API_KEY",
    "copilot": "COPILOT_GITHUB_TOKEN",
}


@dataclass
class Account:
    """单个订阅账号。"""
    backend: str
    id: str                          # 唯一标识，通常是 email 或文件名 stem
    token: str = ""                  # 访问令牌（token 型后端用）
    refresh_token: str = ""
    home: str = ""                   # 该账号独立的登录态目录（目录型后端用，注入 CODEX_HOME / HOME 等）
    extra_env: dict = field(default_factory=dict)  # 账号 JSON 里 "env": {...} 的任意补充环境变量
    enabled: bool = True

    # ── 路由权重（主备号）──────────────────────────────────────────────────
    # priority：数字越小越优先；只有当某个 priority 组内全部不可用时才溢出到下一组（备号）。
    # weight：同一 priority 组内的加权轮换份额（越大命中越多），最小按 1 计。
    priority: int = 0
    weight: int = 1

    # ── token 生命周期（持久化）─────────────────────────────────────────────
    # 直连 HTTP 后端（antigravity_http 等）把 access_token 的过期时间存进来，
    # 调用前用 needs_refresh() 提前刷新；CLI 型后端（claude/codex…）token 由 CLI 自管，expiry 留 0。
    expiry: float = 0.0              # access_token 过期的 unix 时间戳（0 = 未知/不追踪）

    # ── 永久禁用（持久化）──────────────────────────────────────────────────
    # 区别于「冷却」（临时、内存态）：disabled_reason 是 token 彻底失效（如 invalid_grant）这类
    # 需要人工/刷新才能恢复的问题，落盘后重启不再死磕；下次刷新成功自动解除（见 clear_disabled）。
    disabled_reason: str = ""        # 非空即视为永久禁用
    disabled_at: float = 0.0         # 禁用发生的 unix 时间戳

    source_path: str = ""            # 该账号来源的 JSON 文件路径（用于刷新/禁用状态写回；env 兜底账号为空）

    # ── 额度快照（持久化，懒刷新）─────────────────────────────────────────────
    # 由 quota.py 通过各 provider 的 usage 端点抓取并归一化后写入；状态面板直接读这里的缓存，
    # 不在每次轮询时实时拉取（usage 端点昂贵且限流）。手动「刷新额度」时才更新。
    quota: Optional[dict] = None     # 归一化额度：{"five_hour":{...}, "weekly":{...}, "plan_type":...}
    quota_error: str = ""            # 上次刷新失败原因（如 refresh_token_invalidated）
    quota_updated_at: float = 0.0    # 额度快照的 unix 时间戳

    # 冷却状态（运行时，不序列化）
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _cooling_until: float = field(default=0.0, init=False, repr=False)
    _error_count: int = field(default=0, init=False, repr=False)

    @property
    def is_cooling(self) -> bool:
        return time.monotonic() < self._cooling_until

    @property
    def is_disabled(self) -> bool:
        """永久禁用：显式 enabled=False，或带有 disabled_reason。"""
        return not self.enabled or bool(self.disabled_reason)

    @property
    def is_available(self) -> bool:
        return self.enabled and not self.is_disabled and not self.is_cooling

    @property
    def is_invalid_grant_disabled(self) -> bool:
        """因 token 失效（invalid_grant）被禁用——刷新成功后可自动解除。"""
        return bool(self.disabled_reason) and self.disabled_reason.startswith("invalid_grant")

    @property
    def error_count(self) -> int:
        return self._error_count

    def cool_down(self, seconds: float) -> None:
        with self._lock:
            self._cooling_until = time.monotonic() + seconds
            self._error_count += 1

    def reset(self) -> None:
        with self._lock:
            self._cooling_until = 0.0
            self._error_count = 0

    def needs_refresh(self, skew_seconds: int = REFRESH_SKEW_SECONDS) -> bool:
        """access_token 是否需要提前刷新（仅对追踪 expiry 的直连后端有意义）。"""
        if not self.expiry:
            return False
        return time.time() + skew_seconds >= self.expiry

    def disable(self, reason: str) -> None:
        """永久禁用（落盘语义）：标记原因与时间，并把 enabled 置 False。"""
        with self._lock:
            self.enabled = False
            self.disabled_reason = reason
            self.disabled_at = time.time()

    def clear_disabled(self) -> None:
        """解除永久禁用（三个字段一起重置）。"""
        with self._lock:
            self.enabled = True
            self.disabled_reason = ""
            self.disabled_at = 0.0

    def env_override(self) -> dict[str, str]:
        """调 CLI 时注入的环境变量：登录态目录重定向 + token + 额外 env。

        - 目录型后端（codex/agy）：注入 CODEX_HOME / HOME 指向该账号独立 profile 目录，
          每个子进程拿到不同目录即切换账号；env 随 subprocess 传入，不改全局，线程安全。
        - token 型后端（claude/copilot/grok）：注入对应 token 环境变量。
        """
        env: dict[str, str] = {}
        home_key = _HOME_ENV.get(self.backend)
        if home_key and self.home:
            env[home_key] = self.home
        token_key = _TOKEN_ENV.get(self.backend)
        if token_key and self.token:
            env[token_key] = self.token
        if self.extra_env:
            env.update(self.extra_env)
        return env

    @property
    def cooling_seconds(self) -> int:
        """距冷却结束还剩多少秒（非冷却中为 0）。供 UI 展示倒计时。"""
        if not self.is_cooling:
            return 0
        return max(0, int(self._cooling_until - time.monotonic()))

    @property
    def status(self) -> str:
        """聚合状态标签，供 UI 直接着色：available / cooling / disabled。"""
        if self.is_disabled:
            return "disabled"
        if self.is_cooling:
            return "cooling"
        return "available"

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "id": self.id,
            "token": self.token[:8] + "…" if self.token else "",  # 脱敏
            "home": self.home,
            "enabled": self.enabled,
            "available": self.is_available,
            "status": self.status,
            "cooling": self.is_cooling,
            "cooling_seconds": self.cooling_seconds,
            "disabled": self.is_disabled,
            "disabled_reason": self.disabled_reason,
            "expiry": self.expiry,
            "priority": self.priority,
            "weight": self.weight,
            "error_count": self._error_count,
            "quota": self.quota,
            "quota_error": self.quota_error,
            "quota_updated_at": self.quota_updated_at,
        }

    def persist(self) -> None:
        """把当前 token / expiry / 禁用状态写回来源 JSON 文件（合并写，保留其它字段）。

        env 兜底账号（无 source_path）静默跳过。供刷新成功后落盘新 token、
        以及 disable()/clear_disabled() 后持久化禁用态使用，重启后状态不丢。
        """
        if not self.source_path:
            return
        path = Path(self.source_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, json.JSONDecodeError):
            data = {}

        data["type"] = self.backend
        if self.token:
            data["token"] = self.token
            # 文件原本用 access_token / accessToken（codex/claude 等）时同步更新，避免刷新后残留旧 token
            if "access_token" in data:
                data["access_token"] = self.token
            if "accessToken" in data:
                data["accessToken"] = self.token
        if self.refresh_token:
            data["refresh_token"] = self.refresh_token
            if "refreshToken" in data:
                data["refreshToken"] = self.refresh_token
        data["enabled"] = self.enabled

        # 额度快照：有则落盘，供重启后立即展示；失败原因一并保留
        if self.quota is not None:
            data["quota"] = self.quota
        if self.quota_error:
            data["quota_error"] = self.quota_error
        else:
            data.pop("quota_error", None)
        if self.quota_updated_at:
            data["quota_updated_at"] = self.quota_updated_at
        if self.expiry:
            data["expiry"] = (
                datetime.fromtimestamp(self.expiry, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        # 禁用态：有原因就写，解除时清掉，避免残留误判
        if self.disabled_reason:
            data["disabled_reason"] = self.disabled_reason
            data["disabled_at"] = self.disabled_at
        else:
            data.pop("disabled_reason", None)
            data.pop("disabled_at", None)

        try:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass  # 写回失败不致命：内存态仍生效，下次仍会尝试


# ── 认证文件加载 ──────────────────────────────────────────────────────────────

def _parse_expiry_ts(value: object) -> float:
    """把账号文件里的 expiry 解析成 unix 时间戳。

    支持两种写法：ISO8601 字符串（如 "2026-06-27T10:00:00Z"）或纯数字 epoch 秒。
    无法解析时返回 0.0（= 不追踪过期）。
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    # 纯数字 → epoch 秒
    try:
        return float(text)
    except ValueError:
        pass
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _account_from_file(path: Path) -> Optional[Account]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    backend = str(data.get("type", "")).strip().lower()
    if not backend:
        return None
    # 兼容多种 token 字段写法：token / access_token / accessToken（Claude Code 凭据格式）/ key
    token = str(
        data.get("token")
        or data.get("access_token")
        or data.get("accessToken")
        or data.get("key")
        or ""
    ).strip()
    home = str(data.get("home", "")).strip()
    if home:
        home = str(Path(home).expanduser())
    raw_env = data.get("env")
    extra_env = {str(k): str(v) for k, v in raw_env.items()} if isinstance(raw_env, dict) else {}
    # 既无 token 也无 home 也无 env → 这条目没有任何可注入信息，跳过（占位符文件不会污染池子）。
    if not token and not home and not extra_env:
        return None
    disabled_reason = str(data.get("disabled_reason", "")).strip()
    return Account(
        backend=backend,
        id=str(data.get("email", path.stem)),
        token=token,
        refresh_token=str(data.get("refresh_token") or data.get("refreshToken") or ""),
        home=home,
        extra_env=extra_env,
        # disabled_reason 落盘后，重启仍视为禁用（除非用户手动改 enabled / 删原因）
        enabled=bool(data.get("enabled", True)) and not disabled_reason,
        expiry=_parse_expiry_ts(data.get("expiry", data.get("expires_at"))),
        disabled_reason=disabled_reason,
        disabled_at=float(data.get("disabled_at", 0.0) or 0.0),
        priority=int(data.get("priority", 0) or 0),
        weight=max(1, int(data.get("weight", 1) or 1)),
        source_path=str(path),
        quota=data.get("quota") if isinstance(data.get("quota"), dict) else None,
        quota_error=str(data.get("quota_error", "")),
        quota_updated_at=float(data.get("quota_updated_at", 0.0) or 0.0),
    )


def _account_from_env(backend: str) -> Optional[Account]:
    """从 .env / 环境变量读取兜底单账号。"""
    env_key = _ENV_FALLBACK.get(backend)
    if not env_key:
        return None
    token = os.environ.get(env_key, "").strip()
    if not token:
        return None
    return Account(backend=backend, id=f"env:{backend}", token=token)


def load_accounts(backend: Optional[str] = None) -> list[Account]:
    """从 AUTH_DIR 加载认证文件，合并 .env 兜底账号。

    backend 为 None 时加载所有 backend 的账号。
    """
    accounts: list[Account] = []
    seen_ids: set[str] = set()

    if AUTH_DIR.is_dir():
        for f in sorted(AUTH_DIR.glob("*.json")):
            acc = _account_from_file(f)
            # 注意：禁用账号（enabled=False / 带 disabled_reason）也照常入池——
            # pick() 用 is_available 跳过它们，半开探测据此在重启后自动复活（见 pool.py），
            # 状态面板也才能把禁用账号显示出来。过滤交给调度层，而非加载层。
            if acc and (backend is None or acc.backend == backend):
                key = f"{acc.backend}:{acc.id}"
                if key not in seen_ids:
                    accounts.append(acc)
                    seen_ids.add(key)

    # 补充 .env 兜底（每个 backend 最多一个兜底账号）
    backends_to_check = [backend] if backend else list(_ENV_FALLBACK.keys())
    for b in backends_to_check:
        if not any(a.backend == b for a in accounts):
            acc = _account_from_env(b)
            if acc:
                key = f"{acc.backend}:{acc.id}"
                if key not in seen_ids:
                    accounts.append(acc)
                    seen_ids.add(key)

    return accounts


def save_account(account: Account, name: Optional[str] = None) -> Path:
    """把账号信息写入 AUTH_DIR（供命令行注册账号使用）。"""
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    stem = name or f"{account.backend}_{account.id.replace('@', '_').replace('.', '_')}"
    path = AUTH_DIR / f"{stem}.json"
    account.source_path = str(path)
    path.write_text(
        json.dumps(
            {
                "type": account.backend,
                "email": account.id,
                "token": account.token,
                "refresh_token": account.refresh_token,
                "home": account.home,
                "enabled": account.enabled,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path
