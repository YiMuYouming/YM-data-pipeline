import tempfile
import unittest
import unittest.mock
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from ym_stock_data import intraday_state
from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.providers.stocktoday import RequestBudget, StockTodayProvider


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=TZ_SHANGHAI)


def row(ts_code, close, pre_close, high=None, updated="2026-09-28T10:00:00", vol=100.0, name="测试"):
    return {"ts_code": ts_code, "name": name, "close": close, "pre_close": pre_close,
            "high": high if high is not None else close, "vol": vol, "amount": 1000.0,
            "updated_at": updated}


class FakeStockToday:
    def __init__(self, batches, limits):
        self.batches, self.limits, self.calls = batches, limits, []

    def _request_table(self, name, nested, **_kwargs):
        self.calls.append((name, dict(nested)))
        if name == "rt_k":
            items = self.batches.get(nested["ts_code"], [])
        elif name == "stk_limit":
            items = [{"ts_code": code, "trade_date": nested["trade_date"], "up_limit": up, "down_limit": down}
                     for code, (up, down) in self.limits.items()]
        else:
            items = []
        return ProviderOutcome("stocktoday", "success" if items else "empty",
                               data={"items": items}, fetched_at="2026-09-28T10:00:05+08:00")


class SessionTimeTests(unittest.TestCase):
    def test_holiday_restamp_maps_to_previous_session_close(self):
        # 2026-09-25 (中秋) rows carried 09-24 prices.
        mapped, corrected = intraday_state.session_data_time(at("2026-09-25T15:11:00"))
        self.assertEqual(at("2026-09-24T15:00:00"), mapped)
        self.assertTrue(corrected)

    def test_pre_open_stamp_belongs_to_previous_session(self):
        mapped, corrected = intraday_state.session_data_time(at("2026-09-28T08:50:00"))
        self.assertEqual(at("2026-09-24T15:00:00"), mapped)
        self.assertTrue(corrected)

    def test_session_stamp_is_kept(self):
        stamp = at("2026-09-28T10:01:00")
        self.assertEqual((stamp, False), intraday_state.session_data_time(stamp))

    def test_freshness_tiers(self):
        self.assertEqual("normal", intraday_state.freshness_tier(180))
        self.assertEqual("aging", intraday_state.freshness_tier(181))
        self.assertEqual("aging", intraday_state.freshness_tier(600))
        self.assertEqual("expired", intraday_state.freshness_tier(601))
        self.assertEqual("unknown", intraday_state.freshness_tier(None))

    def test_dedupe_keeps_each_codes_latest_row(self):
        rows = [row("688102.SH", 30.9, 31.58, updated="2026-09-28T10:00:00"),
                row("688102.SH", 31.0, 31.58, updated="2026-09-28T10:00:03"),
                row("600519.SH", 1237.0, 1251.24)]
        latest = intraday_state.dedupe_latest(rows)
        self.assertEqual(2, len(latest))
        self.assertEqual(31.0, latest["688102.SH"]["close"])


class BuildTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(intraday_state, "STK_LIMIT_CACHE", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        ladder = patch.object(intraday_state, "_previous_ladder",
                              return_value=({"600001": 1, "600002": 2}, ["600009"], "limit_step_crosschecked"))
        ladder.start()
        self.addCleanup(ladder.stop)
        self.now = at("2026-09-28T10:01:00")
        self.quotes = patch("ym_stock_data.sources.tencent.fetch_quotes", return_value={})
        self.quotes.start()
        self.addCleanup(self.quotes.stop)

    def provider(self, extra=None):
        sh = [row("600001.SH", 11.0, 10.0), row("600001.SH", 11.0, 10.0, updated="2026-09-28T09:59:00"),
              row("600002.SH", 10.5, 10.0, high=11.0), row("600003.SH", 9.0, 10.0),
              row("600004.SH", 10.0, 10.0), row("600005.SH", 11.0, 10.0, name="*ST测试"),
              row("600009.SH", 9.5, 10.0)]
        batches = {"6*.SH": sh, "0*.SZ": [row("000001.SZ", 10.2, 10.0)],
                   "3*.SZ": [row("300001.SZ", 12.0, 10.0)], "*.BJ": [row("920001.BJ", 10.0, 10.0, vol=0)]}
        limits = {"600001.SH": (11.0, 9.0), "600002.SH": (11.0, 9.0), "600003.SH": (11.0, 9.0),
                  "600004.SH": (11.0, 9.0), "600005.SH": (10.5, 9.5), "600009.SH": (11.0, 9.0),
                  "000001.SZ": (11.0, 9.0), "300001.SZ": (12.0, 8.0), "920001.BJ": (13.0, 7.0),
                  "900901.SH": (1.1, 0.9)}
        if extra:
            batches.update(extra)
        return FakeStockToday(batches, limits)

    def test_snapshot_indicators_use_one_input_set(self):
        state = intraday_state.build(self.provider(), now=self.now)
        ind = state["indicators"]
        self.assertEqual("20260928", state["trade_date"])
        self.assertEqual("20260924", state["previous_trade_date"])
        self.assertEqual("normal", state["freshness_tier"])
        self.assertEqual(60, state["age_seconds"])
        # Traded universe includes ST (600005 up); 920001 has no volume.
        self.assertEqual((5, 2, 1), (ind["up_count"], ind["down_count"], ind["flat_count"]))
        self.assertAlmostEqual(71.428571, ind["emotion"], places=6)
        # Limits exclude ST: 600001 & 300001 sealed, 600002 broken, 600003 at limit down.
        self.assertEqual(["300001", "600001"], sorted(item["code"] for item in state["limit_up"]))
        self.assertEqual(["600002"], [item["code"] for item in state["broken"]])
        self.assertEqual(["600003"], [item["code"] for item in state["limit_down"]])
        self.assertAlmostEqual(33.333333, ind["broken_rate"], places=6)
        # 600001 was a first board yesterday → 2 today; 600002 (2 boards) broke.
        self.assertEqual({"2": ["600001"], "1": ["300001"]}, state["ladder"])
        self.assertEqual(100.0, ind["promotion"]["one_to_two"]["pct"])
        self.assertEqual(0.0, ind["promotion"]["two_to_three"]["pct"])
        self.assertEqual(-5.0, ind["broken_return"])
        # B share 900901 is outside the A-share universe.
        self.assertEqual({"covered": 9, "universe": 9, "pct": 100.0}, state["coverage"])
        self.assertEqual(1, state["_stocktoday"]["deduplicated_rows"])

    def test_structural_gap_is_filled_per_code_from_tencent(self):
        provider = self.provider()
        provider.limits["003026.SZ"] = (22.0, 18.0)
        quotes = {"003026": {"name": "中晶科技", "price": 22.0, "last_close": 20.0, "high": 22.0,
                             "volume": 5e6, "amount": 1e8, "quote_time": "2026-09-28T10:00:30+08:00",
                             "ask_price1": 0, "ask_volume1": 0, "bid_price1": 22.0, "bid_volume1": 9e5}}
        state = intraday_state.build(provider, now=self.now, quote_loader=lambda codes: quotes)
        self.assertEqual(["003026"], state["filled_by_fallback"])
        self.assertEqual(100.0, state["coverage"]["pct"])
        sealed = {row["code"]: row for row in state["limit_up"]}
        self.assertEqual("tencent", sealed["003026"]["source"])
        self.assertEqual(3, state["indicators"]["limit_up_count"])
        # the data time stays StockToday's own snapshot time
        self.assertEqual("2026-09-28T10:00:00+08:00", state["data_as_of"])

    def test_large_gap_is_not_patched_with_tencent(self):
        provider = self.provider()
        for i in range(250):
            provider.limits[f"60{i + 1000:04d}.SH"] = (11.0, 9.0)
        loader = unittest.mock.Mock()
        with self.assertRaises(intraday_state.IntradayStateError):
            intraday_state.build(provider, now=self.now, quote_loader=loader)
        loader.assert_not_called()

    def test_expired_snapshot_raises_instead_of_serving_old_numbers(self):
        with self.assertRaises(intraday_state.IntradayStateError) as caught:
            intraday_state.build(self.provider(), now=at("2026-09-28T10:20:00"))
        self.assertEqual("QUALITY_STALE", caught.exception.code)

    def test_previous_session_snapshot_during_session_is_expired(self):
        old = {pattern: [dict(item, updated_at="2026-09-24T15:00:00") for item in items]
               for pattern, items in self.provider().batches.items()}
        with self.assertRaises(intraday_state.IntradayStateError) as caught:
            intraday_state.build(self.provider(old), now=self.now)
        self.assertEqual("QUALITY_STALE", caught.exception.code)

    def test_holiday_query_reports_the_last_completed_session(self):
        restamped = {pattern: [dict(item, updated_at="2026-09-25T15:11:00") for item in items]
                     for pattern, items in self.provider().batches.items()}
        provider = self.provider(restamped)
        state = intraday_state.build(provider, now=at("2026-09-27T10:00:00"))
        self.assertEqual("20260924", state["trade_date"])
        self.assertTrue(state["vendor_stamp_corrected"])
        self.assertEqual("2026-09-24T15:00:00+08:00", state["data_as_of"])
        self.assertIn(("stk_limit", {"trade_date": "20260924"}), provider.calls)

    def test_failed_batch_has_no_fallback(self):
        provider = self.provider({"3*.SZ": []})
        with self.assertRaises(intraday_state.IntradayStateError):
            intraday_state.build(provider, now=self.now)

    def test_low_coverage_is_flagged_then_refused(self):
        limits_extra = {f"60{i:04d}.SH": (11.0, 9.0) for i in range(100, 103)}
        provider = self.provider()
        provider.limits.update(limits_extra)  # 3 uncovered of 12 → 75%
        with self.assertRaises(intraday_state.IntradayStateError) as caught:
            intraday_state.build(provider, now=self.now)
        self.assertEqual("COVERAGE_INSUFFICIENT", caught.exception.code)


class StockTodayBudgetAndPlanTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "budget.sqlite3"

    def test_per_minute_and_per_day_caps_refuse_before_calling_upstream(self):
        minute = RequestBudget(self.path, per_minute=80, per_day=18000)
        now = 1_790_000_000.0
        self.assertTrue(all(minute.acquire(now=now + i * 0.1) for i in range(80)))
        self.assertFalse(minute.acquire(now=now + 9))
        self.assertTrue(minute.acquire(now=now + 61))
        day = RequestBudget(Path(self.path.parent) / "day.sqlite3", per_minute=0, per_day=3)
        self.assertTrue(all(day.acquire(now=now + i) for i in range(3)))
        self.assertFalse(day.acquire(now=now + 100))

        transport = Mock()
        provider = StockTodayProvider(token_loader=lambda: "synthetic-plan-token", post=transport,
                                      budget=RequestBudget(Path(self.path.parent) / "cap.sqlite3", per_minute=1, per_day=0))
        first = Mock(status_code=200)
        first.json.return_value = {"code": 0, "data": [{"ts_code": "600519.SH"}]}
        transport.return_value = first
        provider._request_table("rt_k", {"ts_code": "600519.SH"})
        second = provider._request_table("rt_k", {"ts_code": "600519.SH"})
        self.assertEqual("LOCAL_BUDGET_EXHAUSTED", second.error_code)
        self.assertEqual(1, transport.call_count)

    def test_plan_only_api_is_remembered_and_not_called_again(self):
        transport = Mock()
        refused = Mock(status_code=200)
        refused.json.return_value = {"code": 1, "msg": "该接口为龙虾套餐专属，请升级套餐后使用"}
        transport.return_value = refused
        provider = StockTodayProvider(token_loader=lambda: "synthetic-plan-token", post=transport, budget_path=self.path)
        first = provider._request_table("rt_idx_k", {"ts_code": "000001.SH"})
        second = provider._request_table("rt_idx_k", {"ts_code": "000001.SH"})
        self.assertEqual(("PLAN_NOT_ENTITLED", "PLAN_NOT_ENTITLED"), (first.error_code, second.error_code))
        self.assertEqual(1, transport.call_count)
        other = Mock(status_code=200)
        other.json.return_value = {"code": 0, "data": [{"ts_code": "600519.SH"}]}
        transport.return_value = other
        self.assertEqual("success", provider._request_table("rt_k", {"ts_code": "600519.SH"}).status)


class EntitlementScopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "budget.sqlite3"
        self.refused = Mock(status_code=200)
        self.refused.json.return_value = {"code": 1, "msg": "该接口为龙虾套餐专属，请升级套餐后使用"}

    def provider(self, token, transport):
        return StockTodayProvider(token_loader=lambda: token, post=transport, budget_path=self.path)

    def test_a_new_token_does_not_inherit_the_old_tokens_refusal(self):
        transport = Mock(return_value=self.refused)
        self.provider("synthetic-old-token", transport)._request_table("rt_idx_k", {"ts_code": "000001.SH"})
        ok = Mock(status_code=200)
        ok.json.return_value = {"code": 0, "data": [{"ts_code": "000001.SH", "close": 1.0}]}
        transport.return_value = ok
        result = self.provider("synthetic-new-token", transport)._request_table("rt_idx_k", {"ts_code": "000001.SH"})
        self.assertEqual("success", result.status)
        self.assertEqual(2, transport.call_count)

    def test_entitlement_report_refresh_bypasses_the_cache(self):
        from ym_stock_data.providers.stocktoday import ENTITLEMENT_PROBES, entitlement_report

        transport = Mock(return_value=self.refused)
        provider = self.provider("synthetic-plan-token", transport)
        provider._request_table("rt_idx_k", {"ts_code": "000001.SH"})
        cached = entitlement_report(provider)
        # Index realtime is outside the current plan: expected, not a failure.
        self.assertEqual("outside_plan", cached["apis"][0]["state"])
        calls_before = transport.call_count
        refreshed = entitlement_report(provider, refresh=True)
        self.assertEqual(calls_before + len(ENTITLEMENT_PROBES), transport.call_count)
        self.assertEqual(8, len(refreshed["token_fingerprint"]))
        self.assertNotIn("synthetic", str(refreshed))

    def test_only_stock_realtime_apis_are_required(self):
        from ym_stock_data.providers.stocktoday import entitlement_report

        ok = Mock(status_code=200)
        ok.json.return_value = {"code": 0, "data": [{"ts_code": "600519.SH", "close": 1.0}]}
        transport = Mock(side_effect=lambda *a, **kw: ok if kw["json"]["api_name"] in {"rt_k", "rt_min", "stk_limit"}
                         else self.refused)
        report = entitlement_report(self.provider("synthetic-stock-plan", transport))
        self.assertTrue(report["required_ok"])
        states = {row["api_name"]: row["state"] for row in report["apis"]}
        self.assertEqual("outside_plan", states["rt_idx_k"])
        self.assertEqual("entitled", states["rt_k"])

        transport.side_effect = None
        transport.return_value = self.refused
        report = entitlement_report(self.provider("synthetic-no-stock-plan", transport))
        self.assertFalse(report["required_ok"])
        self.assertEqual("not_entitled", {row["api_name"]: row["state"] for row in report["apis"]}["rt_k"])


class PreviousLadderTests(unittest.TestCase):
    def test_previous_ladder_reads_the_sealed_market_facts_run(self):
        from ym_stock_data.market_facts import MarketFactStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "facts.sqlite3"
        store = MarketFactStore(db)
        pools = {"zt": [{"code": "600001", "name": "甲", "limit_days": 2},
                        {"code": "600002", "name": "*ST乙", "limit_days": 1}],
                 "zb": [{"code": "600009", "name": "丙", "limit_days": 0}], "dt": []}
        store.ingest_limits("20260924", {
            "_meta": {"status": "success", "provider_used": "stocktoday",
                      "fetched_at": "2026-09-24T16:15:00+08:00"},
            "data": {"date": "20260924", "pools": pools, "zt_count": 2, "zb_count": 1,
                     "dt_count": 0, "_board_source": "limit_step_crosschecked"},
        })
        boards, broken, source = intraday_state._previous_ladder("20260924", db)
        self.assertEqual({"600001": 2}, boards)
        self.assertEqual(["600009"], broken)
        self.assertEqual("limit_step_crosschecked", source)
        self.assertEqual((None, None, None), intraday_state._previous_ladder("20260923", db))


class DataTimeMetaTests(unittest.TestCase):
    def test_holiday_snapshot_is_labelled_with_the_real_session_not_fetch_time(self):
        from ym_stock_data import api

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        now = at("2026-09-27T10:00:00")
        transport = Mock()
        response = Mock(status_code=200)
        response.json.return_value = {"code": 0, "data": [{
            "ts_code": "600519.SH", "name": "贵州茅台", "close": 1237.0, "pre_close": 1251.24,
            "open": 1250.01, "high": 1256.13, "low": 1231.05, "vol": 3123935, "amount": 3867310920,
            "updated_at": "2026-09-25T15:11:00.000"}]}
        transport.return_value = response
        clock = Mock(time=Mock(return_value=now.timestamp()), monotonic=Mock(return_value=0.0))
        provider = StockTodayProvider(token_loader=lambda: "synthetic-plan-token", post=transport,
                                      budget_path=Path(tmp.name) / "b.sqlite3", clock=clock)
        tencent = Mock()
        with patch.object(api, "_now_shanghai", return_value=now):
            result = api._query_with(
                "stock_snapshot", {"codes": ["600519"]},
                provider_loader=lambda name: provider if name == "stocktoday" else tencent,
                state_loader=lambda: Mock(active_breaker=Mock(return_value=None)),
            )
        meta = result["_meta"]
        self.assertEqual("stocktoday", meta["provider_used"])
        self.assertEqual("2026-09-24T15:00:00+08:00", meta["data_as_of"])
        self.assertEqual("fresh", meta["freshness"]["status"])
        self.assertEqual("2026-09-27T10:00:00+08:00", meta["fetched_at"])
        row = result["data"]["600519"]
        self.assertEqual("2026-09-24T15:00:00+08:00", row["quote_time"])
        self.assertEqual("2026-09-25T15:11:00+08:00", row["vendor_quote_time"])
        tencent.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
