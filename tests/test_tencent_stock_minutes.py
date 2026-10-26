"""Tencent stock minute regression: newest-first days, date windows and shares.

The minute rows use the observed 688018 wire format (cumulative shares/CNY).
Only HTTP is replaced; public routing, normalization and quality checks run.

2026-10-26（T4）：普通股票分钟 K 改为腾讯直接做主源，默认路由里不再出现
`stk_mins`；同一轮把分时均价（bar 内 vwap 与当日累计 avg_price）作为分钟 bar
字段暴露。日/周/月线、实时报价与显式 `rt_min` 不在本文件覆盖范围。
"""

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from ym_stock_data import api, query
from ym_stock_data.provider_policy import CompiledPolicy
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.providers.local import LocalProvider
from ym_stock_data.sources import pytdx


PAYLOAD = {
    "code": 0,
    "data": {"sh688018": {"data": [
        {"date": "20261026", "data": [
            "0930 151.50 163468 24765402.00",
            "0931 152.00 200000 30318300.00",
            "0935 150.00 300000 45318300.00",
            "0940 148.00 400000 60118300.00",
        ]},
        {"date": "20261023", "data": [
            "1455 154.00 17000000 2478524300.00",
            "1500 154.36 17472695 2551454100.00",
        ]},
        {"date": "20261020", "data": [
            "1455 140.00 18000000 2409778880.00",
            "1500 140.72 18367292 2457680361.00",
        ]},
    ]}},
}

DAILY_PAYLOAD = {
    "code": 0,
    "data": {"sh688018": {"day": [
        ["2026-10-23", "145.00", "154.36", "155.00", "144.00", "17472695"],
        ["2026-10-20", "140.00", "140.72", "141.00", "139.50", "18367292"],
    ]}},
}


class _RefusingPrimary:
    """A stocktoday adapter that must not be reached on the default minute route."""

    def call(self, intent, params):
        return ProviderOutcome("stocktoday", "incompatible", error_code="PLAN_NOT_ENTITLED")


class TencentStockMinuteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = ProviderState(Path(temp.name) / "state.sqlite3")
        self.provider_calls = []

        def loader(name):
            self.provider_calls.append(name)
            if name == "stocktoday":
                return _RefusingPrimary()
            return LocalProvider(name)

        for target, value in [
            ("_STATE", state),
            ("load_compiled_policy", lambda: CompiledPolicy(None, "inactive", None)),
            ("_now_shanghai", lambda: datetime(2026, 10, 26, 10, 5, tzinfo=timezone(timedelta(hours=8)))),
            ("_provider_for", loader),
        ]:
            patched = patch.object(api, target, value)
            patched.start()
            self.addCleanup(patched.stop)

    def _query(self, payload=None, **params):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(payload or PAYLOAD).encode()
        with patch("ym_stock_data.sources.pytdx.urllib.request.urlopen", return_value=response):
            return query("stock_kline", code="688018", period=params.pop("period", "5m"), **params)

    # --- 本机 10-24 已修的日期/单位/时间标记（8 项回归，T4 沿用） ---

    def test_recent_count_selects_newest_bars_from_descending_days(self):
        result = self._query(count=1)
        self.assertEqual("2026-10-26 09:40:00", result["data"]["bars"][0]["datetime"])

    def test_today_window_passes_public_quality_gate_without_old_bars(self):
        result = self._query(start_date="20261026", end_date="20261026", count=20)
        self.assertEqual("tencent", result["_meta"]["provider_used"])
        bars = result["data"]["bars"]
        self.assertEqual(3, len(bars))
        self.assertTrue(all(bar["datetime"].startswith("2026-10-26") for bar in bars))
        self.assertEqual(sorted(bar["datetime"] for bar in bars), [bar["datetime"] for bar in bars])
        self.assertEqual(["tencent"], [attempt["provider"] for attempt in result["_meta"]["attempts"]])
        self.assertIsNone(result["_meta"]["attempts"][0]["error_code"])

    def test_historical_window_is_filtered_before_count(self):
        result = self._query(start_date="20261023", end_date="20261023", count=1)
        self.assertIsNotNone(result["data"])
        self.assertEqual("2026-10-23 15:00:00", result["data"]["bars"][0]["datetime"])

    def test_cumulative_stock_volume_is_already_in_shares(self):
        result = self._query(count=20)
        bars = result["data"]["bars"]
        day23 = [b for b in bars if b["datetime"].startswith("2026-10-23")]
        self.assertEqual(17_472_695, sum(b["volume"] for b in day23))
        self.assertEqual(2_551_454_100, sum(b["amount"] for b in day23))
        self.assertEqual("share", result["data"]["volume_unit"])
        self.assertEqual("CNY", result["data"]["amount_unit"])

    def test_day_and_minute_order_do_not_change_selected_bar(self):
        payload = copy.deepcopy(PAYLOAD)
        payload["data"]["sh688018"]["data"].reverse()
        payload["data"]["sh688018"]["data"][-1]["data"].reverse()
        result = self._query(payload, count=20)
        expected = self._query(count=20)
        self.assertEqual(expected["data"]["bars"], result["data"]["bars"])

    def test_absent_requested_day_does_not_fall_back_to_other_day(self):
        result = self._query(start_date="20261022", end_date="20261022", count=20)
        self.assertEqual("empty", result["_meta"]["status"])
        self.assertEqual([], result["data"]["bars"])
        self.assertEqual("EMPTY_UPSTREAM", result["_meta"]["attempts"][-1]["error_code"])
        self.assertIsNone(result["_meta"]["data_as_of"])

    def test_minute_data_as_of_keeps_the_actual_bar_time(self):
        result = self._query(start_date="20261026", end_date="20261026", count=20)
        self.assertEqual("2026-10-26T09:40:00+08:00", result["_meta"]["data_as_of"])

    def test_date_selection_and_shares_hold_for_each_minute_period(self):
        for period in ("1m", "5m", "15m", "60m"):
            with self.subTest(period=period):
                result = self._query(period=period, start_date="20261026", end_date="20261026", count=20)
                self.assertEqual("tencent", result["_meta"]["provider_used"])
                self.assertEqual(400_000, sum(b["volume"] for b in result["data"]["bars"]))

    # --- T4：腾讯直接做主源（不再先试 stk_mins） ---

    def test_default_minute_route_is_tencent_only(self):
        from ym_stock_data.routing import route_for

        for period in ("1m", "5m", "15m", "60m"):
            with self.subTest(period=period):
                self.assertEqual(("tencent",), route_for("stock_kline", {"period": period}).providers)
        # 日线不变
        self.assertEqual(
            ("stocktoday", "tencent"),
            route_for("stock_kline", {"period": "daily"}).providers,
        )
        # 显式铸剑证据用途不变（pytdx 后备仍在）
        self.assertEqual(
            ("stocktoday", "pytdx", "tencent"),
            route_for("stock_kline", {"period": "5m", "use_case": "zj_replay"}).providers,
        )

    def test_minute_query_reports_tencent_primary_without_stk_mins_attempt(self):
        for period in ("1m", "5m", "15m", "60m"):
            with self.subTest(period=period):
                self.provider_calls.clear()
                result = self._query(
                    period=period, start_date="20261026", end_date="20261026", count=20
                )
                meta = result["_meta"]
                self.assertEqual("tencent", meta["provider_used"])
                self.assertEqual("primary", meta["source_tier"])
                self.assertEqual("success", meta["status"])
                self.assertEqual(["tencent"], [attempt["provider"] for attempt in meta["attempts"]])
                self.assertEqual(["tencent"], self.provider_calls)
                attempt_blob = json.dumps(meta["attempts"], ensure_ascii=False)
                self.assertNotIn("stk_mins", attempt_blob)
                self.assertNotIn("stocktoday", attempt_blob)
                self.assertNotIn("PLAN_NOT_ENTITLED", attempt_blob)
                self.assertNotIn("fallback_source", meta["quality"]["reason_codes"])
                last_bar = result["data"]["bars"][-1]["datetime"].replace(" ", "T")
                self.assertEqual(f"{last_bar}+08:00", meta["data_as_of"])
                self.assertTrue(meta["data_as_of"].startswith("2026-10-26T09:"))
                self.assertEqual("share", result["data"]["volume_unit"])

    # --- T4：分时均价（VWAP）字段 ---

    def test_minute_bars_expose_bar_vwap_and_session_average(self):
        result = self._query(start_date="20261026", end_date="20261026", count=20)
        bars = result["data"]["bars"]
        self.assertEqual(["2026-10-26 09:30:00", "2026-10-26 09:35:00", "2026-10-26 09:40:00"],
                         [bar["datetime"] for bar in bars])
        for bar in bars:
            self.assertIn("vwap", bar)
            self.assertIn("avg_price", bar)
            self.assertAlmostEqual(bar["vwap"], bar["amount"] / bar["volume"], places=4)
        # 09:30 桶：两行累计量价差分 200000 股 / 30318300 元
        self.assertAlmostEqual(151.5915, bars[0]["vwap"], places=4)
        # 09:35 桶：300000-200000 股、45318300-30318300 元
        self.assertAlmostEqual(150.0, bars[1]["vwap"], places=4)
        # 分时均价 = 当日从开盘起的累计成交额/累计成交量
        self.assertAlmostEqual(151.5915, bars[0]["avg_price"], places=4)
        self.assertAlmostEqual(45318300 / 300000, bars[1]["avg_price"], places=4)
        self.assertAlmostEqual(60118300 / 400000, bars[2]["avg_price"], places=4)

    def test_session_average_uses_day_cumulative_even_when_payload_starts_midday(self):
        result = self._query(start_date="20261023", end_date="20261023", count=20)
        bars = result["data"]["bars"]
        self.assertEqual(["2026-10-23 14:55:00", "2026-10-23 15:00:00"],
                         [bar["datetime"] for bar in bars])
        # 上游给的是当日累计量额，14:55 桶起点不是 0，均价仍按当日累计算
        self.assertAlmostEqual(2478524300 / 17000000, bars[0]["avg_price"], places=4)
        self.assertAlmostEqual(2551454100 / 17472695, bars[1]["avg_price"], places=4)
        for bar in bars:
            self.assertAlmostEqual(bar["vwap"], bar["amount"] / bar["volume"], places=4)

    def test_daily_bars_do_not_gain_minute_vwap_fields(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(DAILY_PAYLOAD).encode()
        with patch("ym_stock_data.sources.pytdx.urllib.request.urlopen", return_value=response):
            bars = pytdx._fetch_tencent_kline("688018", period="daily", count=2)
        self.assertTrue(bars)
        for bar in bars:
            self.assertNotIn("vwap", bar)
            self.assertNotIn("avg_price", bar)


if __name__ == "__main__":
    unittest.main()
