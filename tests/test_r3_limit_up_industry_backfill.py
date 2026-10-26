"""R3 item7a：涨停池 industry 回填（Bots #6/#15）。

RED 事实（2026-10-26 真实回放）：limit_list_d 当日明细会**假空**（网关对表读取约 8%
返回空表，同一 trade_date 多次请求结果不同；本轮 5 次回放里第 5 次就拿到空表），
此时 limit_up 每行 industry="" —— 「今日涨停池行业分布」结论直接失效。

钉住的行为：
1. 当日明细**空表重试一次**（既有 BATCH4/ths_daily 的假空先例），reads 如实进诊断；
2. 明细仍不可用/行业为空时，用 DP 既有公开数据 ths_member（同花顺 881 行业成员）
   反查回填，逐行标 industry_source，**不把空写成确定值**；
3. 回填不到的如实留空 + 原因码（limit_up_industry_unresolved），不猜、不 hardcode 名单。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ym_stock_data import intraday_state
from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.providers.base import ProviderOutcome


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=TZ_SHANGHAI)


def row(ts_code, close, pre_close, name="测试"):
    return {"ts_code": ts_code, "name": name, "close": close, "pre_close": pre_close,
            "high": close, "vol": 100.0, "amount": 1000.0,
            "updated_at": "2026-09-28T10:00:00"}


class IndustryFakeProvider:
    """limit_list_d 可控（成功/假空/失败），并可回填行业。"""

    def __init__(self, *, metadata_reads, industry_map=None, has_backfill=True,
                 partial=False):
        self.metadata_reads = list(metadata_reads)
        self.industry_map = industry_map or {}
        self.partial = partial
        self.calls = []
        self.backfill_calls = []
        if not has_backfill:
            self.board_industry_map = None

    def _request_table(self, name, nested, **_kwargs):
        self.calls.append((name, dict(nested)))
        if name == "rt_k":
            items = {"6*.SH": [row("600001.SH", 11.0, 10.0), row("600002.SH", 11.0, 10.0)],
                     "0*.SZ": [row("000001.SZ", 10.2, 10.0)],
                     "3*.SZ": [row("300001.SZ", 12.0, 10.0)],
                     "*.BJ": [row("920001.BJ", 10.0, 10.0)]}[nested["ts_code"]]
            return ProviderOutcome("stocktoday", "success" if items else "empty",
                                   data={"items": items},
                                   fetched_at="2026-09-28T10:00:05+08:00")
        if name == "stk_limit":
            items = [{"ts_code": code, "trade_date": nested["trade_date"],
                      "up_limit": 11.0, "down_limit": 9.0}
                     for code in ("600001.SH", "600002.SH", "000001.SZ", "300001.SZ")]
            return ProviderOutcome("stocktoday", "success", data={"items": items})
        if name == "limit_list_d":
            read = self.metadata_reads.pop(0) if self.metadata_reads else "empty"
            if read == "empty":
                return ProviderOutcome("stocktoday", "empty", data={"items": []})
            if read == "error":
                return ProviderOutcome("stocktoday", "provider_error", error_code="UPSTREAM_500")
            return ProviderOutcome("stocktoday", "success", data={"items": [
                {"ts_code": "600001.SH", "trade_date": nested["trade_date"],
                 "industry": "白酒", "first_time": "093100"},
                {"ts_code": "600002.SH", "trade_date": nested["trade_date"],
                 "industry": "", "first_time": "094500"}]})
        return ProviderOutcome("stocktoday", "empty", data={"items": []})

    def board_industry_map(self, codes, trade_date):
        self.backfill_calls.append({"codes": list(codes), "trade_date": trade_date})
        return {"industries": {code: self.industry_map[code] for code in codes
                               if code in self.industry_map},
                "unresolved": [code for code in codes if code not in self.industry_map],
                "error_code": None, "partial": self.partial, "roster_rows": 3102,
                "cache_hit": 0}


class LimitMetadataRetryTests(unittest.TestCase):
    def test_fake_empty_metadata_is_retried_once(self):
        provider = IndustryFakeProvider(metadata_reads=["empty", "success"])
        diagnostics = {}
        rows = intraday_state._limit_metadata(provider, "20260928", diagnostics=diagnostics)
        self.assertEqual(2, diagnostics["reads"])
        self.assertEqual("success", diagnostics["status"])
        self.assertEqual("白酒", rows["600001"]["industry"])
        self.assertEqual(2, sum(1 for name, _ in provider.calls if name == "limit_list_d"))

    def test_persistent_empty_is_reported_as_empty_without_guessing(self):
        provider = IndustryFakeProvider(metadata_reads=["empty", "empty", "empty"])
        diagnostics = {}
        rows = intraday_state._limit_metadata(provider, "20260928", diagnostics=diagnostics)
        self.assertEqual({}, rows)
        self.assertEqual("empty", diagnostics["status"])
        self.assertEqual(2, diagnostics["reads"])


class IndustryBackfillTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(intraday_state, "STK_LIMIT_CACHE", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        ladder = patch.object(intraday_state, "_previous_ladder",
                              return_value=({"600001": 1}, [], "limit_step_crosschecked"))
        ladder.start()
        self.addCleanup(ladder.stop)
        break_risk = patch.object(intraday_state, "_sealed_break_risk",
                                  return_value={"value": 0.0, "source_gaps": []})
        break_risk.start()
        self.addCleanup(break_risk.stop)
        quotes = patch("ym_stock_data.sources.tencent.fetch_quotes", return_value={})
        quotes.start()
        self.addCleanup(quotes.stop)
        self.now = at("2026-09-28T10:01:00")

    def build(self, provider):
        return intraday_state.build(provider, now=self.now)

    def test_missing_industry_is_backfilled_with_row_provenance(self):
        provider = IndustryFakeProvider(
            metadata_reads=["empty", "empty"],
            industry_map={"600001": {"board_id": "881273", "name": "白酒"}})
        state = self.build(provider)
        by_code = {item["code"]: item for item in state["limit_up"]}
        self.assertEqual("白酒", by_code["600001"]["industry"])
        self.assertEqual("ths_member:881", by_code["600001"]["industry_source"])
        self.assertIn("limit_up_metadata_empty", state["source_gaps"])
        self.assertIn("limit_up_industry_backfilled", state["source_gaps"])
        self.assertEqual(1, len(provider.backfill_calls))
        self.assertEqual("20260928", provider.backfill_calls[0]["trade_date"])

    def test_unresolved_industry_stays_unknown_with_reason_code(self):
        provider = IndustryFakeProvider(metadata_reads=["empty", "empty"], industry_map={})
        state = self.build(provider)
        by_code = {item["code"]: item for item in state["limit_up"]}
        for item in by_code.values():
            self.assertEqual("", item["industry"])
            self.assertEqual("unresolved", item["industry_source"])
        self.assertIn("limit_up_industry_unresolved", state["source_gaps"])

    def test_dated_metadata_industry_wins_and_is_not_overwritten(self):
        provider = IndustryFakeProvider(
            metadata_reads=["success"],
            industry_map={"600001": {"board_id": "881273", "name": "白酒"}})
        state = self.build(provider)
        by_code = {item["code"]: item for item in state["limit_up"]}
        self.assertEqual("白酒", by_code["600001"]["industry"])
        self.assertEqual("limit_list_d", by_code["600001"]["industry_source"])
        # 明细里 industry 为空的那只才回填；明细已给行业的行不重复查、不被覆盖。
        self.assertEqual([{"codes": ["600002"], "trade_date": "20260928"}],
                         provider.backfill_calls)
        self.assertEqual("", by_code["600002"]["industry"])
        self.assertEqual("unresolved", by_code["600002"]["industry_source"])
        self.assertNotIn("limit_up_industry_backfilled", state["source_gaps"])

    def test_blank_industry_inside_dated_metadata_is_backfilled(self):
        provider = IndustryFakeProvider(
            metadata_reads=["success"],
            industry_map={"600002": {"board_id": "881126", "name": "汽车零部件"}})
        state = self.build(provider)
        by_code = {item["code"]: item for item in state["limit_up"]}
        self.assertEqual("汽车零部件", by_code["600002"]["industry"])
        self.assertEqual("ths_member:881", by_code["600002"]["industry_source"])
        self.assertEqual("limit_list_d", by_code["600001"]["industry_source"])

    def test_partial_backfill_is_reported_as_partial_gap(self):
        provider = IndustryFakeProvider(
            metadata_reads=["empty", "empty"], partial=True,
            industry_map={"600001": {"board_id": "881273", "name": "白酒"}})
        state = self.build(provider)
        self.assertIn("limit_up_industry_backfill_partial", state["source_gaps"])
        self.assertIn("limit_up_industry_unresolved", state["source_gaps"])
        observation = state["_stocktoday"]["industry_backfill"]
        self.assertTrue(observation["partial"])
        self.assertEqual(1, observation["resolved"])

    def test_provider_without_backfill_capability_reports_typed_gap(self):
        provider = IndustryFakeProvider(metadata_reads=["empty", "empty"],
                                        has_backfill=False)
        state = self.build(provider)
        self.assertIn("limit_up_industry_backfill_unavailable", state["source_gaps"])
        for item in state["limit_up"]:
            self.assertEqual("", item["industry"])
            self.assertEqual("unresolved", item["industry_source"])


if __name__ == "__main__":
    unittest.main()
