"""cli_proxy 账号管理台（Streamlit 版）。

比内嵌 HTML 面板更"可操作"：表格 + 筛选 + 启用/禁用/重置/reload 按钮。
作为独立进程运行，通过管理 API 与正在运行的 cli_proxy 服务通信。

启动：

    streamlit run src/proxy/ui/streamlit_app.py

代理地址默认 http://127.0.0.1:8317，可用环境变量 CLI_PROXY_URL 覆盖，
或在左侧栏直接修改。若设置了 CLI_PROXY_API_KEY，也在左侧栏填入。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import requests
import streamlit as st

DEFAULT_URL = os.environ.get("CLI_PROXY_URL", "http://127.0.0.1:8317")
STATUS_LABEL = {"available": "🟢 可用", "cooling": "🟡 冷却中", "disabled": "🔴 已禁用"}


def _headers() -> dict:
    key = st.session_state.get("api_key", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _base() -> str:
    return st.session_state.get("base_url", DEFAULT_URL).rstrip("/")


def fetch_accounts() -> dict:
    res = requests.get(
        f"{_base()}/v0/management/accounts", headers=_headers(), timeout=10
    )
    res.raise_for_status()
    return res.json().get("accounts", {})


def do_action(backend: str, account_id: str, action: str, pre_refresh: bool = False) -> tuple[bool, str]:
    try:
        res = requests.post(
            f"{_base()}/v0/management/accounts/action",
            json={"backend": backend, "id": account_id, "action": action, "pre_refresh": pre_refresh},
            headers=_headers(),
            timeout=120,
        )
        if res.status_code == 200:
            return True, "成功"
        return False, res.json().get("detail", res.text)
    except requests.RequestException as exc:
        return False, str(exc)


def do_reload() -> tuple[bool, str]:
    try:
        res = requests.post(
            f"{_base()}/v0/management/reload", headers=_headers(), timeout=10
        )
        res.raise_for_status()
        return True, "已重新加载账号文件"
    except requests.RequestException as exc:
        return False, str(exc)


def do_refresh_quota(pre_refresh: bool = False) -> tuple[bool, str]:
    try:
        res = requests.post(
            f"{_base()}/v0/management/quota/refresh",
            params={"pre_refresh": str(pre_refresh).lower()},
            headers=_headers(),
            timeout=180,
        )
        res.raise_for_status()
        results = res.json().get("results", [])
        ok = sum(r.get("status") == "ok" for r in results)
        return True, f"额度已刷新：{ok}/{len(results)} 个账号成功"
    except requests.RequestException as exc:
        return False, str(exc)


def fmt_expiry(ts: float) -> str:
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
    diff = ts - datetime.now(tz=timezone.utc).timestamp()
    when = dt.strftime("%Y-%m-%d %H:%M")
    if diff <= 0:
        return f"{when}（已过期）"
    mins = int(diff // 60)
    if mins < 60:
        return f"{when}（{mins} 分钟后）"
    hrs = mins // 60
    if hrs < 48:
        return f"{when}（{hrs} 小时后）"
    return f"{when}（{hrs // 24} 天后）"


def fmt_cooling(secs: int) -> str:
    if not secs:
        return "—"
    return f"{secs} 秒" if secs < 60 else f"{secs // 60} 分 {secs % 60} 秒"


def fmt_reset(ts) -> str:
    if not ts:
        return ""
    diff = ts - datetime.now(tz=timezone.utc).timestamp()
    if diff <= 0:
        return "即将重置"
    mins = int(diff // 60)
    if mins < 60:
        return f"{mins} 分钟后重置"
    hrs = mins // 60
    if hrs < 48:
        return f"{hrs} 小时后重置"
    return f"{hrs // 24} 天后重置"


def render_quota(a: dict) -> None:
    """在账号行下渲染 5 小时 / 周额度进度条。"""
    if a.get("quota_error"):
        st.caption(f"⚠️ 额度获取失败：{a['quota_error']}")
        return
    q = a.get("quota")
    if not q:
        return
    plan = q.get("plan_type")
    label = f"额度（plan：{plan}）" if plan else "额度"
    st.caption(label)
    cols = st.columns(2)
    for col, (name, key) in zip(cols, [("5 小时", "five_hour"), ("本周", "weekly")]):
        win = q.get(key)
        if not win or win.get("used_percent") is None:
            continue
        used = win["used_percent"]
        with col:
            st.progress(min(100, used) / 100, text=f"{name}：已用 {used}% · {fmt_reset(win.get('reset_at'))}")


def main() -> None:
    st.set_page_config(page_title="cli_proxy 账号管理台", page_icon="🛰️", layout="wide")
    st.session_state.setdefault("base_url", DEFAULT_URL)
    st.session_state.setdefault("api_key", os.environ.get("CLI_PROXY_API_KEY", ""))

    with st.sidebar:
        st.header("⚙️ 连接")
        st.session_state["base_url"] = st.text_input("代理地址", st.session_state["base_url"])
        st.session_state["api_key"] = st.text_input(
            "API Key（可选）", st.session_state["api_key"], type="password"
        )
        if st.button("🔄 reload 账号文件", use_container_width=True):
            ok, msg = do_reload()
            (st.success if ok else st.error)(msg)
        pre_refresh = st.checkbox(
            "拉取额度前预刷新 token",
            help="claude 默认先用现有 token 查、401 才刷新（避开刷新端点限流）；勾选则强制先刷新。codex 始终预刷新。",
        )
        if st.button("📊 刷新额度（5h/周）", use_container_width=True):
            with st.spinner("正在拉取各账号额度…"):
                ok, msg = do_refresh_quota(pre_refresh)
            (st.success if ok else st.error)(msg)
        st.caption("额度查询较慢且有限流，目前支持 codex / claude；其余操作后页面会自动刷新。")
    st.session_state["pre_refresh"] = pre_refresh

    st.title("🛰️ cli_proxy 账号管理台")

    try:
        accounts = fetch_accounts()
    except requests.RequestException as exc:
        st.error(f"无法连接到 cli_proxy（{_base()}）：{exc}")
        st.info("请确认服务已启动：`python -m proxy --port 8317`")
        return

    all_accts = [a for accs in accounts.values() for a in accs]
    if not all_accts:
        st.warning("尚未加载到任何账号。请在 ~/.cli_proxy_api/ 放置账号文件，或在 .env 配置令牌。")
        return

    # ── 汇总 ──────────────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("账号总数", len(all_accts))
    c2.metric("🟢 可用", sum(a["status"] == "available" for a in all_accts))
    c3.metric("🟡 冷却中", sum(a["status"] == "cooling" for a in all_accts))
    c4.metric("🔴 已禁用", sum(a["status"] == "disabled" for a in all_accts))

    # ── 筛选 ──────────────────────────────────────────────────────────────
    backends = sorted(accounts.keys())
    fc1, fc2 = st.columns([2, 2])
    sel_backends = fc1.multiselect("后端筛选", backends, default=backends)
    sel_status = fc2.multiselect(
        "状态筛选", ["available", "cooling", "disabled"],
        default=["available", "cooling", "disabled"],
        format_func=lambda s: STATUS_LABEL.get(s, s),
    )

    st.divider()

    for backend in sel_backends:
        accs = [a for a in accounts.get(backend, []) if a["status"] in sel_status]
        if not accs:
            continue
        st.subheader(f"{backend} · {len(accs)} 个账号")
        for a in accs:
            cols = st.columns([3, 2, 2, 2, 3])
            cols[0].markdown(f"**{a['id']}**  \n`{a.get('token') or a.get('home') or '—'}`")
            cols[1].markdown(STATUS_LABEL.get(a["status"], a["status"]))
            cols[2].markdown(
                f"优先级/权重\n\n**{a['priority']} / {a['weight']}**"
            )
            detail = []
            if a.get("expiry"):
                detail.append(f"到期：{fmt_expiry(a['expiry'])}")
            if a["status"] == "cooling":
                detail.append(f"冷却剩余：{fmt_cooling(a['cooling_seconds'])}")
            if a.get("error_count"):
                detail.append(f"失败 {a['error_count']} 次")
            if a["status"] == "disabled" and a.get("disabled_reason"):
                detail.append(f"原因：{a['disabled_reason']}")
            cols[3].caption("  \n".join(detail) if detail else "—")

            with cols[4]:
                bc = st.columns(3)
                if a["status"] == "disabled":
                    if bc[0].button("启用", key=f"en-{backend}-{a['id']}", use_container_width=True):
                        ok, msg = do_action(backend, a["id"], "enable")
                        (st.toast if ok else st.error)(msg)
                        st.rerun()
                else:
                    if bc[0].button("禁用", key=f"di-{backend}-{a['id']}", use_container_width=True):
                        ok, msg = do_action(backend, a["id"], "disable")
                        (st.toast if ok else st.error)(msg)
                        st.rerun()
                if bc[1].button("重置", key=f"re-{backend}-{a['id']}", use_container_width=True):
                    ok, msg = do_action(backend, a["id"], "reset")
                    (st.toast if ok else st.error)(msg)
                    st.rerun()
                if bc[2].button("额度", key=f"q-{backend}-{a['id']}", use_container_width=True,
                                help="拉取该账号的 5 小时 / 周额度（codex / claude 支持）"):
                    with st.spinner("拉取额度中…"):
                        ok, msg = do_action(backend, a["id"], "refresh_quota",
                                            st.session_state.get("pre_refresh", False))
                    (st.toast if ok else st.error)(msg)
                    st.rerun()
            render_quota(a)
        st.divider()


if __name__ == "__main__":
    main()
