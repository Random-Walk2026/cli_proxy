"""账号状态仪表盘：静态页面路由与字段。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from proxy.account import Account
from proxy.server import app


class DashboardRoute(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_root_serves_html(self) -> None:
        for path in ("/", "/dashboard"):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200)
            self.assertIn("text/html", res.headers["content-type"])
            self.assertIn("cli_proxy", res.text)
            # 页面靠这个接口取数据
            self.assertIn("/v0/management/accounts", res.text)


class ManagementActions(unittest.TestCase):
    def setUp(self) -> None:
        from proxy import pool as poolmod

        # 用内存账号替换全局池，避免依赖磁盘上的账号文件
        acc = Account("claude", "alice@x.com", token="t", source_path="")
        p = poolmod.AccountPool()
        p._accounts = {"claude": [acc]}
        p._index = {"claude": 0}
        p._loaded = True
        poolmod._pool = p
        self.acc = acc
        self.client = TestClient(app)

    def tearDown(self) -> None:
        from proxy import pool as poolmod

        poolmod._pool = None

    def _act(self, action: str, account_id: str = "alice@x.com"):
        return self.client.post(
            "/v0/management/accounts/action",
            json={"backend": "claude", "id": account_id, "action": action},
        )

    def test_disable_then_enable(self) -> None:
        self.assertEqual(self._act("disable").json()["account"]["status"], "disabled")
        self.assertTrue(self.acc.is_disabled)
        self.assertEqual(self._act("enable").json()["account"]["status"], "available")
        self.assertFalse(self.acc.is_disabled)

    def test_reset_clears_cooldown(self) -> None:
        self.acc.cool_down(60)
        self.assertTrue(self.acc.is_cooling)
        self.assertEqual(self._act("reset").status_code, 200)
        self.assertFalse(self.acc.is_cooling)

    def test_unknown_account_404(self) -> None:
        self.assertEqual(self._act("enable", "nobody@x.com").status_code, 404)

    def test_unknown_action_400(self) -> None:
        self.assertEqual(self._act("frobnicate").status_code, 400)


class AccountStatusFields(unittest.TestCase):
    def test_to_dict_status_transitions(self) -> None:
        acc = Account("claude", "a@x.com", token="sk-ant-token")
        self.assertEqual(acc.to_dict()["status"], "available")
        self.assertTrue(acc.to_dict()["available"])

        acc.cool_down(60)
        d = acc.to_dict()
        self.assertEqual(d["status"], "cooling")
        self.assertGreater(d["cooling_seconds"], 0)
        self.assertFalse(d["available"])

        acc.reset()
        acc.disable("invalid_grant: revoked")
        d = acc.to_dict()
        self.assertEqual(d["status"], "disabled")
        self.assertEqual(d["cooling_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
