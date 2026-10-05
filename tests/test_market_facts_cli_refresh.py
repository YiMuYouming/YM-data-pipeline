"""`market-facts refresh --force-provider` appends one dated run instead of skipping.

审计回复 21 · 断点 B 起，Mac 上 refresh 默认拒绝（Hermes 是唯一封存者），所以这里的 CLI 调用都显式带 `--allow-local-seal`——本文件测的是 refresh 的
追加语义，不是平台闸（平台闸的用例在 tests/test_market_facts_sync_sealed.py）。

The 9-28 promotion rate needs a 9-24 limit pool from the same provider as 9-28
(StockToday): 9-24 was first collected from eastmoney and the store is
append-only, so the fix is an opt-in forced re-collect that appends a new run.
The default path must keep short-circuiting on `already_present`.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ym_stock_data.__main__ import main
from ym_stock_data.market_facts import MarketFactStore
from tests.test_market_facts import limit_result, row


def _stocktoday_result(day, up_rows):
    payload = limit_result(day, up_rows)
    payload["_meta"]["provider_used"] = "stocktoday"
    payload["_meta"]["fetched_at"] = "2026-09-28T21:00:00+08:00"
    return payload


class RefreshForceProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "facts.sqlite3"

    def _store_with_eastmoney_0924(self):
        store = MarketFactStore(self.db)
        store.ingest_limits(
            "20260924",
            limit_result("20260924", [row("000001", 2), row("000002", 1)]),
        )
        self._seed_daily(store, "20260924")
        return store

    def _seed_daily(self, store, day):
        """Pre-seed one daily run so the daily leg short-circuits like in production."""
        items = [
            {"ts_code": f"{code}.SZ", "trade_date": day, "open": 10.0, "high": 10.0, "low": 10.0,
             "close": 10.0, "pre_close": 10.0, "pct_chg": 0.0, "vol": 100.0, "amount": 1000.0}
            for code in (f"3{i:05d}" for i in range(4000))
        ]
        store.ingest_daily(day, {
            "data": {"items": items, "truncated": False, "total_present": False},
            "_meta": {"status": "success", "provider_used": "stocktoday",
                      "fetched_at": "2026-09-24T17:00:00+08:00"},
        })

    def _run_refresh(self, *extra):
        output = io.StringIO()
        with patch("ym_stock_data.__main__.canonical_query") as query, redirect_stdout(output):
            query.return_value = _stocktoday_result("20260924", [row("000001", 2), row("000002", 1)])
            code = main(["market-facts", "refresh", "--date", "20260924",
                         "--db", str(self.db), "--allow-local-seal", *extra])
        return code, json.loads(output.getvalue()), query

    def test_plain_refresh_still_short_circuits_on_existing_run(self):
        self._store_with_eastmoney_0924()
        code, receipt, query = self._run_refresh()
        self.assertEqual(0, code)
        self.assertEqual("already_present", receipt["limit_events"])
        query.assert_not_called()
        store = MarketFactStore(self.db)
        self.assertEqual("eastmoney_limit_pool", store.latest_limit_run("20260924")["provider"])
        with store._connect() as conn:
            self.assertEqual(
                1,
                conn.execute("SELECT COUNT(*) FROM limit_runs WHERE trade_date='20260924'").fetchone()[0],
            )

    def test_force_provider_appends_a_stocktoday_run_without_dropping_the_old_one(self):
        self._store_with_eastmoney_0924()
        code, receipt, query = self._run_refresh("--force-provider", "stocktoday")
        self.assertEqual(0, code)
        self.assertEqual("stocktoday", receipt.get("forced_provider"))
        self.assertIsInstance(receipt["limit_events"], dict)
        self.assertEqual("stocktoday", receipt["limit_events"]["provider"])
        query.assert_called_once_with("market_limit_state", date="20260924")

        store = MarketFactStore(self.db)
        latest = store.latest_limit_run("20260924")
        self.assertEqual("stocktoday", latest["provider"])
        self.assertEqual("post_close_observed", latest["phase"])
        with store._connect() as conn:
            runs = conn.execute(
                "SELECT provider FROM limit_runs WHERE trade_date='20260924' ORDER BY id"
            ).fetchall()
        self.assertEqual(
            ["eastmoney_limit_pool", "stocktoday"],
            [run[0] for run in runs],
            "append-only: the pre-existing eastmoney run must survive",
        )

    def test_force_provider_refuses_a_provider_the_route_does_not_serve(self):
        self._store_with_eastmoney_0924()
        output = io.StringIO()
        with patch("ym_stock_data.__main__.canonical_query") as query, redirect_stdout(output):
            code = main(["market-facts", "refresh", "--date", "20260924",
                         "--db", str(self.db), "--allow-local-seal",
                         "--force-provider", "eastmoney_limit_pool"])
        receipt = json.loads(output.getvalue())
        self.assertEqual(2, code)
        self.assertIn("force_provider_not_routable", receipt["gaps"][0]["error_code"])
        query.assert_not_called()
        store = MarketFactStore(self.db)
        with store._connect() as conn:
            self.assertEqual(
                1,
                conn.execute("SELECT COUNT(*) FROM limit_runs WHERE trade_date='20260924'").fetchone()[0],
            )

    def test_force_provider_records_a_gap_when_the_query_returns_another_provider(self):
        self._store_with_eastmoney_0924()
        mismatched = _stocktoday_result("20260924", [row("000001", 2)])
        mismatched["_meta"]["provider_used"] = "eastmoney_limit_pool"
        output = io.StringIO()
        with patch("ym_stock_data.__main__.canonical_query", return_value=mismatched), redirect_stdout(output):
            code = main(["market-facts", "refresh", "--date", "20260924",
                         "--db", str(self.db), "--allow-local-seal",
                         "--force-provider", "stocktoday"])
        receipt = json.loads(output.getvalue())
        self.assertEqual(2, code)
        self.assertEqual("force_provider_provider_mismatch", receipt["gaps"][0]["error_code"])
        store = MarketFactStore(self.db)
        with store._connect() as conn:
            self.assertEqual(
                1,
                conn.execute("SELECT COUNT(*) FROM limit_runs WHERE trade_date='20260924'").fetchone()[0],
            )


if __name__ == "__main__":
    unittest.main()
