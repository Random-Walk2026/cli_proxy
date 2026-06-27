"""入口：python -m proxy [--port 8317] [--host 127.0.0.1]

启动后任意 OpenAI-compatible 客户端指向 http://127.0.0.1:8317/v1 即可使用。

模型命名格式：<backend>/<model>@<effort>
  claude/sonnet@high   → Claude Code CLI，sonnet 模型，high effort
  codex/gpt-5          → Codex CLI，gpt-5 模型
  grok                 → Grok CLI，默认模型
  antigravity/gemini-3.5-flash-high  → Antigravity CLI
  copilot/gpt-4.1@medium             → GitHub Copilot CLI
"""
import argparse

import uvicorn

from .server import app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="cli_proxy API — 本地 OpenAI-compatible 代理，路由到订阅 CLI"
    )
    parser.add_argument("--port", type=int, default=8317, help="监听端口（默认 8317）")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1）")
    parser.add_argument("--reload", action="store_true", help="代码变更时自动重载（开发用）")
    args = parser.parse_args()

    print(f"cli_proxy API  →  http://{args.host}:{args.port}/v1")
    print(f"账号状态面板   →  http://{args.host}:{args.port}/")
    print(f"支持后端：claude / codex / grok / antigravity / copilot")
    print(f"模型格式：<backend>/<model>@<effort>，如 claude/sonnet@high")
    print()

    uvicorn.run(
        "proxy.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
