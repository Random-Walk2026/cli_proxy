"""FastAPI 服务：OpenAI-compatible /v1/chat/completions。

架构要点（对齐 CLIProxyAPI）：
  1. asyncio + ThreadPoolExecutor：CLI subprocess 在线程里跑，不阻塞事件循环
  2. 账号池轮换：pool.pick() → env_override 注入 → mark_success/mark_failed
  3. 重试循环：单个账号失败时自动切换下一个（最多 max_retries 次）
  4. SSE streaming：CLI 是同步的，完整结果包装成 SSE data 流返回
  5. 管理 API：GET /v0/management/accounts 查看账号池状态
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .account import Account
from .anthropic import (
    AnthropicMessagesRequest,
    anthropic_message_body,
    anthropic_sse_response,
    estimated_token_count,
)
from .pool import get_pool
from .providers import get_provider, SUPPORTED
from .providers.antigravity_http import AntigravityHTTPProvider
from .quota import QUOTA_SUPPORTED, refresh_quota, supports_quota
from .router import parse_model

app = FastAPI(title="cli_proxy API", version="2.0.0")

# 自包含的账号状态仪表盘（静态 HTML，无构建步骤），与 Streamlit 管理台同放在 ui/ 下
_UI_DIR = Path(__file__).parent / "ui"

# CLI subprocess 在专用线程池里跑
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="cli_proxy")

MAX_RETRIES = 3  # 最多尝试几个账号
_antigravity_http_provider = AntigravityHTTPProvider()


# ── 请求 / 响应 schema ────────────────────────────────────────────────────────

class Message(BaseModel):
    role: str
    content: str


class CompletionRequest(BaseModel):
    model: str
    messages: list[Message]
    stream: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None


# ── 消息 → 纯文本 ─────────────────────────────────────────────────────────────

def _to_text(messages: list[Message]) -> str:
    parts: list[str] = []
    for m in messages:
        if m.role == "system":
            parts.append(f"[System]\n{m.content}")
        elif m.role == "user":
            parts.append(m.content)
        else:
            parts.append(f"[{m.role.capitalize()}]\n{m.content}")
    return "\n\n".join(parts)


# ── 响应构造 ──────────────────────────────────────────────────────────────────

def _non_stream_body(content: str, model: str, req_id: str) -> dict:
    return {
        "id": f"chatcmpl-{req_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _stream_response(
    content: str, model: str, req_id: str
) -> AsyncIterator[str]:
    """把完整文本包装成 SSE stream（CLI 同步，模拟流式）。"""
    cid = f"chatcmpl-{req_id}"
    ts = int(time.time())

    def chunk(delta: dict, finish: Optional[str] = None) -> str:
        payload = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": ts,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield chunk({"role": "assistant", "content": ""})
    yield chunk({"content": content})
    yield chunk({}, finish="stop")
    yield "data: [DONE]\n\n"


# ── CLI 执行（含账号轮换 + 重试）────────────────────────────────────────────

async def _run_with_pool(
    provider_name: str,
    text: str,
    model: str,
    effort: str,
    *,
    max_retries: int = MAX_RETRIES,
) -> str:
    """在线程池里跑 CLI，多账号轮换，失败时自动重试。"""
    pool = get_pool()
    provider = get_provider(provider_name)
    last_exc: Optional[Exception] = None
    tried: set[str] = set()

    attempts = max(1, max_retries, len(pool.accounts(provider_name)))
    for _ in range(attempts):
        account = pool.pick(provider_name)
        env_override = account.env_override() if account else None
        acct_id = account.id if account else "env-default"

        if acct_id in tried:
            break  # 已经试过所有账号
        tried.add(acct_id)

        loop = asyncio.get_event_loop()
        try:
            content = await loop.run_in_executor(
                _executor,
                lambda acc=account, env=env_override: provider.run(
                    text, model, effort, env_override=env
                ),
            )
            if account:
                pool.mark_success(account)
            return content
        except RuntimeError as exc:
            last_exc = exc
            if account:
                pool.mark_failed(account, exc)
            print(
                f"  [cli_proxy] {provider_name}/{acct_id} 失败：{exc}；"
                f"{'继续尝试下一个账号…' if _ + 1 < max_retries else '已无可用账号。'}"
            )

    raise last_exc or RuntimeError(f"{provider_name} 所有账号均不可用。")


def _authorize_optional(request: Request) -> None:
    """Require a local API key only when CLI_PROXY_API_KEY is configured."""

    expected = os.environ.get("CLI_PROXY_API_KEY", "").strip()
    if not expected:
        return
    auth = request.headers.get("authorization", "")
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    token = token or request.headers.get("x-api-key", "").strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid CLI_PROXY_API_KEY")


async def _run_anthropic_with_pool(
    req: AnthropicMessagesRequest,
    *,
    max_retries: int = MAX_RETRIES,
) -> str:
    """Run Anthropic /v1/messages through Antigravity profile accounts."""

    pool = get_pool()
    last_exc: Optional[Exception] = None
    tried: set[str] = set()

    attempts = max(1, max_retries, len(pool.accounts("antigravity")))
    for _ in range(attempts):
        account = pool.pick("antigravity")
        if account is None:
            account = Account(
                backend="antigravity",
                id="env-default",
                home=os.environ.get("HOME", ""),
            )
        acct_id = account.id

        if acct_id in tried:
            break
        tried.add(acct_id)

        loop = asyncio.get_event_loop()
        try:
            content = await loop.run_in_executor(
                _executor,
                lambda acc=account: _antigravity_http_provider.run_messages(req, acc),
            )
            if account.id != "env-default":
                pool.mark_success(account)
            return content
        except RuntimeError as exc:
            last_exc = exc
            if account.id != "env-default":
                pool.mark_failed(account, exc)
            print(
                f"  [cli_proxy] antigravity/{acct_id} 失败：{exc}；"
                f"{'继续尝试下一个账号…' if _ + 1 < max_retries else '已无可用账号。'}"
            )

    raise last_exc or RuntimeError("antigravity 所有账号均不可用。")


# ── 路由 ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """账号状态仪表盘：可视化展示账号池（调用 /v0/management/accounts）。"""
    page = _UI_DIR / "dashboard.html"
    try:
        return HTMLResponse(page.read_text(encoding="utf-8"))
    except OSError:
        raise HTTPException(status_code=500, detail="dashboard.html 缺失")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


@app.get("/v1/models")
async def list_models():
    pool = get_pool()
    status = pool.status()
    data = []
    for backend in SUPPORTED:
        accounts = status.get(backend, [])
        data.append({
            "id": backend,
            "object": "model",
            "created": 0,
            "owned_by": "cli_proxy",
            "accounts": len(accounts),
        })
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(req: CompletionRequest):
    provider_name, model, effort = parse_model(req.model)

    if provider_name not in SUPPORTED:
        raise HTTPException(
            status_code=400,
            detail=(
                f"未知 provider '{provider_name}'。支持：{', '.join(SUPPORTED)}。\n"
                f"格式：<provider>/<model>@<effort> 或 <model>(effort)，"
                f"例如 claude/sonnet@high、gpt-5.5(high)、grok-4(medium)"
            ),
        )

    text = _to_text(req.messages)
    req_id = uuid.uuid4().hex[:8]

    try:
        content = await _run_with_pool(provider_name, text, model, effort)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if req.stream:
        return StreamingResponse(
            _stream_response(content, req.model, req_id),
            media_type="text/event-stream",
        )

    return JSONResponse(_non_stream_body(content, req.model, req_id))


@app.post("/v1/messages")
async def anthropic_messages(req: AnthropicMessagesRequest, request: Request):
    _authorize_optional(request)
    req_id = uuid.uuid4().hex[:8]

    try:
        content = await _run_anthropic_with_pool(req)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if req.stream:
        return StreamingResponse(
            anthropic_sse_response(content, req.model, req_id),
            media_type="text/event-stream",
        )

    return JSONResponse(anthropic_message_body(content, req.model, req_id))


@app.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(req: AnthropicMessagesRequest, request: Request):
    _authorize_optional(request)
    return {"input_tokens": estimated_token_count(req)}


# ── 管理 API（对齐 CLIProxyAPI /v0/management/*）────────────────────────────

@app.get("/v0/management/accounts")
async def management_accounts():
    """列出所有 provider 的账号池状态（脱敏）。"""
    pool = get_pool()
    return {"accounts": pool.status()}


@app.get("/v0/management/accounts/{backend}")
async def management_accounts_backend(backend: str):
    """列出指定 backend 的账号状态。"""
    if backend not in SUPPORTED:
        raise HTTPException(status_code=404, detail=f"未知 provider：{backend}")
    pool = get_pool()
    return {"backend": backend, "accounts": pool.status().get(backend, [])}


class AccountAction(BaseModel):
    backend: str
    id: str
    action: str  # enable | disable | reset | refresh_quota
    pre_refresh: bool = False  # refresh_quota 时是否强制先刷新 token（claude 默认 usage-first）


@app.post("/v0/management/accounts/action")
async def management_account_action(req: AccountAction):
    """对单个账号执行管理操作（启用 / 禁用 / 重置冷却 / 刷新额度）。供管理面板按钮调用。"""
    pool = get_pool()
    action = req.action.lower().strip()
    if action == "enable":
        acc = pool.set_enabled(req.backend, req.id, True)
    elif action == "disable":
        acc = pool.set_enabled(req.backend, req.id, False)
    elif action == "reset":
        acc = pool.reset_account(req.backend, req.id)
    elif action == "refresh_quota":
        acc = pool.find(req.backend, req.id)
        if acc is None:
            raise HTTPException(status_code=404, detail=f"未找到账号：{req.backend}/{req.id}")
        if not supports_quota(acc.backend):
            raise HTTPException(status_code=400, detail=f"{acc.backend} 不支持额度查询")
        try:
            await asyncio.get_event_loop().run_in_executor(
                _executor, lambda: refresh_quota(acc, pre_refresh=req.pre_refresh)
            )
        except RuntimeError as exc:
            # 刷新失败时 quota_error 已写入账号，仍返回账号（前端展示错误）
            return JSONResponse(
                status_code=502,
                content={"status": "error", "detail": str(exc), "account": acc.to_dict()},
            )
        return {"status": "ok", "account": acc.to_dict()}
    else:
        raise HTTPException(status_code=400, detail=f"未知操作：{req.action}")
    if acc is None:
        raise HTTPException(
            status_code=404, detail=f"未找到账号：{req.backend}/{req.id}"
        )
    return {"status": "ok", "account": acc.to_dict()}


@app.post("/v0/management/quota/refresh")
async def management_quota_refresh(pre_refresh: bool = False):
    """刷新所有支持额度查询的账号（codex / claude），返回每个账号的结果。

    pre_refresh=true 时强制先刷新各账号 token 再查（claude 默认 usage-first 以避开刷新端点限流）。
    """
    pool = get_pool()
    loop = asyncio.get_event_loop()
    results: list[dict] = []
    for backend in QUOTA_SUPPORTED:
        for acc in pool.accounts(backend):
            entry = {"backend": acc.backend, "id": acc.id}
            try:
                await loop.run_in_executor(
                    _executor, lambda a=acc: refresh_quota(a, pre_refresh=pre_refresh)
                )
                entry["status"] = "ok"
            except RuntimeError as exc:
                entry["status"] = "error"
                entry["detail"] = str(exc)
            results.append(entry)
    return {"status": "ok", "results": results}


@app.post("/v0/management/reload")
async def management_reload():
    """重新从磁盘加载账号文件（添加新账号后调用）。"""
    get_pool().reload()
    return {"status": "reloaded"}
