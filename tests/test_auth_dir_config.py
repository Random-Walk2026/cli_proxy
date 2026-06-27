"""认证目录配置加载行为。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_auth_dir(cwd: Path, home: Path, *, env_auth_dir: str | None = None) -> str:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["HOME"] = str(home)
    env.pop("CLI_PROXY_AUTH_DIR", None)
    if env_auth_dir is not None:
        env["CLI_PROXY_AUTH_DIR"] = env_auth_dir

    code = "from proxy.account import AUTH_DIR; print(AUTH_DIR)"
    res = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return res.stdout.strip()


class AuthDirConfig(unittest.TestCase):
    def test_auth_dir_from_environment_expands_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            got = _run_auth_dir(Path(tmp), home, env_auth_dir="~/.cli_proxy_api")
            self.assertEqual(got, str(home / ".cli_proxy_api"))

    def test_dotenv_auth_dir_is_loaded_before_account_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            (root / ".env").write_text(
                textwrap.dedent(
                    """
                    # local auth directory
                    CLI_PROXY_AUTH_DIR=~/.cli_proxy_api
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            got = _run_auth_dir(root, home)
            self.assertEqual(got, str(home / ".cli_proxy_api"))

    def test_environment_auth_dir_wins_over_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            (root / ".env").write_text(
                "CLI_PROXY_AUTH_DIR=~/.from-dotenv\n", encoding="utf-8"
            )

            got = _run_auth_dir(root, home, env_auth_dir="~/.from-env")
            self.assertEqual(got, str(home / ".from-env"))


if __name__ == "__main__":
    unittest.main()
