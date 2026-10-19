"""DP-1a：板块日线与封单额（离线夹具；实网证据见 DP-1 REPORT）。

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
from ym_stock_data.routing import route_for

SECRET = "synthetic-stocktoday-test-token"


class _NoBreaker:
    @staticmethod
    def active_breaker(provider):
        return None


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
