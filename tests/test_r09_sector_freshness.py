"""R09: sector_index wall-clock budget, partial rows and field-level source times.

2026-11-02 #2: 881143 returned leader 百洋医药 +14.84% while the same-round quote
was -3.68%; the page has no quote clock.  Two independent defects:
1. ths_industry page reads had no total wall-clock budget (11.77s / 13.2s) and
   discarded rows already fetched when a later page failed.
2. leader fields had no source time of their own; they must not inherit the
   board quote time, HTTP Date or fetched_at.
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ym_stock_data import api
from ym_stock_data.provider_policy import CompiledPolicy
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers import stocktoday as stocktoday_module
from ym_stock_data.providers.local import LocalProvider
from ym_stock_data.providers.stocktoday import StockTodayProvider
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.sources import ths_industry


class _Response:
    def __init__(self, text="<html></html>", date="Mon, 02 Nov 2026 06:30:00 GMT"):
        self.text = text
        self.status_code = 200
        self.encoding = "gbk"
        self.headers = {"Date": date} if date else {}


def _row(code, name, **extra):
    return {"code": code, "name": name, "change_pct": 1.0, **extra}


ROW = {
    "code": "881143",
    "name": "医药商业",
    "change_pct": 1.23,
    "leader": "百洋医药",
    "leader_price": 26.08,
    "leader_change_pct": 14.84,
}


class SectorBudgetTests(unittest.TestCase):
    def test_page_requests_stop_at_wall_clock_deadline(self):
        clock = iter([100.0, 100.0, 108.0])
        with patch.object(ths_industry, "_monotonic", side_effect=lambda: next(clock)), \
                patch.object(ths_industry.requests, "get", return_value=_Response()) as get, \
                patch.object(ths_industry, "_parse_industry_rows", return_value=[dict(ROW)]):
            result = ths_industry.fetch_sector_index(codes=["881143"])

        self.assertEqual(1, get.call_count)
        timeout = get.call_args.kwargs["timeout"]
        self.assertLessEqual(sum(timeout) if isinstance(timeout, tuple) else timeout, 7.5)
        self.assertEqual(["881143"], [row["code"] for row in result["items"]])
        self.assertTrue(result["partial"])
        self.assertEqual("SECTOR_PARTIAL_TIMEOUT", result["partial_reason_code"])
        self.assertNotIn("error", result)

    def test_network_error_after_first_page_keeps_fetched_rows(self):
        import requests

        with patch.object(ths_industry.requests, "get",
                          side_effect=[_Response(), requests.Timeout("slow")]), \
                patch.object(ths_industry, "_parse_industry_rows", return_value=[dict(ROW)]):
            result = ths_industry.fetch_sector_index(codes=["881143"])

        self.assertEqual(["881143"], [row["code"] for row in result["items"]])
        self.assertTrue(result["partial"])
        self.assertEqual("SECTOR_PARTIAL_TIMEOUT", result["partial_reason_code"])
        self.assertNotIn("error", result)

    def test_failure_before_any_page_stays_a_failure(self):
        import requests

        with patch.object(ths_industry.requests, "get", side_effect=requests.Timeout("slow")):
            result = ths_industry.fetch_sector_index(codes=["881143"])

        self.assertEqual([], result["items"])
        self.assertIn("error", result)
        self.assertEqual("TIMEOUT", result["error_type"])
        self.assertNotIn("partial", result)

    def test_partial_page_without_requested_sector_is_not_empty(self):
        import requests

        other = {"code": "881121", "name": "半导体", "change_pct": 1.0}
        with patch.object(ths_industry.requests, "get",
                          side_effect=[_Response(), requests.Timeout("slow")]), \
                patch.object(ths_industry, "_parse_industry_rows", return_value=[other]):
            result = ths_industry.fetch_sector_index(codes=["881143"])

        self.assertEqual([], result["items"])
        self.assertEqual(["881143"], result["missing"])
        self.assertIn("error", result)
        self.assertEqual("TIMEOUT", result["error_type"])
        self.assertNotIn("partial", result)

    def test_slow_upstream_ignoring_timeout_returns_within_budget_and_stays_isolated(self):
        def slow_get(url, **kwargs):
            if "/page/1/" in url:
                return _Response("881001", "Mon, 02 Nov 2026 01:00:00 GMT")
            time.sleep(0.6)
            return _Response("881002", "Mon, 02 Nov 2026 01:00:00 GMT")

        def fast_get(url, **kwargs):
            if "/page/1/" in url:
                return _Response("881003", "Mon, 02 Nov 2026 06:00:00 GMT")
            return _Response("")

        def parse(html):
            return [_row(html, f"行业{html}")] if html.startswith("881") else []

        with patch.object(ths_industry, "SECTOR_WALL_BUDGET_SEC", 0.15), \
                patch.object(ths_industry.requests, "get", side_effect=slow_get), \
                patch.object(ths_industry, "_parse_industry_rows", side_effect=parse):
            started = time.monotonic()
            first = ths_industry.fetch_sector_index(codes=["881001", "881002"])
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5)
        self.assertEqual(["881001"], [row["code"] for row in first["items"]])
        self.assertTrue(first["partial"])
        self.assertEqual("SECTOR_PARTIAL_TIMEOUT", first["partial_reason_code"])

        # 第二轮在慢请求仍睡眠时开始；晚到的 881002 不得进入本轮或下一轮。
        with patch.object(ths_industry, "SECTOR_WALL_BUDGET_SEC", 0.15), \
                patch.object(ths_industry.requests, "get", side_effect=fast_get), \
                patch.object(ths_industry, "_parse_industry_rows", side_effect=parse):
            second = ths_industry.fetch_sector_index(codes=["881003"])
            time.sleep(0.75)
            third = ths_industry.fetch_sector_index(codes=["881003"])

        self.assertEqual(["881003"], [row["code"] for row in second["items"]])
        self.assertEqual(["881003"], [row["code"] for row in third["items"]])
        self.assertNotIn("881002", [row["code"] for row in third["items"]])

    def test_retry_failure_merges_rows_already_fetched(self):
        first = [_row("881001", "行业A")]
        retry_error = {"error": "industry wall-clock budget exhausted",
                       "error_type": "TIMEOUT",
                       "rows": [_row("881002", "行业B")]}
        with patch.object(ths_industry, "_fetch_all_industry_rows",
                          side_effect=[first, retry_error]):
            result = ths_industry.fetch_sector_index(codes=["881001", "881002"])

        self.assertEqual(["881001", "881002"], [row["code"] for row in result["items"]])
        self.assertEqual([], result["missing"])
        self.assertTrue(result["partial"])
        self.assertEqual("SECTOR_PARTIAL_TIMEOUT", result["partial_reason_code"])
        self.assertEqual(["881002"], result["recovered_missing"])

    def test_retry_failure_without_requested_sector_is_a_failure(self):
        first = [_row("881001", "行业A")]
        retry_error = {"error": "industry wall-clock budget exhausted",
                       "error_type": "TIMEOUT",
                       "rows": [_row("881002", "行业B")]}
        with patch.object(ths_industry, "_fetch_all_industry_rows",
                          side_effect=[first, retry_error]):
            result = ths_industry.fetch_sector_index(codes=["881143"])

        self.assertEqual([], result["items"])
        self.assertIn("error", result)
        self.assertEqual("TIMEOUT", result["error_type"])
        self.assertNotIn("partial", result)


class LeaderSourceTimeTests(unittest.TestCase):
    def test_leader_does_not_inherit_board_quote_time(self):
        row = dict(ROW, quote_time="2026-11-02T10:00:00+08:00")
        result = ths_industry._select_industries([row], ["881143"], None)

        self.assertEqual("2026-11-02T10:00:00+08:00", result["data_as_of"])
        self.assertIsNone(result["leader_data_as_of"])
        self.assertEqual("unknown", result["leader_time_status"])
        self.assertEqual("DATA_TIME_UNKNOWN", result["leader_time_reason_code"])

    def test_older_leader_time_is_kept_and_flagged(self):
        row = dict(
            ROW,
            quote_time="2026-11-02T10:00:00+08:00",
            leader_quote_time="2026-10-30T15:00:00+08:00",
        )
        result = ths_industry._select_industries([row], ["881143"], None)

        self.assertEqual("2026-11-02T10:00:00+08:00", result["data_as_of"])
        self.assertEqual("2026-10-30T15:00:00+08:00", result["leader_data_as_of"])
        self.assertEqual("older_than_quote", result["leader_time_status"])
        self.assertEqual("LEADER_TIME_OLDER_THAN_QUOTE", result["leader_time_reason_code"])

    def test_no_source_time_anywhere_stays_unknown(self):
        result = ths_industry._select_industries([dict(ROW)], ["881143"], None)

        self.assertIsNone(result["data_as_of"])
        self.assertEqual("DATA_TIME_UNKNOWN", result["data_time_reason_code"])
        self.assertIsNone(result["leader_data_as_of"])
        self.assertEqual("unknown", result["leader_time_status"])

    def test_leader_comparison_is_per_row_not_cross_board_min(self):
        current = _row(
            "881001", "行业A",
            quote_time="2026-11-02T10:00:00+08:00",
            leader="甲", leader_quote_time="2026-11-02T10:00:00+08:00",
        )
        stale = _row(
            "881002", "行业B",
            quote_time="2026-11-02T11:00:00+08:00",
            leader="乙", leader_quote_time="2026-11-02T10:30:00+08:00",
        )
        result = ths_industry._select_industries([current, stale],
                                                 ["881001", "881002"], None)

        statuses = {row["code"]: row["leader_time_status"] for row in result["items"]}
        self.assertEqual("upstream_quote_time", statuses["881001"])
        self.assertEqual("older_than_quote", statuses["881002"])
        self.assertEqual("older_than_quote", result["leader_time_status"])
        self.assertEqual("LEADER_TIME_OLDER_THAN_QUOTE",
                         result["leader_time_reason_code"])

    def test_envelope_marks_partial_rows_degraded(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        def partial(*args, **kwargs):
            return {
                "items": [dict(ROW)],
                "by_code": {"881143": dict(ROW)},
                "by_name": {"医药商业": dict(ROW)},
                "missing": [],
                "source": "ths_industry",
                "data_as_of": None,
                "data_time_basis": "provider_not_supplied",
                "data_time_reason_code": "DATA_TIME_UNKNOWN",
                "partial": True,
                "partial_reason_code": "SECTOR_PARTIAL_TIMEOUT",
            }

        with patch.object(ths_industry, "fetch_sector_index", side_effect=partial):
            result = api._query_with(
                "sector_index",
                {"codes": ["881143"]},
                provider_loader=lambda name: LocalProvider("ths_industry"),
                state_loader=lambda: ProviderState(Path(tmp.name) / "state.sqlite"),
                policy_loader=lambda: CompiledPolicy(None, "inactive", None),
            )

        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertTrue(result["_meta"]["partial"])
        self.assertEqual("partial", result["_meta"]["quality"]["status"])
        self.assertIn("SECTOR_PARTIAL_TIMEOUT", result["_meta"]["quality"]["reason_codes"])
        self.assertEqual(1, len(result["data"]["items"]))

    def test_envelope_flags_older_leader_time(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = ths_industry._select_industries([dict(
            ROW,
            quote_time="2026-11-02T10:00:00+08:00",
            leader_quote_time="2026-10-30T15:00:00+08:00",
        )], ["881143"], None)

        with patch.object(ths_industry, "fetch_sector_index", return_value=base):
            result = api._query_with(
                "sector_index",
                {"codes": ["881143"]},
                provider_loader=lambda name: LocalProvider("ths_industry"),
                state_loader=lambda: ProviderState(Path(tmp.name) / "state.sqlite"),
                policy_loader=lambda: CompiledPolicy(None, "inactive", None),
            )

        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertIn("LEADER_TIME_OLDER_THAN_QUOTE",
                      result["_meta"]["quality"]["reason_codes"])


class StockTodaySectorBudgetTests(unittest.TestCase):
    def test_type_boundary_and_bounded_request(self):
        provider = StockTodayProvider.__new__(StockTodayProvider)
        provider.clock = time
        provider._request_table = Mock(return_value=ProviderOutcome(
            "stocktoday", "success", data={"items": [
                {"ts_code": "881001.TI", "type": "I", "name": "行业"},
                {"ts_code": "885001.TI", "type": "N", "name": "概念"},
            ]}))
        result = provider._call_sector_index({"codes": ["881001", "885001"]})

        self.assertEqual(["行业"], [row["name"] for row in result.data["items"]])
        self.assertIn("885001", result.data["missing"])
        timeout = provider._request_table.call_args.kwargs["timeout"]
        self.assertLessEqual(sum(timeout) if isinstance(timeout, tuple) else timeout, 7.5)
        evidence = result.data["field_evidence"]["881001"]
        self.assertIsNone(evidence["data_as_of"])
        self.assertEqual("provider_not_supplied", evidence["data_time_basis"])

    def test_slow_table_ignores_socket_timeout_but_round_still_returns(self):
        provider = StockTodayProvider(
            token_loader=lambda: "token", budget=Mock(), clock=time)
        calls = []

        def request(name, nested, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                # 合成上游完全忽略 socket timeout；只有有界 future 能截断。
                time.sleep(0.6)
                return ProviderOutcome("stocktoday", "success", data={"items": [
                    {"ts_code": "881001.TI", "type": "I", "name": "旧"}]})
            return ProviderOutcome("stocktoday", "success", data={"items": [
                {"ts_code": "881002.TI", "type": "I", "name": "新"}]})

        provider._request_table = request
        with patch.object(stocktoday_module, "SECTOR_INDEX_BUDGET_SEC", 0.15):
            started = time.monotonic()
            first = provider._call_sector_index({"codes": ["881001"]})
            elapsed = time.monotonic() - started
            second = provider._call_sector_index({"codes": ["881002"]})
            time.sleep(0.7)
            third = provider._call_sector_index({"codes": ["881002"]})

        self.assertLess(elapsed, 0.5)
        self.assertEqual("timeout", first.status)
        self.assertEqual("TIMEOUT", first.error_code)
        self.assertFalse(calls[0]["write_state"])
        self.assertEqual(["新"], [row["name"] for row in second.data["items"]])
        self.assertEqual(["新"], [row["name"] for row in third.data["items"]])


if __name__ == "__main__":
    unittest.main()
