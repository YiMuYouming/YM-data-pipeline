"""审计回复 21 · 断点 B：Mac 只读同步 Hermes 的 market-facts 封存库。

夹具取生产形状（公共约定 10f）：HTTP 未就绪的 payload 逐字来自 2026-10-05 实抓的
`GET /api/live/market-facts`；库侧指纹用真的 `MarketFactStore` 造出来的 run。
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ym_stock_data import market_facts_sync as sync  # noqa: E402
from ym_stock_data import __main__ as cli  # noqa: E402
from ym_stock_data.__main__ import main  # noqa: E402
from ym_stock_data.market_facts import MarketFactStore  # noqa: E402
from tests.test_market_facts import limit_result as _limit_result_base, row as _row  # noqa: E402

DAY = "20260930"

# 2026-10-05 生产实抓（未就绪时）
LIVE_NOT_READY = {
    "data": None,
    "_meta": {
        "contract_version": "1.0",
        "intent": "market_facts",
        "status": "error",
        "provider_used": None,
        "source": None,
        "source_chain": ["market_facts"],
        "attempts": [{"provider": "market_facts", "status": "provider_error",
                      "error_code": "FACT_DAY_MISSING", "latency_ms": 0}],
        "fetched_at": "2026-10-05T01:07:22+08:00",
        "data_as_of": None,
    },
}


def _limit_result(day, codes):
    """生产形状的限价池夹具：复用 tests/test_market_facts 的 limit_result/row（10f）。"""
    return _limit_result_base(day, [_row(code, 2) for code in codes])


def _daily_result(day, n=4000):
    items = [
        {"ts_code": f"{300000 + i}.SZ", "trade_date": day, "open": 10.0, "high": 10.0,
         "low": 10.0, "close": 10.0, "pre_close": 10.0, "pct_chg": 0.0,
         "vol": 100.0, "amount": 1000.0}
        for i in range(n)
    ]
    return {
        "data": {"items": items, "truncated": False, "total_present": False},
        "_meta": {"status": "success", "provider_used": "stocktoday",
                  "fetched_at": f"{day[:4]}-{day[4:6]}-{day[6:]}T17:00:00+08:00"},
    }


def _sealed_db(path: Path, day=DAY) -> Path:
    store = MarketFactStore(path)
    store.ingest_limits(day, _limit_result(day, ["000001", "000002", "000003"]))
    store.ingest_daily(day, _daily_result(day))
    return path


class LivePayloadShapeTest(unittest.TestCase):
    def test_real_not_ready_payload_is_not_ready(self):
        status = sync.parse_live_payload(LIVE_NOT_READY, DAY)
        self.assertFalse(status["ready"])
        self.assertEqual("not_sealed", status["reason"])
        self.assertIn("FACT_DAY_MISSING", status["attempt_error_codes"])

    def test_sealed_payload_is_ready(self):
        payload = {
            "data": {"trade_date": DAY, "counts": {"up": 52}},
            "_meta": {"status": "success", "data_as_of": "2026-09-30T15:00:00+08:00"},
        }
        status = sync.parse_live_payload(payload, DAY)
        self.assertTrue(status["ready"])
        self.assertEqual("2026-09-30T15:00:00+08:00", status["data_as_of"])

    def test_other_day_is_not_ready(self):
        payload = {
            "data": {"trade_date": "20260929"},
            "_meta": {"status": "success"},
        }
        self.assertFalse(sync.parse_live_payload(payload, DAY)["ready"])


class WaitForSealTest(unittest.TestCase):
    def test_times_out_when_never_ready(self):
        calls = []

        def probe():
            calls.append(1)
            return {"ready": False, "reason": "not_sealed"}

        clock = {"t": 0.0}
        status = sync.wait_for_seal(
            DAY, probe=probe, wait_seconds=2.0, poll_seconds=1.0,
            sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
            now=lambda: clock["t"],
        )
        self.assertFalse(status["ready"])
        self.assertTrue(status["timed_out"])
        self.assertGreaterEqual(len(calls), 2, "等待期间要真的轮询")

    def test_zero_wait_probes_once(self):
        calls = []

        def probe():
            calls.append(1)
            return {"ready": True, "reason": "sealed"}

        status = sync.wait_for_seal(DAY, probe=probe, wait_seconds=0.0)
        self.assertTrue(status["ready"])
        self.assertEqual(1, len(calls))


class SyncSealedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote_db = _sealed_db(self.root / "hermes.sqlite3")
        self.target = self.root / "mac.sqlite3"
        self.target.write_bytes(b"old-local-db")

    def _ready_probe(self):
        fp = sync._fingerprint_local(self.remote_db, DAY)
        return lambda: {"ready": True, "reason": "sealed", "fingerprint": fp}

    def test_not_ready_leaves_target_alone(self):
        receipt = sync.sync_sealed(
            DAY, target_db=self.target,
            probe=lambda: {"ready": False, "reason": "not_sealed", "timed_out": True},
            fetch=lambda: b"never",
        )
        self.assertEqual("not_ready", receipt["status"])
        self.assertEqual("MARKET-FACTS-HERMES-NOT-SEALED-001", receipt["gap_code"])
        self.assertEqual(b"old-local-db", self.target.read_bytes())

    def test_pull_and_verify_replaces_target_with_backup(self):
        receipt = sync.sync_sealed(
            DAY, target_db=self.target,
            probe=self._ready_probe(),
            fetch=lambda: self.remote_db.read_bytes(),
        )
        self.assertEqual("pulled", receipt["status"], receipt)
        self.assertEqual(receipt["remote_fingerprint"], receipt["target_fingerprint"])
        self.assertTrue(Path(receipt["backup"]).is_file(), "旧库要先备份")
        self.assertEqual(b"old-local-db", Path(receipt["backup"]).read_bytes())
        # 替换后本地库真的能读出这一天的封存
        self.assertEqual(1, receipt["target_fingerprint"]["tables"]["limit_runs"]["runs"])
        self.assertEqual(1, receipt["target_fingerprint"]["tables"]["daily_runs"]["runs"])

    def test_fingerprint_mismatch_does_not_replace(self):
        stale = _sealed_db(self.root / "hermes-later.sqlite3", DAY)
        store = MarketFactStore(stale)
        store.ingest_limits(DAY, _limit_result(DAY, ["000009"]))  # 远程又追加了一轮
        receipt = sync.sync_sealed(
            DAY, target_db=self.target,
            probe=self._ready_probe(),          # 探针给的是"旧"指纹
            fetch=lambda: stale.read_bytes(),   # 拉回来的是"新"库
        )
        self.assertEqual("verify_failed", receipt["status"])
        self.assertEqual("MARKET-FACTS-PULL-FINGERPRINT-MISMATCH-001", receipt["gap_code"])
        self.assertEqual(b"old-local-db", self.target.read_bytes())

    def test_non_sqlite_blob_does_not_replace(self):
        receipt = sync.sync_sealed(
            DAY, target_db=self.target,
            probe=self._ready_probe(),
            fetch=lambda: b"<html>not a database</html>",
        )
        self.assertEqual("verify_failed", receipt["status"])
        self.assertEqual("MARKET-FACTS-PULL-NOT-SQLITE-001", receipt["gap_code"])
        self.assertEqual(b"old-local-db", self.target.read_bytes())

    def test_dry_run_does_not_touch_disk(self):
        receipt = sync.sync_sealed(
            DAY, target_db=self.target,
            probe=self._ready_probe(),
            fetch=lambda: (_ for _ in ()).throw(AssertionError("dry-run 不该拉整库")),
            dry_run=True,
        )
        self.assertEqual("would_pull", receipt["status"])
        self.assertEqual(b"old-local-db", self.target.read_bytes())

    def test_no_staging_file_is_left_behind(self):
        sync.sync_sealed(
            DAY, target_db=self.target,
            probe=self._ready_probe(),
            fetch=lambda: b"<html>not a database</html>",
        )
        leftovers = [p.name for p in self.root.iterdir() if ".incoming" in p.name]
        self.assertEqual([], leftovers)


class MacLocalSealRefusalTest(unittest.TestCase):
    """Mac 上 `market-facts refresh` 默认拒绝，报错指向 sync-sealed。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "mac.sqlite3"
        # daily leg 预置一条，让它像生产一样短路（否则同一份 mock 会被 ingest_daily 吃坏）
        MarketFactStore(self.db).ingest_daily(DAY, _daily_result(DAY))

    def _run(self, *extra, platform="darwin", env=None):
        out = io.StringIO()
        with mock.patch.object(sys, "platform", platform), \
             mock.patch.dict("os.environ", env or {}, clear=False), \
             mock.patch("ym_stock_data.__main__.canonical_query") as query, \
             redirect_stdout(out):
            query.return_value = _limit_result(DAY, ["000001"])
            code = main(["market-facts", "refresh", "--date", DAY, "--db", str(self.db), *extra])
        return code, out.getvalue()

    def test_refused_on_mac_without_flag(self):
        env = {k: v for k, v in os.environ.items() if k != sync.LOCAL_SEAL_ENV}
        before = self.db.read_bytes()
        with mock.patch.dict("os.environ", env, clear=True):
            code, text = self._run()
        self.assertEqual(3, code)
        payload = json.loads(text)
        self.assertEqual("local_market_facts_seal_disabled", payload["error"])
        self.assertIn("sync-sealed", payload["message"])
        self.assertEqual(before, self.db.read_bytes(), "被拒绝时不许改库")

    def test_allowed_with_explicit_flag(self):
        code, text = self._run("--allow-local-seal")
        self.assertEqual(0, code, text)
        self.assertNotIn("local_market_facts_seal_disabled", text)

    def test_linux_is_not_blocked(self):
        code, text = self._run(platform="linux")
        self.assertEqual(0, code, text)

    def test_env_escape_hatch(self):
        with mock.patch.dict("os.environ", {sync.LOCAL_SEAL_ENV: "1"}):
            code, text = self._run()
        self.assertEqual(0, code, text)

    def test_sync_sealed_is_not_a_write_command(self):
        self.assertNotIn("sync-sealed", cli._FACTS_WRITE_COMMANDS)
        self.assertIn("refresh", cli._FACTS_WRITE_COMMANDS)


