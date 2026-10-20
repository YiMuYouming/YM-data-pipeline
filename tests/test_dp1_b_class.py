"""DP-1a：板块日线与封单额；DP-1b：分钟 K 的显式用途 PyTDX 后备（J20-1）。

1. 分钟 stock_kline（1m/5m/15m/60m）：默认路由保持 stocktoday -> tencent；
   只有显式 use_case ∈ {zj_collection, zj_replay, zj_shadow} 才允许
   stocktoday -> pytdx -> tencent，且不得用于其它 intent/日线。
2. 板块日线：board_daily_kline（ths_daily），成交额 = vol(手)*100*avg_price，字段级 derived。
3. 封单额：market_limit_board 透出 fd_amount / last_time / first_time / open_times / limit_times。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ym_stock_data import api, intent_registry, provider_policy, query, routing
from ym_stock_data.provider_policy import CompiledPolicy
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.providers.local import LocalProvider
from ym_stock_data.routing import route_for

SECRET = "synthetic-stocktoday-test-token"
MINUTES = ("1m", "5m", "15m", "60m")
EVIDENCE_USE_CASES = ("zj_collection", "zj_replay", "zj_shadow")
DEFAULT_MINUTE = ("stocktoday", "tencent")
EVIDENCE_MINUTE = ("stocktoday", "pytdx", "tencent")


class _NoBreaker:
    @staticmethod
    def active_breaker(provider):
        return None


def _refusing_loader(seen):
    """stocktoday 恒 PLAN_NOT_ENTITLED；其余用真实 LocalProvider（pytdx/tencent 被 patch）。"""

    def loader(name):
        seen.append(name)
        if name == "stocktoday":
            class _Refuse:
                def call(self, intent, params):
                    return ProviderOutcome(provider="stocktoday", status="incompatible",
                                           error_code="PLAN_NOT_ENTITLED")
            return _Refuse()
        return LocalProvider(name)

    return loader


PYTDX_BARS = {
    "code": "300885",
    "bars": [
        {"time": "2026-10-19 09:35", "open": 10.0, "high": 10.2, "low": 9.9,
         "close": 10.1, "vol": 12000.0, "amount": 121000.0},
        {"time": "2026-10-19 09:40", "open": 10.1, "high": 10.3, "low": 10.0,
         "close": 10.2, "vol": 8000.0, "amount": 81000.0},
    ],
}
TENCENT_BARS = [{"datetime": "2026-10-19 09:35:00", "open": 1, "high": 1, "low": 1,
                 "close": 1, "volume": 1.0, "amount": 1.0}]


class MinuteKlineEvidenceUseCaseTests(unittest.TestCase):
    def test_default_minute_route_unchanged_and_has_no_pytdx(self):
        for period in MINUTES:
            with self.subTest(period=period):
                self.assertEqual(DEFAULT_MINUTE, route_for("stock_kline", {"period": period}).providers)
        self.assertEqual(("stocktoday", "tencent"),
                         route_for("stock_kline", {"period": "daily"}).providers)

    def test_default_route_inventory_has_no_pytdx(self):
        for spec in routing.all_route_specs():
            with self.subTest(intent=spec.intent, providers=spec.providers):
                self.assertNotIn("pytdx", spec.providers)

    def test_evidence_use_cases_route_stocktoday_then_pytdx_then_tencent(self):
        for use_case in EVIDENCE_USE_CASES:
            for period in MINUTES:
                with self.subTest(use_case=use_case, period=period):
                    self.assertEqual(
                        EVIDENCE_MINUTE,
                        route_for("stock_kline", {"period": period, "use_case": use_case}).providers,
                    )
        # 日/周/月即使带 evidence use_case 也不变（且 API 会拒绝）
        for period in ("daily", "weekly", "monthly"):
            with self.subTest(period=period):
                self.assertEqual(
                    ("stocktoday", "tencent"),
                    route_for("stock_kline", {"period": period, "use_case": "zj_shadow"}).providers,
                )

    def test_evidence_use_case_rejected_outside_stock_kline_minutes(self):
        for intent, params in (
            ("stock_kline", {"code": "300885", "period": "daily", "use_case": "zj_shadow"}),
            ("stock_snapshot", {"codes": ["300885"], "use_case": "zj_shadow"}),
        ):
            with self.subTest(intent=intent), self.assertRaises(ValueError):
                api._query_with(intent, params, provider_loader=lambda name: LocalProvider(name),
                                state_loader=lambda: _NoBreaker())

    def test_unknown_use_case_rejected(self):
        with self.assertRaises(ValueError):
            api._query_with("stock_kline",
                            {"code": "300885", "period": "5m", "use_case": "purpose_typo"},
                            provider_loader=lambda name: LocalProvider(name),
                            state_loader=lambda: _NoBreaker())

    def test_default_minute_request_never_attempts_pytdx(self):
        seen = []
        with patch("ym_stock_data.providers.local.pytdx.fetch_kline") as fk, \
                patch("ym_stock_data.providers.local.pytdx._fetch_tencent_kline",
                      return_value=TENCENT_BARS) as tk:
            result = api._query_with("stock_kline",
                                     {"code": "300885", "period": "5m", "count": 1},
                                     provider_loader=_refusing_loader(seen),
                                     state_loader=lambda: _NoBreaker())
        self.assertEqual(["stocktoday", "tencent"], seen)
        self.assertEqual("tencent", result["_meta"]["provider_used"])
        fk.assert_not_called()
        tk.assert_called_once()
        self.assertNotIn("evidence_use_case", result["_meta"])

    def test_evidence_use_case_not_entitled_falls_back_to_pytdx(self):
        seen = []
        with patch("ym_stock_data.providers.local.pytdx.fetch_kline", return_value=PYTDX_BARS) as fk, \
                patch("ym_stock_data.providers.local.pytdx._fetch_tencent_kline") as tk:
            result = api._query_with(
                "stock_kline",
                {"code": "300885", "period": "5m", "count": 2, "use_case": "zj_shadow"},
                provider_loader=_refusing_loader(seen),
                state_loader=lambda: _NoBreaker(),
            )
        meta = result["_meta"]
        self.assertEqual(["stocktoday", "pytdx"], seen)
        self.assertEqual("pytdx", meta["provider_used"])
        fk.assert_called_once()
        tk.assert_not_called()
        self.assertEqual(
            ["PLAN_NOT_ENTITLED"],
            [a.get("error_code") for a in meta["attempts"] if a.get("provider") == "stocktoday"],
        )
        bars = result["data"]["bars"]
        self.assertEqual(2, len(bars))
        self.assertEqual(12000.0, bars[0]["volume"])  # pytdx 分钟 vol 已是股，不再 *100
        self.assertEqual("share", result["data"]["volume_unit"])
        self.assertEqual("CNY", result["data"]["amount_unit"])
        # 降级/不等价/审计用途标记必须保留，不能伪装成 normal
        self.assertEqual("degraded", meta["status"])
        self.assertEqual("partial", meta["quality"]["status"])
        self.assertEqual("unknown", meta["quality"]["semantic_equivalence"])
        self.assertEqual("zj_shadow", meta["evidence_use_case"])
        self.assertNotEqual("stocktoday", meta["provider_used"])

    def test_evidence_pytdx_failure_still_reaches_tencent_last(self):
        seen = []
        with patch("ym_stock_data.providers.local.pytdx.fetch_kline",
                   return_value={"error": "x", "error_type": "PYTDX_DIRECT_UNAVAILABLE",
                                 "_source": "pytdx"}), \
                patch("ym_stock_data.providers.local.pytdx._fetch_tencent_kline",
                      return_value=TENCENT_BARS):
            result = api._query_with("stock_kline",
                                     {"code": "300885", "period": "5m", "count": 1,
                                      "use_case": "zj_replay"},
                                     provider_loader=_refusing_loader(seen),
                                     state_loader=lambda: _NoBreaker())
        self.assertEqual(["stocktoday", "pytdx", "tencent"], seen)
        self.assertEqual("tencent", result["_meta"]["provider_used"])

    def test_packaged_policy_default_minute_order_unchanged(self):
        policy = provider_policy.load_policy()
        for cap in ("stock_kline_60m", "stock_kline_15m", "stock_kline_5m"):
            with self.subTest(cap=cap):
                self.assertEqual(["stocktoday", "tencent"],
                                 policy["capabilities"][cap]["provider_order"])
        # 1m 在 policy document 中仍无条目（默认路由不受影响；证据用途走显式 route）
        self.assertNotIn("stock_kline_1m", policy["capabilities"])

    def test_code_side_1m_capability_is_covered(self):
        self.assertIn("stock_kline_1m", provider_policy.CAPABILITIES)
        self.assertIn("pytdx", provider_policy._CAPABILITY_PROVIDER_ALLOWLIST["stock_kline_1m"])
        self.assertEqual(frozenset({"stk_mins"}), provider_policy._CAPABILITY_METHODS["stock_kline_1m"])
        self.assertEqual(300, provider_policy._BASE_MAX_AGE_SEC["stock_kline_1m"])
        self.assertEqual("1MIN", provider_policy._MINUTE_CAPABILITY_FREQ["stock_kline_1m"])


class _StockTodayCase(unittest.TestCase):
    def setUp(self):
        from ym_stock_data.providers.stocktoday import StockTodayProvider
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transport = Mock()
        self.response = Mock(status_code=200)
        self.transport.return_value = self.response
        self.provider = StockTodayProvider(
            token_loader=lambda: SECRET, post=self.transport,
            budget_path=Path(self.tmp.name) / "budget.sqlite3",
        )
        self.state = ProviderState(Path(self.tmp.name) / "state.sqlite3")
        self.addCleanup(patch.stopall)
        patch.object(api, "load_compiled_policy",
                     return_value=CompiledPolicy(None, "inactive", None)).start()
        patch.object(api, "_STATE", self.state).start()
        patch.dict(api.PROVIDER_REGISTRY, {"stocktoday": self.provider}).start()


# 夹具说明（一致性核对，实网 2026-10-19，见 DP-1 REPORT 与 live/）：
# 881117.TI 当日 vol=25308329 手、avg_price=16.8049 -> 25308329*100*16.8049 = 4.2531e10 元
# = 425.3 亿，与 sector_index(881117) 同日 turnover_yi=425.31 一致（差 < 0.01 亿）。
_BOARD_ROWS = [
    {"ts_code": "881117.TI", "trade_date": "20261019", "open": 16.5, "high": 17.1,
     "low": 16.4, "close": 16.9, "pre_close": 16.6, "avg_price": 16.8049,
     "change": 0.3, "pct_change": 1.8, "vol": 25308329.0, "turnover_rate": 1.2,
     "total_mv": None, "float_mv": None},
    {"ts_code": "881117.TI", "trade_date": "20261016", "open": 16.4, "high": 16.7,
     "low": 16.3, "close": 16.6, "pre_close": 16.5, "avg_price": None,
     "change": 0.1, "pct_change": 0.6, "vol": 20000000.0, "turnover_rate": 1.0,
     "total_mv": None, "float_mv": None},
]


class BoardDailyKlineTests(_StockTodayCase):
    def test_intent_is_registered_and_routed_to_stocktoday_ths_daily(self):
        resolved = intent_registry.resolve_intent("查板块日线", codes=["881117.TI"])
        self.assertEqual("board_daily_kline", resolved["intent"])
        spec = route_for("board_daily_kline", {"codes": ["881117.TI"]})
        self.assertEqual(("stocktoday",), spec.providers)
        self.assertEqual(frozenset({"ths_daily"}),
                         provider_policy._CAPABILITY_METHODS["board_daily_kline"])

    def test_returns_ohlc_and_derived_amount_with_field_provenance(self):
        self.response.json.return_value = {"code": 0, "total": 2, "data": _BOARD_ROWS}
        result = query("board_daily_kline", codes=["881117.TI"],
                       start_date="20261016", end_date="20261019")
        self.assertEqual("success", result["_meta"]["status"], result["_meta"])
        self.assertEqual("ths_daily", self.transport.call_args.kwargs["json"]["api_name"])
        self.assertEqual("881117.TI",
                         self.transport.call_args.kwargs["json"]["params"]["ts_code"])
        board = result["data"]["boards"][0]
        self.assertEqual("881117.TI", board["board_id"])
        bars = board["bars"]
        self.assertEqual(["20261016", "20261019"], [b["trade_date"] for b in bars])  # 升序
        last = bars[-1]
        for key in ("open", "high", "low", "close", "pre_close", "pct_change", "vol", "avg_price"):
            self.assertEqual(_BOARD_ROWS[0][key], last[key], key)
        self.assertAlmostEqual(25308329.0 * 100 * 16.8049, last["amount"], places=2)
        self.assertAlmostEqual(425.31, last["amount"] / 1e8, delta=0.01)  # 对 sector_index.turnover_yi
        self.assertTrue(last["amount_derived"])
        # 缺 avg_price：null，不补 0
        self.assertIsNone(bars[0]["amount"])
        self.assertFalse(bars[0]["amount_derived"])
        prov = result["data"]["field_provenance"]["amount"]
        self.assertTrue(prov["derived"])
        self.assertEqual("vol(手)*100*avg_price", prov["formula"])
        self.assertEqual("CNY", prov["unit"])
        self.assertEqual("20261019", result["data"]["data_as_of"])

    def test_bare_code_gets_ti_suffix_and_non_board_code_is_rejected(self):
        self.response.json.return_value = {"code": 0, "total": 1, "data": _BOARD_ROWS[:1]}
        query("board_daily_kline", codes=["881117"])
        self.assertEqual("881117.TI",
                         self.transport.call_args.kwargs["json"]["params"]["ts_code"])
        with self.assertRaises(ValueError):
            api._validate_params("board_daily_kline", {"codes": ["600519"]})

    def test_empty_upstream_is_reported_missing_not_filled(self):
        self.response.json.return_value = {"code": 0, "total": 0, "data": []}
        result = query("board_daily_kline", codes=["881117"])
        self.assertNotEqual("success", result["_meta"]["status"])
        # 三次空读后才接受为空；missing 与 board_id 同为带 .TI 的写法
        self.assertEqual(3, self.transport.call_count)
        self.assertEqual(["881117.TI"], result["data"]["missing"])

    def test_spurious_empty_table_is_retried_then_succeeds(self):
        empty = Mock(status_code=200)
        empty.json.return_value = {"code": 0, "total": 0, "data": []}
        full = Mock(status_code=200)
        full.json.return_value = {"code": 0, "total": 1, "data": _BOARD_ROWS[:1]}
        self.transport.side_effect = [empty, empty, full]
        result = query("board_daily_kline", codes=["881117.TI"])
        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual(3, self.transport.call_count)
        self.assertEqual(2, result["data"]["_stocktoday"]["empty_reads"])

    def test_meta_data_as_of_is_the_latest_trade_date(self):
        self.response.json.return_value = {"code": 0, "total": 2, "data": _BOARD_ROWS}
        result = query("board_daily_kline", codes=["881117.TI"])
        self.assertTrue(str(result["_meta"]["data_as_of"]).startswith("2026-10-19"),
                        result["_meta"]["data_as_of"])


class LimitBoardSealAmountTests(_StockTodayCase):
    def test_limit_row_exposes_fd_amount_and_times_without_changing_old_fields(self):
        self.response.json.return_value = {
            "code": 0, "total": 2,
            "data": [
                {"ts_code": "600519.SH", "trade_date": "20261019", "name": "贵州茅台",
                 "close": 1400, "pct_chg": 10, "limit_times": 2, "open_times": 0,
                 "first_time": "092500", "last_time": "093000", "fd_amount": 123456789.5,
                 "industry": "白酒"},
                {"ts_code": "000001.SZ", "trade_date": "20261019", "name": "平安",
                 "close": 10, "pct_chg": 10, "limit_times": 1, "open_times": 1,
                 "first_time": "100000", "last_time": None, "fd_amount": None},
            ],
        }
        result = self.provider.call("market_limit_board", {"kind": "up", "date": "20261019"})
        first, second = result.data["items"]
        # 原字段不变
        self.assertEqual("600519", first["code"])
        self.assertEqual(2, first["limit_days"])
        self.assertEqual(0, first["break_times"])
        self.assertEqual("092500", first["first_limit_time"])
        self.assertEqual("093000", first["last_limit_time"])
        # 新增字段
        self.assertEqual(123456789.5, first["fd_amount"])
        self.assertEqual("093000", first["last_time"])
        self.assertEqual("092500", first["first_time"])
        self.assertEqual(0, first["open_times"])
        self.assertEqual(2, first["limit_times"])
        # 缺失即 null，不补 0
        self.assertIsNone(second["fd_amount"])
        self.assertIsNone(second["last_time"])
        sent = self.transport.call_args.kwargs["json"]["fields"]
        self.assertIn("fd_amount", sent.split(","))


if __name__ == "__main__":
    unittest.main()
