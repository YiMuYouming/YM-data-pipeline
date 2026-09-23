import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from ym_stock_data import query
from ym_stock_data import api
from ym_stock_data.__main__ import main
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.routing import route_for
from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.providers.base import ProviderOutcome


SECRET = "synthetic-stocktoday-test-token"


class StockTodayTests(unittest.TestCase):
    def setUp(self):
        from ym_stock_data.providers.stocktoday import StockTodayProvider
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transport = Mock()
        self.response = Mock(status_code=200)
        self.response.json.return_value = {
            "code": 0, "columns": ["ts_code", "trade_date", "close"],
            "data": [{"ts_code": "600519.SH", "trade_date": "20260921", "close": 1252.57}],
            "total": 1,
        }
        self.transport.return_value = self.response
        self.provider = StockTodayProvider(
            token_loader=lambda: SECRET, post=self.transport,
            budget_path=Path(self.tmp.name) / "budget.sqlite3",
        )
        self.state = ProviderState(Path(self.tmp.name) / "state.sqlite3")
        self.addCleanup(patch.stopall)
        patch.object(api, "_STATE", self.state).start()
        patch.dict(api.PROVIDER_REGISTRY, {"stocktoday": self.provider}).start()

    def call(self, **kwargs):
        return query("stocktoday_data", api_name="daily", params={"ts_code": "600519.SH", "trade_date": "20260921"}, **kwargs)

    def test_public_dataset_and_bounded_https_transport(self):
        result = self.call()
        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual("stocktoday", result["_meta"]["provider_used"])
        self.assertEqual(1, result["_meta"]["quality"]["returned_count"])
        self.assertEqual(1252.57, result["data"]["items"][0]["close"])
        args, kwargs = self.transport.call_args
        self.assertEqual("https://tushare.citydata.club/daily", args[0])
        self.assertEqual("https://tushare.citydata.club/", kwargs["json"]["params"]["ts_type_name"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual((4, 15), kwargs["timeout"])
        self.assertEqual(SECRET, kwargs["json"]["token"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual("20260921", result["_meta"]["observation"]["latest_trade_date"])

    def test_tushare_envelope_supported_without_zip_truncation(self):
        self.response.json.return_value = {"code": 0, "data": {"fields": ["ts_code", "close"], "items": [["600519.SH", 100]]}}
        self.assertEqual(100, self.call()["data"]["items"][0]["close"])
        self.response.json.return_value["data"]["items"] = [["600519.SH"]]
        self.assertEqual("INVALID_RESPONSE", self.call()["_meta"]["attempts"][0]["error_code"])

    def test_native_sdk_envelope_accepts_record_items(self):
        self.response.json.return_value = {"code": 0, "data": {"fields": ["ts_code", "close"], "items": [{"ts_code": "600519.SH", "close": 100}]}}
        self.assertEqual(100, self.call()["data"]["items"][0]["close"])

    def test_empty_remains_empty_not_zero_price(self):
        self.response.json.return_value = {"code": 0, "data": [], "columns": [], "total": 0}
        self.assertEqual("empty", self.call()["_meta"]["status"])
        self.assertEqual(0, self.call()["_meta"]["quality"]["returned_count"])

    def test_partial_response_declares_truncation(self):
        self.response.json.return_value["total"] = 10
        result = self.call()
        self.assertTrue(result["data"]["truncated"])
        self.assertEqual("partial", result["_meta"]["quality"]["status"])
        self.assertEqual("degraded", result["_meta"]["status"])

    def test_raw_realtime_batch_detects_silent_missing_symbols(self):
        self.response.json.return_value = {"code": 0, "total": 1, "data": [{"ts_code": "600519.SH", "close": 100, "updated_at": "2099-01-01T10:00:00"}]}
        result = query("stocktoday_data", api_name="rt_k", params={"ts_code": "600519.SH,000001.SZ"})
        self.assertEqual(["000001.SZ"], result["_meta"]["observation"]["missing_codes"])
        self.assertEqual("degraded", result["_meta"]["status"])

    def test_realtime_market_maps_rt_idx_k_to_three_index_shape(self):
        self.response.json.return_value = {
            "code": 0,
            "total": 3,
            "data": [
                {
                    "ts_code": code,
                    "name": name,
                    "pre_close": 100,
                    "open": 101,
                    "high": 103,
                    "low": 99,
                    "close": close,
                    "amount": amount,
                    "trade_time": "2026-09-23",
                    "updated_at": "2026-09-23T10:00:00",
                }
                for code, name, close, amount in (
                    ("000001.SH", "上证指数", 102, 100000000),
                    ("399001.SZ", "深证成指", 98, 200000000),
                    ("399006.SZ", "创业板指", 101, 300000000),
                )
            ],
        }

        result = self.provider.call("realtime_market", {})

        self.assertEqual("success", result.status)
        self.assertEqual("rt_idx_k", self.transport.call_args.args[0].rsplit("/", 1)[-1])
        self.assertEqual(102, result.data["上证指数"])
        self.assertEqual(98, result.data["深证指数"])
        self.assertEqual(101, result.data["创业指数"])
        self.assertEqual("+2.00%", result.data["上证指数涨幅"])
        self.assertEqual("3.00亿", result.data["成交额"])

    def test_canonical_market_keeps_stocktoday_lunch_index_quotes(self):
        now = datetime.fromisoformat("2026-09-23T12:00:00+08:00")
        clock = Mock()
        clock.time.return_value = now.timestamp()
        clock.monotonic.return_value = 1.0
        self.provider.clock = clock
        self.response.json.return_value = {
            "code": 0, "total": 3,
            "data": [
                {
                    "ts_code": code, "pre_close": 100.0, "close": close,
                    "amount": 100000000.0,
                    "updated_at": "2026-09-23T11:30:00+08:00",
                }
                for code, close in (
                    ("000001.SH", 102.0),
                    ("399001.SZ", 98.0),
                    ("399006.SZ", 101.0),
                )
            ],
        }
        fallback = Mock()
        with patch.object(api, "_now_shanghai", return_value=now):
            result = api._query_with(
                "realtime_market", {},
                provider_loader=lambda name: self.provider if name == "stocktoday" else fallback,
                state_loader=lambda: self.state,
            )
        self.assertEqual("stocktoday", result["_meta"]["provider_used"], result["_meta"]["attempts"])
        self.assertEqual("fresh", result["data"]["_stocktoday"]["status"])
        self.assertEqual(0, result["data"]["_stocktoday"]["age_sec"])
        fallback.call.assert_not_called()

    def test_canonical_market_falls_back_after_quote_exceeds_sixty_trading_seconds(self):
        now = datetime.fromisoformat("2026-09-23T13:01:30+08:00")
        clock = Mock()
        clock.time.return_value = now.timestamp()
        clock.monotonic.return_value = 1.0
        self.provider.clock = clock
        self.response.json.return_value = {
            "code": 0, "total": 3,
            "data": [
                {
                    "ts_code": code, "pre_close": 100.0, "close": close,
                    "amount": 100000000.0,
                    "updated_at": "2026-09-23T11:30:00+08:00",
                }
                for code, close in (
                    ("000001.SH", 102.0),
                    ("399001.SZ", 98.0),
                    ("399006.SZ", 101.0),
                )
            ],
        }
        fallback = Mock()
        fallback.call.return_value = ProviderOutcome(
            "tencent", "success",
            data={"上证指数": 102.0, "深证指数": 98.0, "创业指数": 101.0},
        )
        with patch.object(api, "_now_shanghai", return_value=now):
            result = api._query_with(
                "realtime_market", {},
                provider_loader=lambda name: self.provider if name == "stocktoday" else fallback,
                state_loader=lambda: self.state,
            )
        self.assertEqual("tencent", result["_meta"]["provider_used"])
        self.assertEqual("QUALITY_STALE", result["_meta"]["attempts"][0]["error_code"])
        fallback.call.assert_called_once()

    def test_realtime_market_uses_rt_idx_tick_after_rt_idx_k_empty(self):
        empty = Mock(status_code=200)
        empty.json.return_value = {"code": 0, "total": 0, "columns": [], "data": []}
        tick = Mock(status_code=200)
        tick.json.return_value = {
            "code": 0,
            "total": 3,
            "data": [
                {"ts_code": code, "pre_close": 100, "close": close, "amount": 1}
                for code, close in (
                    ("000001.SH", 102),
                    ("399001.SZ", 98),
                    ("399006.SZ", 101),
                )
            ],
        }
        self.transport.side_effect = [empty, tick]

        result = self.provider.call("realtime_market", {})

        self.assertEqual("success", result.status)
        self.assertEqual(
            ["rt_idx_k", "rt_idx_tick"],
            [call.args[0].rsplit("/", 1)[-1] for call in self.transport.call_args_list],
        )
        self.assertEqual(102, result.data["上证指数"])

    def test_limit_state_maps_stocktoday_limit_list_to_canonical_pool(self):
        self.response.json.return_value = {
            "code": 0,
            "total": 1,
            "data": [
                {
                    "ts_code": "600519.SH",
                    "trade_date": "20260923",
                    "name": "贵州茅台",
                    "close": 1400,
                    "pct_chg": 10,
                    "limit_times": 2,
                }
            ],
        }

        result = self.provider.call(
            "market_limit_state", {"date": "20260923", "limit_type": "U"}
        )

        self.assertEqual("success", result.status)
        self.assertEqual(1, result.data["zt_count"])
        self.assertEqual(0, result.data["dt_count"])
        self.assertEqual("limit_list_d", self.transport.call_args.kwargs["json"]["api_name"])
        self.assertEqual("U", self.transport.call_args.kwargs["json"]["params"]["limit_type"])

    def test_limit_board_maps_stocktoday_limit_list_to_canonical_rows(self):
        self.response.json.return_value = {
            "code": 0,
            "total": 1,
            "data": [
                {
                    "ts_code": "600519.SH",
                    "trade_date": "20260923",
                    "name": "贵州茅台",
                    "close": 1400,
                    "pct_chg": 10,
                    "limit_times": 2,
                }
            ],
        }

        result = self.provider.call(
            "market_limit_board", {"kind": "up", "date": "20260923"}
        )

        self.assertEqual("success", result.status)
        self.assertEqual("600519", result.data["items"][0]["code"])
        self.assertEqual("limit_list_d", self.transport.call_args.kwargs["json"]["api_name"])

    def test_limit_board_without_date_selects_latest_returned_trade_date(self):
        self.response.json.return_value = {
            "code": 0,
            "total": 2,
            "data": [
                {
                    "ts_code": "600519.SH",
                    "trade_date": "20260921",
                    "name": "旧日",
                    "close": 1390,
                    "pct_chg": 10,
                },
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20260922",
                    "name": "最新日",
                    "close": 10,
                    "pct_chg": 10,
                },
            ],
        }

        result = self.provider.call("market_limit_board", {"kind": "up"})

        self.assertEqual("success", result.status)
        self.assertEqual("20260922", result.data["date"])
        self.assertEqual("20260922", result.data["_stocktoday"]["selected_trade_date"])
        self.assertEqual(["000001"], [row["code"] for row in result.data["items"]])

    def test_hot_rank_uses_full_stocktoday_table_and_selects_latest_date(self):
        self.response.json.return_value = {
            "code": 0,
            "total": 2,
            "data": [
                {"ts_code": "600519.SH", "ts_name": "旧", "rank": 2, "trade_date": "20260921"},
                {"ts_code": "600519.SH", "ts_name": "贵州茅台", "rank": 1, "trade_date": "20260922"},
            ],
        }

        result = self.provider.call("market_hot_rank", {"source": "ths", "limit": 1})

        self.assertEqual("success", result.status)
        self.assertEqual("600519.SH", result.data["items"][0]["ts_code"])
        self.assertEqual("20260922", result.data["trade_date"])
        self.assertEqual("ths_hot", self.transport.call_args.kwargs["json"]["api_name"])
        self.assertEqual("", self.transport.call_args.kwargs["json"]["fields"])
        self.assertNotIn("is_new", self.transport.call_args.kwargs["json"]["params"])

    def test_duplicate_quotes_are_flagged_and_newest_record_wins(self):
        rows = [{"ts_code": "600519.SH", "close": 100, "updated_at": "2026-09-22T10:00:00"}, {"ts_code": "600519.SH", "close": 90, "updated_at": "2026-09-21T10:00:00"}]
        self.response.json.return_value = {"code": 0, "data": {"fields": list(rows[0]), "items": rows}}
        result = query("stock_snapshot", codes=["600519"], source="stocktoday")
        self.assertEqual(100, result["data"]["600519"]["price"])
        self.assertEqual(1, result["_meta"]["observation"]["duplicate_code_count"])
        self.assertEqual("degraded", result["_meta"]["status"])

    def test_snapshot_rejects_nonfinite_price(self):
        self.response.json.return_value = {"code": 0, "total": 1, "data": [{"ts_code": "600519.SH", "close": float("nan")}]}
        self.assertEqual("error", query("stock_snapshot", codes=["600519"], source="stocktoday")["_meta"]["status"])

    def test_kline_does_not_accept_another_stock(self):
        self.response.json.return_value = {"code": 0, "total": 1, "data": [{"ts_code": "000001.SZ", "trade_date": "20260921", "open": 10, "high": 11, "low": 9, "close": 10}]}
        self.assertEqual("error", query("stock_kline", code="600519", period="daily", count=1, source="stocktoday")["_meta"]["status"])

    def test_irregular_minute_bars_cannot_be_used_as_closed_bars(self):
        self.response.json.return_value = {"code": 0, "total": 1, "data": [{"ts_code": "600519.SH", "trade_time": "2026-09-21 11:02:37", "open": 10, "high": 11, "low": 9, "close": 10}]}
        result = query("stock_kline", code="600519", period="60m", count=1, source="stocktoday")
        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("unverified", result["_meta"]["observation"]["bar_time_convention"])
        self.assertIsNone(result["data"]["bars"][0]["bar_closed"])

    def test_capability_manifest_exposes_opt_in_source(self):
        from ym_stock_data.v2.capabilities import capability_manifest
        item = capability_manifest()["providers"]["stocktoday"]
        self.assertTrue(item["registered"])
        self.assertTrue(item["default_route"])
        self.assertIn("realtime_market", item["automatic_fallback_intents"])
        self.assertIn("stock_snapshot", item["automatic_fallback_intents"])
        self.assertIn("stock_kline", item["automatic_fallback_intents"])

    def test_ignored_date_filter_is_not_reported_as_success(self):
        self.response.json.return_value = {"code": 0, "total": 1, "data": [{"month": "195112", "nt_val": 100}]}
        result = query("stocktoday_data", api_name="cn_cpi", params={"m": "202608"})
        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertIn("m", result["_meta"]["observation"]["filter_violations"])

    def test_limit_and_max_rows_preserve_coverage(self):
        self.response.json.return_value = {"code": 0, "total": 2, "data": [{"ts_code": "600519.SH"}, {"ts_code": "000001.SZ"}]}
        r = self.call(max_rows=1)
        self.assertEqual(1, len(r["data"]["items"]))
        self.assertEqual(2, r["data"]["total"])
        self.assertEqual("degraded", r["_meta"]["status"])

    def test_errors_never_echo_server_or_request_secrets(self):
        self.response.json.return_value = {"code": 40203, "msg": "无权限 " + SECRET}
        result = self.call()
        self.assertEqual("error", result["_meta"]["status"])
        self.assertEqual("AUTH_DENIED", result["_meta"]["attempts"][0]["error_code"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.transport.side_effect = requests.Timeout(SECRET)
        self.assertEqual("timeout", self.call()["_meta"]["attempts"][0]["status"])

    def test_missing_auth_does_not_call_network(self):
        self.provider.token_loader = lambda: None
        self.assertEqual("AUTH_MISSING", self.call()["_meta"]["attempts"][0]["error_code"])
        self.transport.assert_not_called()

    def test_redirect_is_not_followed_and_rate_limit_is_explicit(self):
        self.response.status_code = 302
        self.assertEqual("REDIRECT_BLOCKED", self.call()["_meta"]["attempts"][0]["error_code"])
        self.response.status_code = 429
        self.assertEqual("RATE_LIMITED", self.call()["_meta"]["attempts"][0]["error_code"])

    def test_http_auth_and_rate_statuses_win_over_non_json_body(self):
        for status_code, expected_error in ((401, "AUTH_DENIED"), (403, "AUTH_DENIED"), (429, "RATE_LIMITED")):
            with self.subTest(status_code=status_code):
                self.response.status_code = status_code
                self.response.json.side_effect = ValueError("plain-text gateway response")
                outcome = self.provider.call(
                    "stocktoday_data",
                    {"api_name": "daily", "params": {"ts_code": "600519.SH"}},
                )
                self.assertEqual(expected_error, outcome.error_code)
                self.assertEqual(status_code, outcome.provenance["http_status"])
                self.response.json.side_effect = None

    def test_unapproved_methods_and_credentials_in_params_rejected_before_network(self):
        for kwargs in [
            {"api_name": "place_order"},
            {"api_name": "daily", "params": {"token": SECRET}},
            {"api_name": "daily", "params": {"gateway": "http://elsewhere"}},
            {"api_name": "daily", "params": {"ts_code": [SECRET]}},
            {"api_name": "daily", "max_rows": 10001},
            {"api_name": "rt_min", "params": {"ts_code": "600519.SH", "freq": "60min"}},
        ]:
            with self.subTest(kwargs=list(kwargs)):
                with self.assertRaises(ValueError):
                    query("stocktoday_data", **kwargs)
        self.transport.assert_not_called()

    def test_loader_does_not_consume_official_tushare_token(self):
        from ym_stock_data.providers.stocktoday_auth import load_token
        with patch.dict("os.environ", {"TUSHARE_TOKEN": SECRET}, clear=True), \
             patch("ym_stock_data.providers.stocktoday_auth._CACHED_TOKEN", None), \
             patch("ym_stock_data.providers.stocktoday_auth._keyring") as ring:
            ring.return_value.get_password.return_value = None
            self.assertIsNone(load_token())

    def test_loader_uses_macos_keychain_cli_when_keyring_package_is_missing(self):
        from ym_stock_data.providers.stocktoday_auth import load_token
        with patch.dict("os.environ", {}, clear=True), \
             patch("ym_stock_data.providers.stocktoday_auth._CACHED_TOKEN", None), \
             patch("ym_stock_data.providers.stocktoday_auth._keyring", side_effect=ModuleNotFoundError), \
             patch("ym_stock_data.providers.stocktoday_auth._security_token", return_value=SECRET) as security:
            self.assertEqual(SECRET, load_token())
            self.assertEqual(SECRET, load_token())
        security.assert_called_once_with()

    def test_auth_stdin_and_doctor_never_output_token(self):
        from ym_stock_data.providers.stocktoday_auth import save_token
        with patch("ym_stock_data.providers.stocktoday_auth._keyring") as ring:
            save_token(SECRET)
            ring.return_value.set_password.assert_called_once_with("ym-stock-data/stocktoday", "api-token", SECRET)
        with patch("ym_stock_data.providers.stocktoday_auth.save_token") as save, patch("sys.stdin", io.StringIO(SECRET)), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(0, main(["auth", "set-stocktoday", "--stdin"]))
            save.assert_called_once_with(SECRET)
            self.assertNotIn(SECRET, output.getvalue())
        self.assertEqual("configured_unverified", self.provider.probe()["status"])
        self.transport.assert_not_called()

    def test_budget_is_cross_instance_and_fails_fast(self):
        from ym_stock_data.providers.stocktoday import RequestBudget
        path = Path(self.tmp.name) / "limit.sqlite3"
        one = RequestBudget(path, per_minute=2, per_day=3)
        two = RequestBudget(path, per_minute=2, per_day=3)
        self.assertTrue(one.acquire(now=100))
        self.assertTrue(two.acquire(now=101))
        self.assertFalse(one.acquire(now=102))
        self.assertTrue(two.acquire(now=162))
        self.assertFalse(one.acquire(now=223))
        self.assertNotIn(SECRET.encode(), path.read_bytes())

    def test_explicit_snapshot_preserves_bse_and_reports_stale_timestamp(self):
        self.response.json.return_value = {"code": 0, "total": 1, "columns": ["ts_code", "close", "pre_close", "updated_at", "vol", "amount"], "data": [{"ts_code": "920931.BJ", "close": 20, "pre_close": 10, "updated_at": "2024-01-01T10:00:00", "vol": 100, "amount": 2000}]}
        result = query("stock_snapshot", codes=["920931"], source="stocktoday")
        self.assertEqual("920931.BJ", self.transport.call_args.kwargs["json"]["params"]["ts_code"])
        self.assertEqual(20, result["data"]["920931"]["price"])
        self.assertEqual(0.2, result["data"]["920931"]["amount_wan"])
        self.assertEqual("stale", result["_meta"]["observation"]["status"])
        self.assertEqual("degraded", result["_meta"]["status"])

    def test_canonical_snapshot_stocktoday_observation_uses_exchange_sessions(self):
        examples = (
            ("2026-09-23T12:00:00+08:00", "2026-09-23T11:30:00+08:00", True),
            ("2026-09-23T17:00:00+08:00", "2026-09-23T15:00:00+08:00", True),
            ("2026-09-26T12:00:00+08:00", "2026-09-24T15:00:00+08:00", True),
            ("2026-09-23T13:03:00+08:00", "2026-09-23T11:30:00+08:00", False),
        )
        for now_text, quote_time, fresh in examples:
            with self.subTest(now=now_text):
                now = datetime.fromisoformat(now_text)
                clock = Mock()
                clock.time.return_value = now.timestamp()
                clock.monotonic.return_value = 1.0
                self.provider.clock = clock
                self.response.json.return_value = {
                    "code": 0, "total": 1,
                    "data": [{
                        "ts_code": "600519.SH", "close": 100.0, "pre_close": 99.0,
                        "open": 99.0, "high": 101.0, "low": 98.0,
                        "vol": 1000.0, "amount": 100000.0,
                        "updated_at": quote_time,
                    }],
                }
                fallback = Mock()
                fallback.call.return_value = ProviderOutcome("tencent", "success", data={
                    "600519": {
                        "code": "600519", "price": 100.0, "last_close": 99.0,
                        "open": 99.0, "high": 101.0, "low": 98.0,
                        "volume": 1000.0, "amount": 100000.0,
                        "quote_time": now_text,
                    },
                })
                with patch.object(api, "_now_shanghai", return_value=now):
                    result = api._query_with(
                        "stock_snapshot", {"codes": ["600519"]},
                        provider_loader=lambda name: self.provider if name == "stocktoday" else fallback,
                        state_loader=lambda: self.state,
                    )
                if fresh:
                    self.assertEqual("stocktoday", result["_meta"]["provider_used"], result["_meta"]["attempts"])
                    self.assertEqual("success", result["_meta"]["status"])
                    self.assertEqual("fresh", result["data"]["_stocktoday"]["status"])
                    self.assertEqual(0, result["data"]["_stocktoday"]["age_sec"])
                    self.assertEqual(quote_time, result["data"]["600519"]["quote_time"])
                    fallback.call.assert_not_called()
                else:
                    self.assertEqual("tencent", result["_meta"]["provider_used"])
                    self.assertEqual("degraded", result["_meta"]["status"])
                    self.assertEqual("QUALITY_STALE", result["_meta"]["attempts"][0]["error_code"])
                    fallback.call.assert_called_once()

    def test_snapshot_missing_timestamp_never_claims_fresh(self):
        self.response.json.return_value = {"code": 0, "data": [{"ts_code": "600519.SH", "close": 100, "pre_close": 99}], "columns": ["ts_code", "close", "pre_close"], "total": 1}
        result = query("stock_snapshot", codes=["600519"], source="stocktoday")
        self.assertEqual("unknown", result["_meta"]["observation"]["status"])
        self.assertEqual("degraded", result["_meta"]["status"])

    def test_default_routes_stay_free_and_new_source_is_opt_in(self):
        self.assertEqual(("stocktoday",), route_for("stock_snapshot", {"source": "stocktoday"}).providers)
        self.assertEqual("stocktoday", route_for("stock_snapshot", {}).providers[0])
        self.assertEqual("stocktoday", route_for("realtime_market", {}).providers[0])
        self.assertEqual("stocktoday", route_for("stock_kline", {"period": "daily"}).providers[0])
        self.assertNotIn("stocktoday", route_for("review_sentiment", {"query": "筛选"}).providers)
        with self.assertRaises(ValueError):
            query("stock_snapshot", codes=["600519"], source="unknown")

    def test_daily_bars_units_order_and_count(self):
        self.response.json.return_value = {"code": 0, "data": [{"ts_code": "600519.SH", "trade_date": d, "open": 10, "high": 11, "low": 9, "close": 10, "vol": 2, "amount": 3} for d in ["20260921", "20260918"]], "total": 2}
        result = query("stock_kline", code="600519", period="daily", count=2, source="stocktoday")
        self.assertEqual("daily", self.transport.call_args.kwargs["json"]["api_name"])
        self.assertEqual("2026-09-18", result["data"]["bars"][0]["datetime"])
        self.assertEqual(3000, result["data"]["bars"][0]["amount"])
        self.assertEqual(200, result["data"]["bars"][0]["volume"])
        self.assertEqual("none", result["data"]["adjustment"])

    def test_minute_bars_without_count_are_not_truncated_to_sixty(self):
        rows = [
            {
                "ts_code": "600519.SH",
                "trade_time": f"2026-09-22 09:{30 + index // 2:02d}:{(index % 2) * 30:02d}",
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.1,
                "vol": 100,
                "amount": 10000,
            }
            for index in range(61)
        ]
        self.response.json.return_value = {
            "code": 0,
            "data": rows,
            "total": len(rows),
        }

        result = query(
            "stock_kline",
            code="600519",
            period="5m",
            source="stocktoday",
        )

        self.assertEqual(61, len(result["data"]["bars"]))

    def test_stock_minute_date_range_uses_stocktoday_datetime_window(self):
        self.response.json.return_value = {"code": 0, "data": [], "total": 0}

        query(
            "stock_kline",
            code="600519",
            period="5m",
            start_date="20260922",
            end_date="20260922",
            source="stocktoday",
        )

        payload = self.transport.call_args.kwargs["json"]
        self.assertEqual("stk_mins", payload["api_name"])
        self.assertEqual(
            "2026-09-22 09:00:00",
            payload["params"]["start_date"],
        )
        self.assertEqual(
            "2026-09-22 15:10:00",
            payload["params"]["end_date"],
        )

    def test_index_minute_date_range_uses_stocktoday_datetime_window(self):
        self.response.json.return_value = {"code": 0, "data": [], "total": 0}

        query(
            "index_kline",
            index_code="000001.SH",
            period="5m",
            start_date="20260922",
            end_date="20260922",
        )

        payload = self.transport.call_args.kwargs["json"]
        self.assertEqual("idx_mins", payload["api_name"])
        self.assertEqual(
            "2026-09-22 09:00:00",
            payload["params"]["start_date"],
        )
        self.assertEqual(
            "2026-09-22 15:10:00",
            payload["params"]["end_date"],
        )

    def test_realtime_industry_flow_rejects_non_current_stocktoday_rows(self):
        yesterday = (datetime.now(TZ_SHANGHAI) - timedelta(days=1)).strftime("%Y%m%d")
        self.response.json.return_value = {
            "code": 0,
            "data": [
                {
                    "trade_date": yesterday,
                    "industry": "行业",
                    "net_amount": 1.0,
                }
            ],
            "total": 1,
        }

        result = self.provider.call("industry_flow", {"current_session": True})

        self.assertEqual("empty", result.status)
        self.assertEqual([], result.data["items"])

    def test_realtime_industry_flow_accepts_only_current_stocktoday_rows(self):
        today = datetime.now(TZ_SHANGHAI).strftime("%Y%m%d")
        self.response.json.return_value = {
            "code": 0,
            "data": [
                {
                    "trade_date": today,
                    "industry": "行业",
                    "net_amount": 1.0,
                }
            ],
            "total": 1,
        }

        result = self.provider.call("industry_flow", {"current_session": True})

        self.assertEqual("success", result.status)
        self.assertEqual(today, result.data["trade_date"])
        self.assertEqual(1, len(result.data["items"]))

    def test_realtime_legacy_hot_rank_rejects_non_current_stocktoday_rows(self):
        yesterday = (datetime.now(TZ_SHANGHAI) - timedelta(days=1)).strftime("%Y%m%d")
        self.response.json.return_value = {
            "code": 0,
            "data": [{"trade_date": yesterday, "ts_code": "600519"}],
            "total": 1,
        }

        result = self.provider.call("legacy_hot_rank", {"current_session": True})

        self.assertEqual("empty", result.status)
        self.assertEqual([], result.data["items"])


if __name__ == "__main__":
    unittest.main()