class SyncSealedCliTest(unittest.TestCase):
    def test_dry_run_cli_reports_status_without_writing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = Path(tmp.name) / "mac.sqlite3"
        remote = _sealed_db(Path(tmp.name) / "hermes.sqlite3")
        out = io.StringIO()
        with mock.patch.object(sync, "seal_status",
                               return_value={"ready": True, "reason": "sealed",
                                             "fingerprint": sync._fingerprint_local(remote, DAY)}), \
             redirect_stdout(out):
            code = main(["market-facts", "sync-sealed", "--date", DAY,
                         "--db", str(target), "--dry-run"])
        self.assertEqual(0, code, out.getvalue())
        receipt = json.loads(out.getvalue())
        self.assertEqual("would_pull", receipt["status"])
        self.assertFalse(target.exists())

    def test_not_ready_cli_exits_nonzero_with_gap(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = Path(tmp.name) / "mac.sqlite3"
        out = io.StringIO()
        with mock.patch.object(sync, "seal_status",
                               return_value={"ready": False, "reason": "not_sealed",
                                             "timed_out": True}), redirect_stdout(out):
            code = main(["market-facts", "sync-sealed", "--date", DAY, "--db", str(target)])
        self.assertEqual(2, code)
        receipt = json.loads(out.getvalue())
        self.assertEqual("not_ready", receipt["status"])


if __name__ == "__main__":
    unittest.main()
