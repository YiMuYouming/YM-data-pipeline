"""P10/#5#10#16#20#22#24：盘中 market_intraday_state 目标 <8s，超时批次不整轮 gap。

改前 RED（2026-10-27 真实巡检）：四个 rt_k 批次虽并发，但任一慢批次（上游 read
timeout 15s）会把整轮拖到 16.3~24.2s，随后整轮 pipeline_gap，已取到的三批数据
全部丢弃。要求：
1. 单批超过硬预算如实标缺口（snapshot_batch_timeout:<pattern>），不整轮失败；
2. 部分批次成功时返回 partial=true + 已取到的部分，source_gaps 点名缺哪批；
3. 全部批次都失败时仍按源错误码整轮失败（不造 partial 假象）；
4. 尾部的锦上添花步骤（行业回填）在预算耗尽时跳过并如实标缺口，不拖时。
"""

from __future__ import annotations

import tempfile
import time
import unittest
import unittest.mock
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ym_stock_data import intraday_state
from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.providers.base import ProviderOutcome

BATCH_BUDGET = 0.30
SLOW_BATCH = "6*.SH"


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=TZ_SHANGHAI)


def row(ts_code, close, pre_close, updated="2026-09-28T10:00:00"):
    return {"ts_code": ts_code, "name": "测试", "close": close, "pre_close": pre_close,
            "high": close, "vol": 100.0, "amount": 1000.0, "updated_at": updated}


class BudgetProvider:
    """6*.SH 批次超过硬预算，其余批次立即返回；用来钉住「部分成功不整轮 gap」。"""

    def __init__(self, *, slow=SLOW_BATCH, sleep=0.8, fail_all=False):
        self.slow, self.sleep, self.fail_all = slow, sleep, fail_all
        self.calls = []

    def _request_table(self, name, nested, **_kwargs):
        self.calls.append((name, dict(nested)))
        if name == "rt_k":
            pattern = nested["ts_code"]
            if self.fail_all:
                return ProviderOutcome("stocktoday", "provider_error", error_code="TIMEOUT")
            if pattern == self.slow:
                time.sleep(self.sleep)
                return ProviderOutcome("stocktoday", "proxy_error", error_code="TIMEOUT")
            items = {
                # 000001 封在涨停价 11.0：用来验证涨停池行业回填的预算分支。
                "0*.SZ": [row("000001.SZ", 11.0, 10.0)],
                "3*.SZ": [row("300001.SZ", 12.0, 10.0)],
                "*.BJ": [row("920001.BJ", 10.0, 10.0)],
            }.get(pattern, [])
            return ProviderOutcome("stocktoday", "success", data={"items": items},
                                   fetched_at="2026-09-28T10:00:05+08:00")
        if name == "stk_limit":
            items = [{"ts_code": code, "trade_date": nested["trade_date"],
                      "up_limit": 11.0, "down_limit": 9.0}
                     for code in ("600001.SH", "000001.SZ", "300001.SZ", "920001.BJ")]
            return ProviderOutcome("stocktoday", "success", data={"items": items})
        if name == "limit_list_d":
            return ProviderOutcome("stocktoday", "success", data={"items": []})
        return ProviderOutcome("stocktoday", "empty", data={"items": []})


class PartialSnapshotTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in (("STK_LIMIT_CACHE", Path(tmp.name)),
                            ("SNAPSHOT_BATCH_BUDGET_SEC", BATCH_BUDGET),
                            ("TOTAL_BUDGET_SEC", 5.0)):
            patcher = patch.object(intraday_state, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        ladder = patch.object(intraday_state, "_previous_ladder",
                              return_value=(None, None, None))
        ladder.start()
        self.addCleanup(ladder.stop)
        quotes = patch("ym_stock_data.sources.tencent.fetch_quotes", return_value={})
        quotes.start()
        self.addCleanup(quotes.stop)
        self.now = at("2026-09-28T10:01:00")

    def test_timed_out_batch_returns_partial_instead_of_whole_round_gap(self):
        provider = BudgetProvider()
        started = time.monotonic()
        state = intraday_state.build(provider, now=self.now)
        elapsed = time.monotonic() - started
        # 硬预算：不能等慢批次的上游 read timeout（真实 15s+）。
        self.assertLess(elapsed, 0.8)
        self.assertTrue(state["partial"])
        self.assertIn("snapshot_batch_timeout:" + SLOW_BATCH, state["source_gaps"])
        self.assertEqual(4, len(state["_stocktoday"]["batches"]))
        slow = [b for b in state["_stocktoday"]["batches"] if b["ts_code"] == SLOW_BATCH][0]
        self.assertEqual("timeout", slow["status"])
        self.assertEqual("SNAPSHOT_BATCH_TIMEOUT", slow["error_code"])
        # 已取到的三批照常出数：宽表覆盖 3/4 只票，不因缺一批整轮作废。
        self.assertEqual(3, state["breadth_buckets"]["_total"])
        self.assertEqual("20260928", state["trade_date"])

    def test_all_batches_failing_still_raises_the_source_code(self):
        with self.assertRaises(intraday_state.IntradayStateError) as ctx:
            intraday_state.build(BudgetProvider(fail_all=True), now=self.now)
        self.assertEqual("TIMEOUT", ctx.exception.code)

    def test_industry_backfill_is_skipped_when_budget_is_gone(self):
        provider = BudgetProvider()

        backfill = unittest.mock.Mock(return_value={"industries": {}, "error_code": None})
        with patch.object(intraday_state, "_industry_backfill", backfill), \
                patch.object(intraday_state, "TOTAL_BUDGET_SEC", 1.0), \
                patch.object(intraday_state, "INDUSTRY_BACKFILL_MIN_REMAINING_SEC", 4.0):
            state = intraday_state.build(provider, now=self.now)
        self.assertFalse(backfill.called)
        self.assertIn("limit_up_industry_backfill_skipped_budget", state["source_gaps"])


if __name__ == "__main__":
    unittest.main()
