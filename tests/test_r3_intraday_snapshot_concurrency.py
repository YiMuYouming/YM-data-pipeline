"""R3 item7b：盘中状态快照批次必须并发取，且预算计数在并发下不丢请求。

改前 RED：四个 rt_k 批次串行，单批 >200ms 时总耗时线性叠加，盘中稳定超过 8s
（2026-10-26 真实回放 5 次：median 8.87s / max 13.13s）。

两个被钉住的行为：
1. SNAPSHOT_BATCHES 四个批次的抓取是**并发**的（耗时接近单批而不是四批之和），
   但 _stocktoday.batches 诊断仍按 SNAPSHOT_BATCHES 的固定顺序如实排列；
2. 并发下 RequestBudget 的"每分钟/每日上限"语义不变：并发不丢请求、不因
   sqlite 写锁把合法请求错报成 LOCAL_BUDGET_UNAVAILABLE。
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ym_stock_data import intraday_state
from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.providers.stocktoday import RequestBudget

BATCH_SLEEP = 0.20


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=TZ_SHANGHAI)


def row(ts_code, close, pre_close, updated="2026-09-28T10:00:00"):
    return {"ts_code": ts_code, "name": "测试", "close": close, "pre_close": pre_close,
            "high": close, "vol": 100.0, "amount": 1000.0, "updated_at": updated}


class SlowSnapshotProvider:
    """每个 rt_k 批次都真实等待 BATCH_SLEEP，用来区分串行与并发。"""

    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def _request_table(self, name, nested, **_kwargs):
        with self._lock:
            self.calls.append((name, dict(nested), time.monotonic()))
        if name == "rt_k":
            time.sleep(BATCH_SLEEP)
            items = [row("600001.SH", 11.0, 10.0), row("000001.SZ", 10.2, 10.0),
                     row("300001.SZ", 12.0, 10.0), row("920001.BJ", 10.0, 10.0)]
            return ProviderOutcome("stocktoday", "success", data={"items": items},
                                   fetched_at="2026-09-28T10:00:05+08:00")
        if name == "stk_limit":
            time.sleep(BATCH_SLEEP)
            items = [{"ts_code": code, "trade_date": nested["trade_date"], "up_limit": 11.0,
                      "down_limit": 9.0}
                     for code in ("600001.SH", "000001.SZ", "300001.SZ", "920001.BJ")]
            return ProviderOutcome("stocktoday", "success", data={"items": items})
        if name == "limit_list_d":
            time.sleep(BATCH_SLEEP)
            return ProviderOutcome("stocktoday", "success", data={"items": []})
        return ProviderOutcome("stocktoday", "empty", data={"items": []})

    def board_industry_map(self, codes, trade_date):  # 7a 的可选回填入口
        return {}


class SnapshotConcurrencyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(intraday_state, "STK_LIMIT_CACHE", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        ladder = patch.object(intraday_state, "_previous_ladder",
                              return_value=(None, None, None))
        ladder.start()
        self.addCleanup(ladder.stop)
        break_risk = patch.object(intraday_state, "_sealed_break_risk",
                                  return_value={"value": None, "source_gaps": []})
        break_risk.start()
        self.addCleanup(break_risk.stop)
        quotes = patch("ym_stock_data.sources.tencent.fetch_quotes", return_value={})
        quotes.start()
        self.addCleanup(quotes.stop)

    def test_four_snapshot_batches_are_fetched_concurrently(self):
        provider = SlowSnapshotProvider()
        started = time.monotonic()
        state = intraday_state.build(provider, now=at("2026-09-28T10:01:00"))
        elapsed = time.monotonic() - started
        # 串行：4 批 * 0.20 = 0.80s+；并发：约 0.20s + 后置独立请求 0.20s。
        self.assertLess(elapsed, 4 * BATCH_SLEEP,
                        f"snapshot batches not concurrent: {elapsed:.3f}s")
        # 诊断顺序仍按固定批次表，不因并发而乱序。
        self.assertEqual(list(intraday_state.SNAPSHOT_BATCHES),
                         [batch["ts_code"] for batch in state["_stocktoday"]["batches"]])
        self.assertEqual("20260928", state["trade_date"])

    def test_snapshot_batch_failure_keeps_typed_error(self):
        class Broken(SlowSnapshotProvider):
            def _request_table(self, name, nested, **kwargs):
                if name == "rt_k" and nested.get("ts_code") == "0*.SZ":
                    return ProviderOutcome("stocktoday", "empty", error_code="UPSTREAM_EMPTY",
                                           data={"items": []})
                return super()._request_table(name, nested, **kwargs)

        with self.assertRaises(intraday_state.IntradayStateError) as caught:
            intraday_state.build(Broken(), now=at("2026-09-28T10:01:00"))
        self.assertEqual("UPSTREAM_EMPTY", caught.exception.code)


class BudgetConcurrencyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "budget.sqlite3"

    def _hammer(self, budget, threads=16):
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(threads)

        def worker():
            barrier.wait()
            try:
                ok = budget.acquire()
            except Exception as error:  # noqa: BLE001 - 并发下不允许抛
                ok = type(error).__name__
            with lock:
                results.append(ok)

        workers = [threading.Thread(target=worker) for _ in range(threads)]
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join()
        return results

    def test_concurrent_acquire_never_loses_a_request(self):
        budget = RequestBudget(self.path, per_day=1000, per_minute=1000)
        results = self._hammer(budget)
        self.assertEqual([True] * 16, sorted(results, key=str))

    def test_concurrent_acquire_respects_per_minute_ceiling(self):
        budget = RequestBudget(self.path, per_day=1000, per_minute=5)
        results = self._hammer(budget)
        self.assertEqual(5, sum(1 for item in results if item is True))
        self.assertEqual(11, sum(1 for item in results if item is False))


if __name__ == "__main__":
    unittest.main()
