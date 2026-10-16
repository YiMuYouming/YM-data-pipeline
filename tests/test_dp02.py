"""DP-02: safe temporal semantics, empty/error attribution, and timing."""
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ym_stock_data import api, intraday_state
from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.provider_policy import CompiledPolicy
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.providers.local import LocalProvider
from ym_stock_data.providers.stocktoday import StockTodayProvider
from ym_stock_data.sources import eastmoney_index, ths_industry

NOW = datetime(2026, 10, 16, 10, 0, tzinfo=TZ_SHANGHAI)

class DP02Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = ProviderState(Path(self.tmp.name) / "providers.sqlite")
        clock = patch.object(api, "_now_shanghai", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def query(self, intent, params, provider, data, status="success"):
        outcome = ProviderOutcome(provider, status, data=data, fetched_at=NOW.isoformat(), latency_ms=12)
        return api._query_with(intent, params, provider_loader=lambda name: Mock(call=Mock(return_value=outcome)),
                               state_loader=lambda: self.state,
                               policy_loader=lambda: CompiledPolicy(None, "inactive", None))

    def test_sector_unverified_time_is_degraded_not_fetch_time(self):
        result = self.query("sector_index", {"names": ["文化传媒"]}, "ths_industry", {
            "items": [{"code": "881164", "name": "文化传媒", "change_pct": 1.0}], "missing": []})
        meta = result["_meta"]
        self.assertIsNone(meta["data_as_of"])
        self.assertEqual("unknown", meta["freshness"]["status"])
        self.assertEqual("degraded", meta["status"])
        self.assertIn("DATA_TIME_UNKNOWN", meta["quality"]["reason_codes"])

    def test_sector_exact_name_miss_remains_legal_empty(self):
        result = self.query("sector_index", {"names": ["出版"]}, "ths_industry", {
            "items": [], "missing": ["出版"]}, status="empty")
        self.assertEqual("empty", result["_meta"]["status"])
        self.assertEqual("EMPTY_UPSTREAM", result["_meta"]["attempts"][0]["error_code"])
        self.assertEqual(12, result["_meta"]["attempts"][0]["latency_ms"])

    def test_stale_daily_bars_explain_date_lag_without_inventing_suspension(self):
        bar = {"datetime": "2026-10-12 15:00:00", "open": 10, "high": 11,
               "low": 9, "close": 10, "volume": 100, "amount": 1000}
        result = self.query("stock_kline", {"code": "600825", "count": 1, "period": "daily"}, "stocktoday", {
            "bars": [bar], "adjustment": "none", "volume_unit": "share", "amount_unit": "CNY"})
        meta = result["_meta"]
        self.assertEqual("stale", meta["freshness"]["status"])
        self.assertEqual("bars_before_latest_completed_trade_date", meta["freshness"].get("reason"))
        self.assertEqual("20261015", meta["freshness"].get("expected_trade_date"))
        self.assertEqual("degraded", meta["status"])
        self.assertIn("BAR_TRADE_DATE_LAG", meta["quality"]["reason_codes"])
        self.assertNotIn("suspended", str(result).lower())

    def test_explicit_historical_bars_remain_historical(self):
        stamp = datetime(2026, 10, 12, 15, 0, tzinfo=TZ_SHANGHAI)
        result = api._data_freshness("stock_kline", {"end_date": "20261012"}, stamp, 86400)
        self.assertEqual("historical", result["status"])

    def test_compare_has_common_completed_bar_time(self):
        data = {"trade_date": "20261016", "period": "15m", "items": []}
        for n, code in enumerate(("000001.SH", "399001.SZ", "399006.SZ")):
            def bar(t):
                return {"t": t, "chg": 1.0, "amount": 100, "vol": 10,
                        "volRatio": 1.0, "yesterdayAmt": 90}
            data["items"].append({"index_code": code, "bars": [
                bar("09:45"), *([bar("10:00")] if n == 0 else []),
                {**bar("累计"), "_cum": True}]})
            data[("上证15min", "深证15min", "创业15min")[n]] = data["items"][-1]["bars"]
        result = self.query("index_intraday_compare", {"trade_date": "20261016"}, "tencent", data)
        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual("2026-10-16T09:45:00+08:00", result["_meta"]["data_as_of"])

    def test_opening_compare_before_first_closed_bar_is_legal_empty(self):
        class OpeningClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 16, 9, 30, 31, tzinfo=TZ_SHANGHAI)
        fetcher = Mock(return_value={"bars": []})
        with patch.object(eastmoney_index, "datetime", OpeningClock):
            data = eastmoney_index.build_index_intraday_compare(fetcher, source="tencent", trade_date="20261016")
        self.assertNotIn("error", data)
        self.assertEqual([], data["items"])
        self.assertEqual("NO_COMPLETED_INTRADAY_BARS", data.get("empty_reason"))
        fetcher.assert_not_called()

    def test_compare_time_requires_a_bar_shared_by_all_three_indices(self):
        data = {"trade_date": "20261016", "items": [
            {"bars": [{"t": "09:35"}, {"t": later}]} for later in ("09:40", "09:45", "09:50")]}
        self.assertEqual("2026-10-16T09:35:00+08:00",
                         api._data_as_of("index_intraday_compare", {"period": "5m"}, data).isoformat())

    def test_after_opening_missing_provider_is_still_error(self):
        class LaterClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 16, 10, 0, tzinfo=TZ_SHANGHAI)
        with patch.object(eastmoney_index, "datetime", LaterClock):
            result = eastmoney_index.build_index_intraday_compare(Mock(return_value={"error": "timeout", "error_type": "TIMEOUT"}), source="tencent", trade_date="20261016")
        self.assertEqual("INCOMPLETE_INDEX_COMPARE", result.get("error_type"))

    def test_early_compare_empty_reason_survives_public_boundary(self):
        result = self.query("index_intraday_compare", {"trade_date": "20261016"}, "tencent", {
            "trade_date": "20261016", "items": [], "empty_reason": "NO_COMPLETED_INTRADAY_BARS"}, status="empty")
        self.assertEqual("empty", result["_meta"]["status"])
        self.assertIn("NO_COMPLETED_INTRADAY_BARS", result["_meta"]["quality"]["reason_codes"])
        self.assertEqual("empty", result["_meta"]["quality"]["status"])
        self.assertEqual(0, result["_meta"]["quality"]["returned_count"])

    def test_limit_failure_latency_includes_previous_subcalls(self):
        provider = StockTodayProvider(clock=Mock(monotonic=Mock(side_effect=[10.0, 16.0])))
        failure = ProviderOutcome("stocktoday", "timeout", error_code="TIMEOUT", latency_ms=1000)
        with patch.object(provider, "_limit_target_date", return_value="20261016"), \
             patch.object(provider, "_collect_limit_pools", return_value=(None, failure)):
            result = provider._call_limit_state({"date": "20261016"})
        self.assertEqual(6000, result.latency_ms)
        self.assertEqual("TIMEOUT", result.error_code)

    def test_canonical_promotion_uses_normalized_previous_date(self):
        provider = StockTodayProvider()
        current = {"pools": {"zt": [{"code": "600001", "limit_days": 1}], "zb": [], "dt": []}}
        last = ProviderOutcome("stocktoday", "success", fetched_at=NOW.isoformat())
        provider._collect_limit_pools = Mock(side_effect=[(current, last), (current, last)])
        with patch("ym_stock_data.sources.limit_state.derive_limit_promotion", return_value={"verified": True}):
            result = api._query_with("market_limit_state", {"date": "20261016", "include_promotion": True},
                                     provider_loader=lambda name: provider, state_loader=lambda: self.state,
                                     policy_loader=lambda: CompiledPolicy(None, "inactive", None))
        self.assertEqual("20261015", provider._collect_limit_pools.call_args_list[-1].args[0])
        self.assertTrue((result["data"].get("promotion") or {}).get("verified"))

    def test_flow_parse_failure_is_not_legal_empty(self):
        with patch.object(ths_industry, "_fetch_all_industry_rows", return_value={"error": "unparsed", "error_type": "PARSE_FAILED"}):
            result = LocalProvider("ths_industry").call("industry_flow", {"current_session": True})
        self.assertEqual("provider_error", result.status)
        self.assertEqual("PARSE_FAILED", result.error_code)

    def test_current_flow_passes_explicit_current_date_upstream(self):
        clock = Mock(time=Mock(return_value=NOW.timestamp()))
        provider = StockTodayProvider(clock=clock)
        provider._request_table = Mock(return_value=ProviderOutcome("stocktoday", "empty", data={"items": []}))
        provider._call_flow(api_name="moneyflow_ind_ths", params={"current_session": True})
        self.assertEqual({"trade_date": "20261016"}, provider._request_table.call_args.args[1])

    def test_current_flow_does_not_invent_close_time(self):
        result = self.query("industry_flow", {"limit": 10, "use_case": "realtime_poll"}, "ths_industry", {
            "trade_date": "20261016", "items": [{"code": "881164", "net_inflow_yi": 1}]})
        self.assertIsNone(result["_meta"]["data_as_of"])
        self.assertIn("DATA_TIME_UNKNOWN", result["_meta"]["quality"]["reason_codes"])

    def test_limit_metadata_reports_missing_rows_without_filling_industry(self):
        provider = Mock()
        provider._request_table.return_value = ProviderOutcome("stocktoday", "empty", data={"items": []}, latency_ms=17)
        # Production build must expose the safe observation from the helper.
        diagnostics = {}
        try:
            rows = intraday_state._limit_metadata(provider, "20261016", diagnostics=diagnostics)
        except TypeError:
            self.fail("limit metadata must expose a safe diagnostic observation")
        self.assertEqual({}, rows)
        self.assertEqual("empty", diagnostics["status"])
        self.assertEqual("EMPTY_UPSTREAM", diagnostics["error_code"])
        self.assertEqual(17, diagnostics["latency_ms"])

    def test_intraday_public_observation_keeps_batch_and_metadata_details(self):
        from tests.test_intraday_state import FakeStockToday, row
        codes = ("600001.SH", "000001.SZ", "300001.SZ", "920001.BJ")
        provider = FakeStockToday({pattern: [row(code, 11, 10, updated="2026-10-16T10:00:00")]
                                  for pattern, code in zip(intraday_state.SNAPSHOT_BATCHES, codes)},
                                 {code: (11, 9) for code in codes})
        with patch.object(intraday_state, "STK_LIMIT_CACHE", Path(self.tmp.name) / "limits"), \
             patch.object(intraday_state, "_previous_ladder", return_value=({}, [], "verified")), \
             patch.object(intraday_state, "_sealed_break_risk", return_value={"value": None}):
            result = intraday_state.build(provider, now=NOW, quote_loader=lambda codes: {})
        observation = result["_stocktoday"]
        self.assertTrue(all(isinstance(batch.get("latency_ms"), int) for batch in observation["batches"]))
        self.assertEqual("EMPTY_UPSTREAM", observation.get("limit_metadata", {}).get("error_code"))
        self.assertIn("limit_up_metadata_empty", result["source_gaps"])
        self.assertTrue(all(not row.get("industry") for row in result["limit_up"]))
