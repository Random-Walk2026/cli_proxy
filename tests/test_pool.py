"""账号池：永久禁用 / 主动刷新 / 落盘 行为测试。

覆盖移植自 cockpit-tools 的三项能力：
  ① 认证失效 → 永久禁用 + 落盘；配额/瞬时 → 仅冷却；半开探测自动恢复
  ② expiry 入模型 + needs_refresh 提前刷新
  ③ 刷新/禁用状态写回 registry JSON
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proxy import pool as poolmod
from proxy.account import Account, _account_from_file, _parse_expiry_ts


def _write_account(dir_path: Path, **overrides) -> Path:
    data = {
        "type": "claude",
        "email": "a@example.com",
        "token": "sk-abc12345",
        "refresh_token": "r1",
        "enabled": True,
    }
    data.update(overrides)
    path = dir_path / "claude_work.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class AuthFailureDisablesPermanently(unittest.TestCase):
    def test_invalid_grant_disables_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_account(Path(tmp))
            acc = _account_from_file(path)
            pool = poolmod.AccountPool()

            pool.mark_failed(acc, RuntimeError("HTTP 400 invalid_grant: token revoked"))

            self.assertTrue(acc.is_disabled)
            self.assertTrue(acc.is_invalid_grant_disabled)
            saved = json.loads(path.read_text())
            self.assertIn("disabled_reason", saved)
            self.assertFalse(saved["enabled"])

            # 重新从磁盘加载仍是禁用态
            reloaded = _account_from_file(path)
            self.assertTrue(reloaded.is_disabled)
            self.assertFalse(reloaded.enabled)

    def test_quota_error_only_cools_down(self) -> None:
        acc = Account("claude", "q", token="t")
        poolmod.AccountPool().mark_failed(
            acc, RuntimeError("429 too many requests quota exceeded")
        )
        self.assertFalse(acc.is_disabled)
        self.assertTrue(acc.is_cooling)

    def test_transient_error_only_cools_down(self) -> None:
        acc = Account("claude", "t", token="t")
        poolmod.AccountPool().mark_failed(acc, RuntimeError("connection timeout 503"))
        self.assertFalse(acc.is_disabled)
        self.assertTrue(acc.is_cooling)


class LoadIncludesDisabled(unittest.TestCase):
    def test_disabled_account_still_loaded(self) -> None:
        """禁用账号也要入池：否则半开探测无法在重启后复活，状态面板也看不到它。"""
        from proxy import account as accountmod

        with tempfile.TemporaryDirectory() as tmp:
            _write_account(
                Path(tmp),
                enabled=False,
                disabled_reason="invalid_grant: old",
                disabled_at=time.time(),
            )
            orig = accountmod.AUTH_DIR
            accountmod.AUTH_DIR = Path(tmp)
            try:
                loaded = accountmod.load_accounts("claude")
            finally:
                accountmod.AUTH_DIR = orig
            self.assertEqual(len(loaded), 1)
            self.assertTrue(loaded[0].is_disabled)
            self.assertEqual(loaded[0].status, "disabled")


class HalfOpenRecovery(unittest.TestCase):
    def test_disabled_account_probed_then_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_account(
                Path(tmp),
                enabled=False,
                disabled_reason="invalid_grant: old",
                disabled_at=time.time() - poolmod.RECOVERY_PROBE_AFTER - 1,
            )
            acc = _account_from_file(path)
            pool = poolmod.AccountPool()
            pool._accounts = {"claude": [acc]}
            pool._index = {"claude": 0}
            pool._loaded = True

            # 正常账号都不可用 → 半开探测放行这个禁用账号
            probed = pool.pick("claude")
            self.assertIs(probed, acc)

            # 探测成功 → 自动解禁并落盘
            pool.mark_success(probed)
            self.assertFalse(acc.is_disabled)
            self.assertNotIn("disabled_reason", json.loads(path.read_text()))

    def test_recently_disabled_not_probed(self) -> None:
        acc = Account("claude", "r", token="t")
        acc.disable("invalid_grant: fresh")  # disabled_at = now
        pool = poolmod.AccountPool()
        pool._accounts = {"claude": [acc]}
        pool._index = {"claude": 0}
        pool._loaded = True
        self.assertIsNone(pool.pick("claude"))


class ExpiryAndRefresh(unittest.TestCase):
    def test_parse_expiry_iso_and_epoch(self) -> None:
        self.assertGreater(_parse_expiry_ts("2030-01-01T00:00:00Z"), 0)
        self.assertEqual(_parse_expiry_ts(1893456000.0), 1893456000.0)
        self.assertEqual(_parse_expiry_ts(""), 0.0)
        self.assertEqual(_parse_expiry_ts(None), 0.0)

    def test_needs_refresh_respects_skew(self) -> None:
        acc = Account("antigravity", "e", expiry=time.time() + 100)
        self.assertTrue(acc.needs_refresh(skew_seconds=300))
        self.assertFalse(acc.needs_refresh(skew_seconds=10))
        # 不追踪 expiry 的后端永不刷新
        self.assertFalse(Account("claude", "c", token="t").needs_refresh())

    def test_persist_writes_expiry_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_account(Path(tmp))
            acc = _account_from_file(path)
            acc.expiry = 1893456000.0
            acc.persist()
            self.assertIn("expiry", json.loads(path.read_text()))


class PriorityWeightRouting(unittest.TestCase):
    def _pool(self, accounts: list[Account]) -> poolmod.AccountPool:
        pool = poolmod.AccountPool()
        pool._accounts = {"claude": list(accounts)}
        pool._index = {"claude": 0}
        pool._loaded = True
        return pool

    def test_primary_group_served_before_backup(self) -> None:
        primary = Account("claude", "primary", token="t", priority=0)
        backup = Account("claude", "backup", token="t", priority=1)
        pool = self._pool([primary, backup])
        # 主号可用时，连续多次都只命中主号
        picks = {pool.pick("claude").id for _ in range(10)}
        self.assertEqual(picks, {"primary"})

    def test_spillover_to_backup_when_primary_unavailable(self) -> None:
        primary = Account("claude", "primary", token="t", priority=0)
        backup = Account("claude", "backup", token="t", priority=1)
        pool = self._pool([primary, backup])
        primary.cool_down(60)  # 主号冷却
        self.assertEqual(pool.pick("claude").id, "backup")
        primary.reset()  # 主号恢复 → 重新优先主号
        self.assertEqual(pool.pick("claude").id, "primary")

    def test_weighted_distribution_within_group(self) -> None:
        heavy = Account("claude", "heavy", token="t", priority=0, weight=3)
        light = Account("claude", "light", token="t", priority=0, weight=1)
        pool = self._pool([heavy, light])
        counts = {"heavy": 0, "light": 0}
        for _ in range(40):  # 4 的整数倍，weight 3:1 → 30:10
            counts[pool.pick("claude").id] += 1
        self.assertEqual(counts["heavy"], 30)
        self.assertEqual(counts["light"], 10)

    def test_defaults_are_plain_round_robin(self) -> None:
        a = Account("claude", "a", token="t")
        b = Account("claude", "b", token="t")
        pool = self._pool([a, b])
        seq = [pool.pick("claude").id for _ in range(4)]
        self.assertEqual(seq, ["a", "b", "a", "b"])


if __name__ == "__main__":
    unittest.main()
