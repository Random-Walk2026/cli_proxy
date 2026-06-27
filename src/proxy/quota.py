"""账号额度查询：拉取各 provider 的 usage 端点，归一化成 5 小时 / 周窗口。

已实现并验证的 backend：

  Codex（ChatGPT 订阅）——每次刷新 token 再查：
    1. 用 refresh_token 走 OAuth 刷新拿新 access_token（顺手回写账号文件）；
    2. 调 https://chatgpt.com/backend-api/wham/usage，带 ChatGPT-Account-Id（从 JWT 取）；
    3. primary_window → 5 小时窗口，secondary_window → 周窗口。

  Claude（Claude Code OAuth）——先试 usage、过期才刷新：
    1. 直接用现有 access_token 调 https://api.anthropic.com/api/oauth/usage
       （需 anthropic-beta + User-Agent，端点在 Cloudflare 后面）；
    2. 401 且有 refresh_token 时，POST https://platform.claude.com/v1/oauth/token 刷新一次再重试；
    3. five_hour.utilization → 5 小时窗口，seven_day → 周窗口。
       注意：Claude 刷新端点限流很严，必须「先试 usage、过期才刷新」，不能每次都刷。

归一化后的额度结构（backend 无关，供 UI 直接渲染）：

    {
        "plan_type": "plus" | None,
        "five_hour": {"used_percent": 4, "reset_at": 1750000000, "window_minutes": 300},
        "weekly":    {"used_percent": 33, "reset_at": 1750500000, "window_minutes": 10080},
    }

额度查询昂贵且限流，调用方应缓存到 Account.quota，仅在用户手动「刷新额度」时调用本模块。
其它 backend（grok/copilot/antigravity）暂无公开 usage 端点，fetch 返回 None 表示不支持。
"""
from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from .account import Account

# ── Codex（ChatGPT 订阅）────────────────────────────────────────────────────
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

# ── Claude（Claude Code OAuth）──────────────────────────────────────────────
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_TOKEN_ENDPOINT = "https://platform.claude.com/v1/oauth/token"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
# usage / 刷新端点都在 Cloudflare 后面，缺 User-Agent 会被 1010 拒绝；可用 env 覆盖版本号。
CLAUDE_USER_AGENT = f"claude-code/{os.environ.get('CLI_PROXY_CLAUDE_CODE_VERSION', '2.1.0')}"

# 哪些 backend 支持额度查询（其余返回 None）
QUOTA_SUPPORTED: frozenset[str] = frozenset({"codex", "claude"})

_HTTP_TIMEOUT = 30


def supports_quota(backend: str) -> bool:
    return backend in QUOTA_SUPPORTED


def _jwt_claims(token: str) -> dict:
    """解出 JWT payload（不验签，仅取 claim）。失败返回空 dict。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _codex_account_id(access_token: str) -> Optional[str]:
    auth = _jwt_claims(access_token).get("https://api.openai.com/auth", {})
    if isinstance(auth, dict):
        acct = auth.get("chatgpt_account_id")
        if acct:
            return str(acct)
    return None


def _refresh_codex_token(account: Account) -> str:
    """用 refresh_token 换新 access_token，并回写账号文件。返回新 access_token。"""
    if not account.refresh_token:
        raise RuntimeError("codex 账号缺少 refresh_token，无法刷新额度，请重新登录")
    resp = requests.post(
        CODEX_TOKEN_ENDPOINT,
        data={
            "grant_type": "refresh_token",
            "refresh_token": account.refresh_token,
            "client_id": CODEX_CLIENT_ID,
        },
        timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code >= 400:
        code = ""
        try:
            code = resp.json().get("error", {}).get("code", "")
        except ValueError:
            pass
        raise RuntimeError(f"codex token 刷新失败：{resp.status_code} {code}".strip())
    data = resp.json()
    access_token = str(data.get("access_token", "")).strip()
    if not access_token:
        raise RuntimeError("codex token 刷新响应缺少 access_token")
    # 回写新 token（同时受益于后续的实际请求路由）
    account.token = access_token
    new_refresh = str(data.get("refresh_token", "")).strip()
    if new_refresh:
        account.refresh_token = new_refresh
    account.persist()
    return access_token


def _normalize_window(window: Optional[dict]) -> Optional[dict]:
    """把 wham/usage 的窗口结构归一化成 {used_percent, reset_at, window_minutes}。"""
    if not isinstance(window, dict):
        return None
    used = window.get("used_percent")
    used_percent = max(0, min(100, int(used))) if isinstance(used, (int, float)) else None

    reset_at = window.get("reset_at")
    if not isinstance(reset_at, (int, float)):
        after = window.get("reset_after_seconds")
        reset_at = int(time.time() + after) if isinstance(after, (int, float)) and after >= 0 else None

    window_seconds = window.get("limit_window_seconds")
    window_minutes = (
        (int(window_seconds) + 59) // 60
        if isinstance(window_seconds, (int, float)) and window_seconds > 0
        else None
    )
    return {
        "used_percent": used_percent,
        "reset_at": int(reset_at) if reset_at else None,
        "window_minutes": window_minutes,
    }


def _fetch_codex_quota(account: Account, pre_refresh: bool = False) -> dict:
    """刷新 token → 调 usage → 归一化。失败抛 RuntimeError。

    codex 刷新端点不限流，一律先刷新再查（pre_refresh 对它无意义，仅为统一签名）。
    """
    access_token = _refresh_codex_token(account)
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    acct_id = _codex_account_id(access_token)
    if acct_id:
        headers["ChatGPT-Account-Id"] = acct_id

    resp = requests.get(CODEX_USAGE_URL, headers=headers, timeout=_HTTP_TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"codex usage 接口返回 {resp.status_code}")
    body = resp.json()
    rate_limit = body.get("rate_limit") or {}
    return {
        "plan_type": body.get("plan_type"),
        "five_hour": _normalize_window(rate_limit.get("primary_window")),
        "weekly": _normalize_window(rate_limit.get("secondary_window")),
    }


# ── Claude（Claude Code OAuth）──────────────────────────────────────────────

def _iso_to_ts(value: object) -> Optional[int]:
    """ISO8601（带或不带毫秒/Z）→ unix 时间戳。失败返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _normalize_claude_window(window: Optional[dict], window_minutes: int) -> Optional[dict]:
    """Claude usage 窗口 {utilization, resets_at} → {used_percent, reset_at, window_minutes}。"""
    if not isinstance(window, dict):
        return None
    util = window.get("utilization")
    used_percent = max(0, min(100, round(util))) if isinstance(util, (int, float)) else None
    return {
        "used_percent": used_percent,
        "reset_at": _iso_to_ts(window.get("resets_at")),
        "window_minutes": window_minutes,
    }


