# cli_proxy

本地 OpenAI-compatible 代理包，把 **Claude / Codex / Grok / Antigravity / Copilot** 订阅 CLI 变成标准 HTTP API。

架构思路源自 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)（Go 版），本包是纯 Python 实现，与 Go 版共用相同的认证文件格式和目录约定。

---

## 目录

- [快速上手](#快速上手)
- [Claude Code 接入 Antigravity](#claude-code-接入-antigravity)
- [账号配置目录 ~/.cli_proxy_api/](#账号配置目录-cli_proxy_api)
- [每个 backend 的账号文件格式](#每个-backend-的账号文件格式)
- [模型字符串格式](#模型字符串格式)
- [三种调用方式](#三种调用方式)
- [管理 API](#管理-api)
- [账号状态面板](#账号状态面板)
- [环境变量参考](#环境变量参考)
- [Antigravity 特殊说明](#antigravity-特殊说明)

---

## 快速上手

```bash
# 1. 安装本包（开发模式）
python -m pip install -e /Users/lyzhk/GitHub/cli_proxy

# 2. 配置账号（见下文）

# 3. 启动服务
python -m proxy              # 默认 127.0.0.1:8317
python -m proxy --port 8318  # 指定端口

# 4. 调用
curl http://127.0.0.1:8317/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any-value" \
  -d '{"model":"claude/sonnet@high","messages":[{"role":"user","content":"你好"}]}'
```

---

## 目录结构说明

本仓库使用 Python 公共包常见的 `src` layout：

```text
cli_proxy/              # GitHub 仓库根目录
├── src/
│   └── proxy/          # 真正的 Python 包；import proxy 读这里
├── docs/
├── tests/
└── pyproject.toml
```

Python 包名为 `proxy`，CLI 脚本入口仍叫 `cli_proxy`（两者独立，不再同名）。`src` layout 可以避免测试时误导入仓库根目录里的文件，是 Python 包常见做法。

如果运行：

```bash
python -m proxy
```

出现：

```text
No module named proxy
```

说明当前 Python 环境还没有安装本包。先在当前环境执行：

```bash
python -m pip install -e /Users/lyzhk/GitHub/cli_proxy
```

再验证：

```bash
python -m proxy --help
cli_proxy --help  # CLI 脚本入口名仍是 cli_proxy
```

---

## Claude Code 接入 Antigravity

`proxy` 现在同时提供 Anthropic-compatible `/v1/messages`，Claude Code 可以把本地代理当成 Anthropic API 使用，实际请求会走你的 Antigravity profile 账号池。

```bash
# 1. 启动本地代理
python -m proxy --port 8317

# 2. 让 Claude Code 指向本地代理
export ANTHROPIC_BASE_URL="http://127.0.0.1:8317"
export ANTHROPIC_AUTH_TOKEN="local-any-value"

# 3. 正常启动 Claude Code
claude --model claude-sonnet-4-6
```

如果想给本地代理加一层简单鉴权：

```bash
export CLI_PROXY_API_KEY="your-local-secret"
python -m proxy --port 8317

export ANTHROPIC_BASE_URL="http://127.0.0.1:8317"
export ANTHROPIC_AUTH_TOKEN="your-local-secret"
claude --model claude-sonnet-4-6
```

当前实现是 MVP：Claude Code 的 Anthropic SSE 格式已支持，Antigravity 账号池会轮换；优先尝试直连 Cloud Code Assist，token 过期或直连不可用时会退回同 profile 的 `agy --print`。上游返回仍先完整收齐再包装成 SSE，不是逐 token 真流式。复杂 tool_use 的精确回传和 thinking signature 透传还可以继续增强。

完整接入指南见 [docs/claude_code_guide.md](./docs/claude_code_guide.md)。

---

## 账号配置目录 `~/.cli_proxy_api/`

这是认证文件目录，与 CLIProxyAPI（Go 版）**共用相同路径和格式**，可以互通：

```
~/.cli_proxy_api/           # 默认目录（可通过 CLI_PROXY_AUTH_DIR 覆盖）
├── claude_work.json         # 一个 Claude 账号
├── claude_personal.json     # 另一个 Claude 账号（自动轮换）
├── codex_main.json          # Codex 账号
├── grok_account1.json       # Grok 账号
├── antigravity_google.json  # Antigravity（Google Cloud Code Assist）账号
└── copilot_pat.json         # GitHub Copilot 账号
```

**文件名规则**：随意命名，只要以 `.json` 结尾即可。`type` 字段决定属于哪个 backend。

**多账号**：同一 backend 放多个文件 → 自动 round-robin 轮换，失败账号自动冷却并切换下一个。

**权限建议**：

```bash
chmod 600 ~/.cli_proxy_api/*.json   # 保护 token 文件
```

---

## 每个 backend 的账号文件格式

### Claude（claude_*.json）

Claude Code CLI 的 OAuth token。来源：`claude login` 后从 `~/.claude/` 目录复制，或从 `CLAUDE_CODE_OAUTH_TOKEN` 环境变量获取。

```json
{
    "type": "claude",
    "email": "you@example.com",
    "access_token": "YOUR_CLAUDE_TOKEN",
    "refresh_token": "rt-XXXXX...",
    "expired": "2025-12-31T00:00:00Z"
}
```

字段说明：

| 字段 | 必填 | 说明 |
|------|------|------|
| `type` | ✅ | 固定为 `"claude"` |
| `email` | 可选 | 账号标识，仅用于显示 |
| `access_token` | ✅ | OAuth access token（`sk-ant-...`）|
| `refresh_token` | 可选 | 刷新用（当前版本不自动刷新）|
| `expired` | 可选 | 过期时间，仅记录用 |

> **最简版本**（单账号时可直接在 `.env` 里写，无需创建文件）：
> ```
> CLAUDE_CODE_OAUTH_TOKEN=YOUR_CLAUDE_TOKEN
> ```

---

### Codex（codex_*.json）

OpenAI Codex CLI 的 OAuth token。来源：`codex login` 后的登录态。

```json
{
    "type": "codex",
    "email": "you@example.com",
    "access_token": "eyJhbGc..."
}
```

---

### Grok（grok_*.json）

xAI Grok CLI 的认证。来源：`grok login` 后的登录态。

```json
{
    "type": "grok",
    "email": "you@example.com",
    "access_token": "xai-XXXXX..."
}
```

---

### Antigravity（antigravity_*.json）

> ⚠️ **Antigravity ≠ Gemini API**
>
> Antigravity 是 **Google Cloud Code Assist**（前身为 Cloud Code AI），底层走 `cloudcode-pa.googleapis.com`，使用 Google OAuth2 认证。和普通 `GEMINI_API_KEY` 完全不同——它是 Google 账号的订阅额度，不是 API key 计费。

推荐给每个 Antigravity 账号准备一个独立 profile home，并在账号 JSON 里填写 `home`。`proxy` 会从该 profile 下读取 `antigravity-oauth-token`；如果直连 token 不可用，会退回同 profile 的 `agy --print`。

```json
{
    "type": "antigravity",
    "email": "you@google.com",
    "home": "~/.cli_proxy_api/profiles/agy_main",
    "enabled": true
}
```

profile 内的 token 文件路径通常是：

```text
~/.cli_proxy_api/profiles/agy_main/.gemini/antigravity-cli/antigravity-oauth-token
```

---

### Copilot（copilot_*.json）

GitHub Copilot 的 Personal Access Token（PAT）或 fine-grained PAT。

```json
{
    "type": "copilot",
    "email": "you@github.com",
    "token": "ghp_XXXXX..."
}
```

> 也可直接在 `.env` 里写：
> ```
> COPILOT_GITHUB_TOKEN=ghp_XXXXX...
> ```

---

## 模型字符串格式

支持多种格式，对齐 CLIProxyAPI 的 model string 规范：

### 显式 provider 前缀（推荐）

```
claude                      → Claude CLI，默认模型，无思考强度
claude/sonnet               → Claude CLI，sonnet 模型
claude/sonnet@high          → Claude CLI，sonnet 模型，high effort
claude@high                 → Claude CLI，默认模型，high effort
codex/gpt-5.5               → Codex CLI，gpt-5.5 模型
grok/grok-4                 → Grok CLI，grok-4 模型
antigravity/gemini-3.5-flash → Antigravity CLI
copilot/gpt-4.1@medium      → Copilot CLI，medium effort
```

### 括号 effort 格式（兼容 CLIProxyAPI 风格）

```
claude/sonnet(high)         → 等价于 claude/sonnet@high
gpt-5.5(high)               → 自动推断为 codex/gpt-5.5@high
grok-4(medium)              → 自动推断为 grok/grok-4@medium
gemini-3.5-flash(high)      → 自动推断为 antigravity/gemini-3.5-flash@high
```

### 模型名自动推断 backend

| 模型名前缀 | 推断为 |
|-----------|--------|
| `claude-*` | `claude` |
| `gpt-*`, `o1`, `o3`, `o4*` | `codex` |
| `grok-*` | `grok` |
| `gemini-*` | `antigravity` |

---

### Antigravity 的 effort → 模型变体

Antigravity CLI（`agy`）没有独立的 `--effort` 参数，思考强度通过**模型名变体**表达：

| 基础模型 | effort | 最终 --model 参数 |
|---------|--------|-----------------|
| `gemini-3.5-flash` | `high` | `gemini-3.5-flash-high` |
| `gemini-3.5-flash` | `low` | `gemini-3.5-flash-low` |
| `gemini-3.1-pro` | `low` | `gemini-3.1-pro-low` |
| `claude-sonnet-4-6` | `high` | `claude-sonnet-4-6-thinking` |

直接写变体名时原样传入，不再叠加：`antigravity/gemini-3.5-flash-high` → `--model gemini-3.5-flash-high`。

---

## 三种调用方式

### 方式 A：直接调 runner（无 HTTP 开销，适合同进程内嵌）

```python
from proxy.runner import run_cli

reply = run_cli("claude", "你好", model="sonnet", effort="high")
print(reply)
```

### 方式 B：HTTP 客户端（先 `python -m proxy` 启动服务）

```python
from proxy import get_client

client = get_client()   # 默认 http://127.0.0.1:8317/v1
resp = client.chat.completions.create(
    model="claude/sonnet@high",
    messages=[{"role": "user", "content": "你好"}],
    stream=True,        # 支持流式
)
for chunk in resp:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

### 方式 C：LangChain 接入（先启动服务）

```python
from proxy import get_langchain_model

llm = get_langchain_model("claude/sonnet")
result = llm.invoke("你好")
print(result.content)
```

---

## 管理 API

服务运行时可通过以下端点管理账号池（对齐 CLIProxyAPI `/v0/management/*`）：

```bash
# 查看所有账号状态
curl http://127.0.0.1:8317/v0/management/accounts

# 查看指定 backend 的账号
curl http://127.0.0.1:8317/v0/management/accounts/claude

# 添加账号文件后重新加载（无需重启服务）
curl -X POST http://127.0.0.1:8317/v0/management/reload

# 启用 / 禁用 / 重置 / 刷新额度（action ∈ enable | disable | reset | refresh_quota）
curl -X POST http://127.0.0.1:8317/v0/management/accounts/action \
  -H "Content-Type: application/json" \
  -d '{"backend":"claude","id":"work@example.com","action":"disable"}'

# 刷新所有账号的额度（5 小时 / 周；目前支持 codex / claude）
curl -X POST http://127.0.0.1:8317/v0/management/quota/refresh

# 查看可用模型列表
curl http://127.0.0.1:8317/v1/models

# 健康检查
curl http://127.0.0.1:8317/health
```

账号状态返回示例：

```json
{
  "accounts": {
    "claude": [
      {"backend": "claude", "id": "work@example.com", "status": "available", "available": true, "cooling": false, "cooling_seconds": 0, "error_count": 0, "quota": null}
    ],
    "codex": [
      {"backend": "codex", "id": "hk@example.com", "status": "available", "available": true, "error_count": 0,
       "quota": {"plan_type": "plus",
                 "five_hour": {"used_percent": 4, "reset_at": 1782556912, "window_minutes": 300},
                 "weekly": {"used_percent": 33, "reset_at": 1783084966, "window_minutes": 10080}},
       "quota_error": ""}
    ]
  }
}
```

---

## 账号状态面板

两种可视化界面，按需选用：

**① 内嵌 HTML 面板（零依赖，随服务自带）**

服务启动后直接访问 <http://127.0.0.1:8317/>（或 `/dashboard`）。卡片式展示各 backend 的账号、状态徽章（🟢 可用 / 🟡 冷却中 / 🔴 已禁用）、脱敏令牌、优先级/权重、令牌到期倒计时、冷却剩余、失败次数、禁用原因，5 秒自动刷新。无需安装任何额外依赖。

点「刷新额度」可拉取各账号的 **5 小时 / 周额度**（进度条 + 重置倒计时 + 套餐类型）。额度查询较慢且有限流，因此不随状态自动刷新，按需手动触发。

目前支持 **codex** 和 **claude**：

| backend | usage 端点 | token 策略 |
| --- | --- | --- |
| codex | `chatgpt.com/backend-api/wham/usage` | 每次用 `refresh_token` 刷新后查询 |
| claude | `api.anthropic.com/api/oauth/usage`（需 `anthropic-beta` + `User-Agent`） | 先用现有 token 查，401 才刷新（刷新端点限流严，避免空刷） |

其余 backend（grok / copilot / antigravity）暂无公开 usage 端点，不显示额度。Claude 的 `User-Agent` 版本号可用 `CLI_PROXY_CLAUDE_CODE_VERSION` 覆盖。

**② Streamlit 管理台（带操作按钮）**

需要表格筛选 + 一键启用/禁用/重置/reload 时使用：

```bash
python -m pip install -e ".[ui]"            # 安装 streamlit
python -m streamlit run src/proxy/ui/streamlit_app.py
```

默认连接 <http://127.0.0.1:8317>，可用 `CLI_PROXY_URL` 环境变量或左侧栏修改；若服务设了 `CLI_PROXY_API_KEY`，在左侧栏填入即可。操作按钮通过 `/v0/management/accounts/action` 端点生效并落盘，含每账号「额度」按钮与左侧栏「刷新额度」批量按钮，额度以进度条展示 5 小时 / 周用量。

---

## 环境变量参考

所有变量都可以写在项目根目录的 `.env` 里，优先级高于账号文件中的默认值。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CLI_PROXY_AUTH_DIR` | `~/.cli_proxy_api` | 认证文件目录 |
| `AGENT_LLM_CLI_TIMEOUT` | `600` | CLI 调用超时（秒）|
| `CLAUDE_CLI_BIN` | `claude` | Claude CLI 可执行文件路径 |
| `CODEX_CLI_BIN` | `codex` | Codex CLI 可执行文件路径 |
| `GROK_CLI_BIN` | `grok` | Grok CLI 可执行文件路径 |
| `ANTIGRAVITY_CLI_BIN` | `~/.local/bin/agy` 或 `agy` | Antigravity CLI 路径 |
| `COPILOT_CLI_BIN` | `copilot` | Copilot CLI 可执行文件路径 |
| `CLAUDE_CODE_OAUTH_TOKEN` | — | Claude 单账号兜底 token（无文件时用）|
| `COPILOT_GITHUB_TOKEN` | — | Copilot 单账号兜底 token |
| `CLI_PROXY_API_KEY` | — | 设置后 `/v1/messages` 需要匹配的 Bearer token 或 `x-api-key` |
| `ANTIGRAVITY_PROJECT_ID` | 自动探测 | Cloud Code Assist project，自动探测失败时可手动指定 |
| `ANTIGRAVITY_CLOUDCODE_ENDPOINT` | 多端点 fallback | 覆盖 Cloud Code Assist v1internal endpoint |
| `ANTIGRAVITY_OAUTH_CLIENT_ID` | — | Antigravity token 过期后自动刷新所需 OAuth client id |
| `ANTIGRAVITY_OAUTH_CLIENT_SECRET` | — | Antigravity token 过期后自动刷新所需 OAuth client secret |

---

## 冷却机制

账号失败时自动进入冷却，冷却期间跳过并切换下一个账号：

| 错误类型 | 冷却时长 |
|---------|--------|
| 配额耗尽 / 限速（429、quota exceeded）| 60 秒 |
| 瞬时错误（超时、5xx、连接失败）| 15 秒 |

冷却结束后自动恢复参与轮换，无需手动干预。

---

## 与 CLIProxyAPI（Go 版）的关系

| 特性 | CLIProxyAPI（Go）| cli_proxy（Python）|
|------|-----------------|-------------------|
| 认证文件目录 | `~/.cli_proxy_api/` | `~/.cli_proxy_api/`（相同）|
| 认证文件格式 | JSON，`type` 字段 | JSON，`type` 字段（相同）|
| 多账号轮换 | ✅ | ✅ |
| 冷却/重试 | ✅ | ✅ |
| 管理 API | ✅ `/v0/management/*` | ✅（子集）|
| WebSocket（Codex）| ✅ | 暂不支持 |
| OAuth 自动刷新 | ✅ | 暂不支持 |
| 直接 HTTP 调用（bypass CLI）| ✅ | Antigravity `/v1/messages` 已支持；OpenAI chat 仍可走 CLI |
| 真·流式 | ✅ | 模拟（CLI 同步后包装 SSE）|
| Docker | ✅ | `python -m proxy` |
