"""DP-01 offline regressions; HTTP responses are self-contained fixtures."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ym_stock_data import query
from ym_stock_data import api
from ym_stock_data.contracts import ProviderAttempt, build_result
from ym_stock_data.provider_policy import CompiledPolicy
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.providers.local import LocalProvider
from ym_stock_data.providers.stocktoday import StockTodayProvider
from ym_stock_data.sources import ths_industry, pytdx
from ym_stock_data.intraday_state import IntradayStateError
from tests.fixed_clock import FIXED_NOW_ISO, freeze_trading_clock


class DP01Tests(unittest.TestCase):
    def setUp(self):
        freeze_trading_clock(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = ProviderState(Path(self.tmp.name) / "state.sqlite")

    def run_query(self, intent, params, outcomes):
        providers = {name: Mock(call=Mock(return_value=outcome)) for name, outcome in outcomes.items()}
        return api._query_with(intent, params, provider_loader=providers.__getitem__,
                               state_loader=lambda: self.state,
                               policy_loader=lambda: CompiledPolicy(None, "inactive", None))

    def test_D1_suffix_matches_bare_industry_code(self):
        with patch.object(ths_industry, "_fetch_all_industry_rows", return_value=[
            {"code": "881121", "name": "半导体", "change_pct": 1.0}]), \
             patch.object(api, "_provider_for", return_value=LocalProvider("ths_industry")), \
             patch.object(api, "_provider_state", return_value=self.state), \
             patch.object(api, "load_compiled_policy", return_value=CompiledPolicy(None, "inactive", None)):
            result = query("sector_index", codes=["881121.TI"])
        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual(["881121"], [row["code"] for row in result["data"]["items"]])

    def test_D2_board_strength_gaps_degrade_envelope(self):
        gap = {"gap_code": "board_member_coverage_below_floor:None<0.8"}
        outcome = ProviderOutcome("board_strength", "success", data={
            "boards": [{"board_id": "881121", "fields": {}}], "source_gaps": [gap]})
        result = self.run_query("market_board_strength", {"trade_date": "20261014", "board_ids": ["881121"]}, {"board_strength": outcome})
        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertIn(gap["gap_code"], result["_meta"]["quality"]["reason_codes"])
        self.assertEqual([gap], result["_meta"]["quality"]["gaps"])

    def test_D3_legal_empty_has_stable_attempt_code(self):
        result = self.run_query("sector_index", {"names": ["不存在"]}, {
            "ths_industry": ProviderOutcome("ths_industry", "empty", data={"items": [], "missing": ["不存在"]})})
        self.assertEqual("empty", result["_meta"]["status"])
        self.assertEqual("EMPTY_UPSTREAM", result["_meta"]["attempts"][0]["error_code"])

    def test_D3_builder_fills_missing_failure_code(self):
        result = build_result(intent="filings", data=None, status="error", provider_used=None,
                              attempts=[ProviderAttempt("cninfo", "dependency_missing", None, 1)],
                              data_scope="stock", trade_usage="observation_only", max_age_sec=300,
                              quality={"status": "error", "returned_count": 0, "reason_codes": []})
        self.assertEqual("DEPENDENCY_MISSING", result["_meta"]["attempts"][0]["error_code"])

    def test_D4_fallback_retry_reports_previous_failure(self):
        result = build_result(intent="stock_event", data={"items": [{}]}, status="degraded", provider_used="eastmoney_datacenter",
                              attempts=[ProviderAttempt("stocktoday", "timeout", "TIMEOUT", 1), ProviderAttempt("eastmoney_datacenter", "success", None, 1)],
                              data_scope="stock", trade_usage="observation_only", max_age_sec=300,
                              quality={"status": "normal", "returned_count": 1, "reason_codes": []})
        self.assertIn("TIMEOUT", result["_meta"]["quality"]["reason_codes"])

    def test_D3_http_or_unparseable_page_is_failure(self):
        for http_status, html, code in [(403, "denied", "HTTP_403"), (200, "<html>challenge</html>", "PARSE_FAILED")]:
            with self.subTest(http_status=http_status), patch.object(ths_industry.requests, "get", return_value=Mock(status_code=http_status, text=html)):
                outcome = LocalProvider("ths_industry").call("sector_index", {"codes": ["881121"]})
            self.assertEqual("provider_error", outcome.status)
            self.assertEqual(code, outcome.error_code)

    def test_D4_degraded_observations_always_explain_reason(self):
        for observation, reason in [({"status": "unknown"}, "DATA_TIME_UNKNOWN"),
                                    ({"status": "stale"}, "DATA_STALE"),
                                    ({"truncated": True}, "UPSTREAM_TRUNCATED"),
                                    ({"duplicate_code_count": 1}, "DUPLICATE_CODES")]:
            with self.subTest(observation=observation):
                result = self.run_query("stocktoday_data", {"api_name": "ths_daily", "params": {"ts_code": "881121.TI"}}, {
                    "stocktoday": ProviderOutcome("stocktoday", "success", data={"items": [{"close": 1}], "_stocktoday": observation})})
                self.assertEqual("degraded", result["_meta"]["status"])
                self.assertIn(reason, result["_meta"]["quality"]["reason_codes"])

    def test_D5_failed_snapshot_empty_preserves_specific_code(self):
        provider = StockTodayProvider()
        with patch("ym_stock_data.intraday_state.build", side_effect=IntradayStateError("SNAPSHOT_BATCH_EMPTY", "empty")):
            result = self.run_query("market_intraday_state", {}, {"stocktoday": provider._call_intraday_state("market_intraday_state")})
        self.assertEqual("error", result["_meta"]["status"])
        self.assertEqual("SNAPSHOT_BATCH_EMPTY", result["_meta"]["attempts"][0]["error_code"])

    def test_D6_empty_board_members_is_legal_empty(self):
        result = self.run_query("board_members", {"codes": ["881121.TI"]}, {
            "stocktoday": ProviderOutcome("stocktoday", "empty", data={"boards": [], "missing": ["881121.TI"]})})
        self.assertEqual("empty", result["_meta"]["status"])
        self.assertEqual("EMPTY_UPSTREAM", result["_meta"]["attempts"][0]["error_code"])

    def test_D5_limit_latency_counts_entire_composite(self):
        provider = StockTodayProvider(clock=Mock(monotonic=Mock(side_effect=[10.0, 15.0])))
        last = ProviderOutcome("stocktoday", "empty", latency_ms=400)
        with patch.object(provider, "_limit_target_date", return_value="20261014"), \
             patch.object(provider, "_collect_limit_pools", return_value=({}, last)):
            outcome = provider._call_limit_state({"date": "20261014"})
        self.assertEqual(5000, outcome.latency_ms)

    def test_D9_volume_and_average_price_are_not_index_points(self):
        html = (Path(__file__).resolve().parents[1] / "tests/fixtures/dp01-ths-industry.html").read_text()
        row = ths_industry._parse_industry_rows(html)[0]
        self.assertIsNone(row["latest"])
        self.assertEqual(910.64, row.get("volume_wan_lots"))
        self.assertEqual(8.59, row.get("avg_price"))
        self.assertIsNone(row["flat_count"])

    def test_D7_all_failed_kline_reasons_are_preserved(self):
        bar = {"datetime": "2026-09-22 15:00:00", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 1}
        result = self.run_query("stock_kline", {"code": "301041", "count": 1, "adjustment": "qfq"}, {
            "stocktoday": ProviderOutcome("stocktoday", "provider_error", error_code="INVALID_RESPONSE"),
            "tencent": ProviderOutcome("tencent", "success", data={"bars": [bar], "adjustment": "qfq", "volume_unit": "share", "amount_unit": "CNY"})})
        self.assertEqual("error", result["_meta"]["status"])
        self.assertEqual(["INVALID_RESPONSE", "QUALITY_KLINE_FIELDS"], [a["error_code"] for a in result["_meta"]["attempts"]])

    def test_D10_five_indices_keep_turnover_total_scoped(self):
        symbols = {"sh000001": "上证指数", "sz399001": "深证指数", "sz399006": "创业指数",
                   "sh000688": "科创50", "sh000016": "上证50"}
        lines = []
        for symbol, name in symbols.items():
            values = ["0"] * 53
            values[1], values[3], values[4], values[30], values[32], values[33], values[34], values[37] = name, "100", "99", "20260922150000", "1.01", "101", "98", "10000"
            lines.append(f'v_{symbol}="' + "~".join(values) + '";')
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = "".join(lines).encode("gbk")
        with patch.object(pytdx.urllib.request, "urlopen", return_value=response) as http:
            data = pytdx._fallback_index_tencent()
        self.assertEqual(100, data.get("科创50"))
        self.assertEqual(100, data.get("上证50"))
        self.assertEqual("2.00亿", data["成交额"])
        self.assertIn("sh000688", http.call_args.args[0].full_url)