def _refresh_claude_token(account: Account) -> str:
    """用 refresh_token 换新 access_token 并回写。返回新 access_token。"""
    if not account.refresh_token:
        raise RuntimeError("claude 账号缺少 refresh_token，无法刷新，请重新登录")
    resp = requests.post(
        CLAUDE_TOKEN_ENDPOINT,
        data={
            "grant_type": "refresh_token",
            "refresh_token": account.refresh_token,
            "client_id": CLAUDE_CLIENT_ID,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": CLAUDE_USER_AGENT,
        },
        timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code == 429:
        raise RuntimeError("claude 刷新被限流（429），请过几分钟再试")
    if resp.status_code >= 400:
        raise RuntimeError(f"claude token 刷新失败：{resp.status_code}")
    data = resp.json()
    access_token = str(data.get("access_token", "")).strip()
    if not access_token:
        raise RuntimeError("claude token 刷新响应缺少 access_token")
    account.token = access_token
    new_refresh = str(data.get("refresh_token", "")).strip()
    if new_refresh:
        account.refresh_token = new_refresh
    account.persist()
    return access_token


def _claude_usage_request(access_token: str) -> requests.Response:
    return requests.get(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "anthropic-beta": CLAUDE_OAUTH_BETA,
            "User-Agent": CLAUDE_USER_AGENT,
        },
        timeout=_HTTP_TIMEOUT,
    )


def _fetch_claude_quota(account: Account, pre_refresh: bool = False) -> dict:
    """调 usage → 归一化。失败抛 RuntimeError。

    两种 token 策略：
      - 默认（pre_refresh=False）：先用现有 token 调 usage，仅在 401（token 过期）时刷新一次再重试。
        Claude 刷新端点限流很严，这样能在 token 仍新鲜时完全不碰刷新端点。
      - pre_refresh=True：像 codex 一样先刷新 token 再查（token 已知过期、或想强制拿最新额度时用）。
    """
    if pre_refresh and account.refresh_token:
        token = _refresh_claude_token(account)
    else:
        token = account.token
    if not token:
        raise RuntimeError("claude 账号无 access_token，请重新登录")

    resp = _claude_usage_request(token)
    # 未预刷新时才走 401 兜底刷新（预刷新已经换过新 token，再 401 就是真失效）
    if resp.status_code == 401 and not pre_refresh and account.refresh_token:
        token = _refresh_claude_token(account)
        resp = _claude_usage_request(token)

    if resp.status_code == 401:
        raise RuntimeError("claude access_token 失效，请重新登录（claude login）")
    if resp.status_code == 429:
        raise RuntimeError("claude usage 接口被限流（429），请过几分钟再试")
    if resp.status_code >= 400:
        raise RuntimeError(f"claude usage 接口返回 {resp.status_code}")

    body = resp.json()
    return {
        "plan_type": None,  # OAuth usage 端点不返回套餐名
        "five_hour": _normalize_claude_window(body.get("five_hour"), 300),
        "weekly": _normalize_claude_window(body.get("seven_day"), 7 * 24 * 60),
    }


# ── 调度 ──────────────────────────────────────────────────────────────────────

_FETCHERS = {
    "codex": _fetch_codex_quota,
    "claude": _fetch_claude_quota,
}


def refresh_quota(account: Account, pre_refresh: bool = False) -> Optional[dict]:
    """刷新单个账号额度并写入 account.quota / quota_error / quota_updated_at。

    pre_refresh=True 时强制先刷新 token 再查（claude 默认 usage-first 以避开刷新端点限流）。
    返回归一化额度（成功）或 None（该 backend 不支持额度查询）。失败时记录 quota_error 并抛 RuntimeError。
    """
    fetcher = _FETCHERS.get(account.backend)
    if fetcher is None:
        return None
    try:
        quota = fetcher(account, pre_refresh=pre_refresh)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        account.quota_error = str(exc)
        account.quota_updated_at = time.time()
        account.persist()
        raise RuntimeError(str(exc)) from exc
    account.quota = quota
    account.quota_error = ""
    account.quota_updated_at = time.time()
    account.persist()
    return quota
