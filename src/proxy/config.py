"""cli_proxy 配置：二进制路径和超时，全部可由 .env 覆盖。"""
from __future__ import annotations

import os
from pathlib import Path


_DOTENV_LOADED = False


def load_project_env(path: Path | None = None) -> None:
    """加载当前工作目录的 .env，且不覆盖已存在的进程环境变量。"""
    global _DOTENV_LOADED
    if _DOTENV_LOADED and path is None:
        return

    env_path = path or (Path.cwd() / ".env")
    _DOTENV_LOADED = True
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        os.environ[key] = value


load_project_env()

DEFAULT_PORT = 8317
DEFAULT_HOST = "127.0.0.1"
DEFAULT_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"

CLI_TIMEOUT = int(os.environ.get("AGENT_LLM_CLI_TIMEOUT", "600"))

CLAUDE_BIN = os.environ.get("CLAUDE_CLI_BIN", "claude")
CODEX_BIN = os.environ.get("CODEX_CLI_BIN", "codex")
GROK_BIN = os.environ.get("GROK_CLI_BIN", "grok")
COPILOT_BIN = os.environ.get("COPILOT_CLI_BIN", "copilot")


def _default_antigravity_bin() -> str:
    configured = os.environ.get("ANTIGRAVITY_CLI_BIN", "").strip()
    if configured:
        return configured
    local_bin = Path.home() / ".local" / "bin" / "agy"
    return str(local_bin) if local_bin.exists() else "agy"


ANTIGRAVITY_BIN = _default_antigravity_bin()
