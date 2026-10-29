"""H20 #9（含 10-28 #5）：个股快照滞后与集合竞价快照过期。
- 保留下来的快照行带 lag_sec（取数时刻减去行情时点，午休不计），超过 180 秒的仍走既有的腾讯回退；
- 盘中（含 09:15 起的集合竞价）实时类数据新鲜度是 stale（例如指数停在上一交易日 15:00）时，
  信封不能再标 success，必须降级并带 DATA_STALE，不让下游把过期行情当 ok。"""
import unittest
from datetime import datetime
from unittest.mock import patch

from ym_stock_data import api
from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.providers.base import ProviderOutcome

DAY = "2026-10-29"


def at(hms):
    return datetime.fromisoformat(f"{DAY}T{hms}").replace(tzinfo=TZ_SHANGHAI)


def row(code, quote_time, price=14.0):
    return {"code": code, "price": price, "last_close": 13.1, "open": 12.82, "high": 14.81, "low": 12.7,
            "volume": 100.0, "amount": 1000.0, "quote_time": f"{DAY}T{quote_time}+08:00"}


class RowLag(unittest.TestCase):
    def fill(self, rows, now="13:07:00", tencent=None):
        loader = lambda name: type("P", (), {"call": lambda self, intent, params: ProviderOutcome(
            "tencent", "success", data=tencent or {})})()
        with patch("ym_stock_data.api._now_shanghai", return_value=at(now)):
            return api._fill_snapshot_codes({"codes": list(rows)}, dict(rows), loader, 600)

    def test_kept_rows_carry_lag_seconds(self):
        data = {"300045": row("300045", "13:05:10")}
        with patch("ym_stock_data.api._now_shanghai", return_value=at("13:07:00")):
            api._fill_snapshot_codes({"codes": ["300045"]}, data, lambda name: None, 600)
        self.assertEqual(110, data["300045"]["lag_sec"])

    def test_lunch_break_is_not_counted_as_lag(self):
        data = {"300045": row("300045", "11:29:50")}
        with patch("ym_stock_data.api._now_shanghai", return_value=at("13:00:20")):
            api._fill_snapshot_codes({"codes": ["300045"]}, data, lambda name: None, 600)
        self.assertLess(data["300045"]["lag_sec"], 60)

    def test_rows_older_than_180s_still_fall_back_to_tencent(self):
        data = {"300045": row("300045", "13:03:52", price=14.26)}
        fresh = row("300045", "13:06:55", price=14.10)
        loader = lambda name: type("P", (), {"call": lambda self, intent, params: ProviderOutcome(
            "tencent", "success", data={"300045": fresh})})()
        with patch("ym_stock_data.api._now_shanghai", return_value=at("13:07:00")):
            report = api._fill_snapshot_codes({"codes": ["300045"]}, data, loader, 600)
        self.assertEqual(["300045"], report["filled"])
        self.assertEqual("tencent", data["300045"]["source"])
        self.assertEqual(14.10, data["300045"]["price"])


class StaleDegrades(unittest.TestCase):
    def meta(self, freshness_status, status="success", quality="normal"):
        return {"status": status, "freshness": {"status": freshness_status},
                "quality": {"status": quality, "reason_codes": []}}

    def test_stale_realtime_snapshot_is_degraded(self):
        meta = self.meta("stale")
        api._degrade_stale_realtime(meta, "stock_snapshot")
        self.assertEqual("degraded", meta["status"])
        self.assertEqual("semantic_degraded", meta["quality"]["status"])
        self.assertIn("DATA_STALE", meta["quality"]["reason_codes"])

    def test_realtime_market_in_the_auction_is_covered_too(self):
        meta = self.meta("stale")
        api._degrade_stale_realtime(meta, "realtime_market")
        self.assertEqual("degraded", meta["status"])

    def test_fresh_and_aging_are_untouched(self):
        for state in ("fresh", "aging"):
            meta = self.meta(state)
            api._degrade_stale_realtime(meta, "stock_snapshot")
            self.assertEqual("success", meta["status"])
            self.assertEqual([], meta["quality"]["reason_codes"])

    def test_non_realtime_intents_and_errors_are_untouched(self):
        meta = self.meta("stale")
        api._degrade_stale_realtime(meta, "stock_kline")
        self.assertEqual("success", meta["status"])
        meta = self.meta("stale", status="error")
        api._degrade_stale_realtime(meta, "stock_snapshot")
        self.assertEqual("error", meta["status"])

    def test_existing_reason_is_not_duplicated(self):
        meta = self.meta("stale")
        meta["quality"]["reason_codes"].append("DATA_STALE")
        api._degrade_stale_realtime(meta, "stock_snapshot")
        self.assertEqual(["DATA_STALE"], meta["quality"]["reason_codes"])


if __name__ == "__main__":
    unittest.main()
