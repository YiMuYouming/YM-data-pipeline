import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from ym_stock_data import api
from ym_stock_data.api import query
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.providers.local import LocalProvider
from ym_stock_data.providers.stocktoday import RequestBudget
from ym_stock_data.routing import route_for
from ym_stock_data.sources import pytdx
from ym_stock_data.sources import eastmoney_index
from ym_stock_data.sources import eastmoney_stock
from ym_stock_data.sources import sina_index
from ym_stock_data.sources import tencent
from ym_stock_data.fetch import fetch


def _outcome(provider, status, data=None, error_code=None):
    return ProviderOutcome(
        provider=provider,
        status=status,
        data=data,
        error_code=error_code,
    )


def _full_index():
    return {
        "上证指数": 3200.0,
        "深证指数": 10000.0,
        "创业指数": 2000.0,
    }


def _full_snapshot(*codes):
    return {
        code: {
            "code": code,
            "price": 10.0,
            "last_close": 9.5,
            "open": 9.8,
            "high": 10.2,
            "low": 9.7,
            "volume": 1000,
            "amount": 10000.0,
            "quote_time": datetime.now().isoformat(timespec="seconds"),
            "change_pct": 1.0,
        }
        for code in codes
    }


def _full_kline(adjustment="none"):
    return {
        "bars": [
            {
                "datetime": "2026-09-22 15:00:00",
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.5,
                "volume": 1000,
                "amount": 10500.0,
            }
        ],
        "adjustment": adjustment,
        "volume_unit": "share",
        "amount_unit": "CNY",
    }


class _FakeProvider:
    def __init__(self, provider, outcomes):
        self.provider = provider
        self.outcomes = list(outcomes)
        self.calls = []

    def call(self, intent, params):
        self.calls.append((intent, dict(params)))
        if not self.outcomes:
            return _outcome(self.provider, "empty", {})
        return self.outcomes.pop(0)


class CoreRepairTests(unittest.TestCase):
    def _run_with_fakes(self, intent, providers, **params):
        with tempfile.TemporaryDirectory() as tmp:
            state = ProviderState(Path(tmp) / "providers.sqlite3")
            return api._query_with(
                intent,
                params,
                provider_loader=lambda name: providers[name],
                state_loader=lambda: state,
            )

    def _public_query_with_providers(self, intent, providers, **params):
        with tempfile.TemporaryDirectory() as tmp:
            state = ProviderState(Path(tmp) / "providers.sqlite3")
            with patch.object(api, "_STATE", state), patch.object(
                api,
                "_provider_for",
                side_effect=lambda name: providers[name],
            ):
                return query(intent, **params)

    def test_realtime_poll_profile_prefers_pytdx_without_exposing_provider_source(self):
        self.assertEqual(
            ("stocktoday", "tencent", "pytdx", "eastmoney"),
            route_for("realtime_market", {}).providers,
        )
        self.assertEqual(
            ("pytdx", "tencent", "eastmoney"),
            route_for("realtime_market", {"use_case": "realtime_poll"}).providers,
        )
        self.assertEqual(
            ("pytdx", "tencent", "tdx_quotes"),
            route_for(
                "stock_snapshot",
                {"codes": ["600519"], "use_case": "realtime_poll"},
            ).providers,
        )

        with self.assertRaises(ValueError):
            query("stock_snapshot", codes=["600519"], source="pytdx")

    def test_long_tail_capabilities_have_stocktoday_first_canonical_routes(self):
        self.assertEqual(
            ("stocktoday", "ths_industry"),
            route_for("industry_flow", {"trade_date": "20260922"}).providers,
        )
        self.assertEqual(
            ("ths_industry", "stocktoday"),
            route_for(
                "industry_flow",
                {"use_case": "realtime_poll"},
            ).providers,
        )
        self.assertEqual(
            ("stocktoday",),
            route_for("fund_flow", {"trade_date": "20260922"}).providers,
        )
        self.assertEqual(
            ("stocktoday", "northbound"),
            route_for("northbound_flow", {"trade_date": "20260922"}).providers,
        )
        self.assertEqual(
            ("stocktoday", "ths_hot"),
            route_for("legacy_hot_rank", {"trade_date": "20260922"}).providers,
        )
        self.assertEqual(
            ("northbound",),
            route_for(
                "northbound_flow",
                {"trade_date": "20260922", "use_case": "realtime_poll"},
            ).providers,
        )
        self.assertEqual(
            ("ths_hot", "stocktoday"),
            route_for(
                "legacy_hot_rank",
                {"trade_date": "20260922", "use_case": "realtime_poll"},
            ).providers,
        )
        self.assertEqual(
            ("eastmoney_index", "sina_index", "stocktoday"),
            route_for(
                "index_intraday_compare",
                {"period": "15m", "use_case": "realtime_poll"},
            ).providers,
        )
        self.assertEqual(
            ("stocktoday", "eastmoney_index", "sina_index", "pytdx_index"),
            route_for(
                "index_kline",
                {"index_code": "000001.SH", "period": "daily"},
            ).providers,
        )

    def test_stock_kline_accepts_minute_range_and_qfq_is_long_period_only(self):
        self.assertEqual(
            ("stocktoday", "pytdx", "sina", "tdx_kline"),
            route_for(
                "stock_kline",
                {"period": "1m", "start_date": "20260922", "end_date": "20260922"},
            ).providers,
        )

    def test_index_kline_accepts_backfill_codes_and_dashed_dates(self):
        provider = _FakeProvider(
            "stocktoday",
            [
                _outcome(
                    "stocktoday",
                    "success",
                    {
                        "items": [
                            {
                                "index_code": "000001.SH",
                                **_full_kline("none"),
                            },
                            {
                                "index_code": "399001.SZ",
                                **_full_kline("none"),
                            },
                        ]
                    },
                )
            ],
        )
        result = self._run_with_fakes(
            "index_kline",
            {"stocktoday": provider},
            codes=["000001", "399001"],
            start_date="2026-09-22",
            end_date="2026-09-22",
        )
        self.assertEqual("stocktoday", result["_meta"]["provider_used"])
        self.assertEqual("000001.SH", provider.calls[0][1]["codes"][0])
        self.assertEqual("20260922", provider.calls[0][1]["start_date"])

    def test_index_minute_stocktoday_failure_reaches_eastmoney_before_pytdx(self):
        stocktoday = _FakeProvider(
            "stocktoday",
            [_outcome("stocktoday", "provider_error", error_code="UPSTREAM")],
        )
        eastmoney = _FakeProvider(
            "eastmoney_index",
            [_outcome("eastmoney_index", "success", {"index_code": "000001.SH", **_full_kline()})],
        )
        pytdx_provider = _FakeProvider(
            "pytdx_index",
            [_outcome("pytdx_index", "provider_error", error_code="SHOULD_NOT_RUN")],
        )

        result = self._run_with_fakes(
            "index_kline",
            {
                "stocktoday": stocktoday,
                "eastmoney_index": eastmoney,
                "pytdx_index": pytdx_provider,
            },
            index_code="000001.SH",
            period="5m",
            start_date="20260922",
            end_date="20260922",
        )

        self.assertEqual("eastmoney_index", result["_meta"]["provider_used"])
        self.assertEqual(
            ["provider_error", "success"],
            [attempt["status"] for attempt in result["_meta"]["attempts"]],
        )
        self.assertFalse(pytdx_provider.calls)

    def test_index_minute_eastmoney_failure_reaches_sina_before_pytdx(self):
        eastmoney = _FakeProvider(
            "eastmoney_index",
            [_outcome("eastmoney_index", "provider_error", error_code="RATE_LIMITED")],
        )
        sina = _FakeProvider(
            "sina_index",
            [_outcome("sina_index", "success", {"index_code": "000001.SH", **_full_kline()})],
        )
        stocktoday = _FakeProvider(
            "stocktoday",
            [_outcome("stocktoday", "provider_error", error_code="SHOULD_NOT_RUN")],
        )

        result = self._run_with_fakes(
            "index_kline",
            {"eastmoney_index": eastmoney, "sina_index": sina, "stocktoday": stocktoday},
            index_code="000001.SH",
            period="5m",
            start_date="20260922",
            end_date="20260922",
        )

        self.assertEqual("sina_index", result["_meta"]["provider_used"])
        self.assertEqual(
            ["provider_error", "provider_error", "success"],
            [attempt["status"] for attempt in result["_meta"]["attempts"]],
        )
        self.assertEqual(1, len(stocktoday.calls))

    def test_eastmoney_index_minute_adapter_converts_hands_to_shares(self):
        response = Mock()
        response.json.return_value = {
            "data": {
                "klines": [
                    "2026-09-22 09:30,3000,3001,3002,2999,12810144,128101441536,0,0,1,0",
                    "2026-09-22 09:35,3001,3002,3003,3000,12810145,128101451536,0,0,1,0",
                ]
            }
        }
        with patch.object(eastmoney_index.CLIENT, "get", return_value=response):
            result = eastmoney_index.fetch_index_kline(
                "000001.SH",
                period="5m",
                start_date="20260922",
                end_date="20260922",
            )

        self.assertEqual(2, len(result["bars"]))
        self.assertEqual(1281014400, result["bars"][0]["volume"])
        self.assertEqual(128101441536, result["bars"][0]["amount"])
        self.assertEqual("share", result["volume_unit"])
        self.assertEqual("CNY", result["amount_unit"])

    def test_eastmoney_index_adapter_retries_transient_disconnect(self):
        response = Mock()
        response.json.return_value = {
            "data": {
                "klines": [
                    "2026-09-22 09:30,3000,3001,3002,2999,100,1000,0,0,1,0",
                ]
            }
        }
        with (
            patch.object(
                eastmoney_index.CLIENT,
                "get",
                side_effect=[ConnectionError("remote disconnected"), response],
            ) as get,
            patch.object(eastmoney_index.time, "sleep") as sleep,
        ):
            result = eastmoney_index.fetch_index_kline(
                "000001.SH",
                period="5m",
                start_date="20260922",
                end_date="20260922",
            )

        self.assertEqual(1, len(result["bars"]))
        self.assertEqual(2, get.call_count)
        self.assertEqual(1, sleep.call_count)
        self.assertEqual(
            "close", get.call_args_list[0].kwargs["headers"]["Connection"]
        )

    def test_sina_index_minute_adapter_preserves_share_and_cny_units(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = (
            "var _k=([{\"day\":\"2026-09-21 09:30:00\",\"open\":\"2990\","
            "\"high\":\"2991\",\"low\":\"2989\",\"close\":\"2990.5\","
            "\"volume\":\"120\",\"amount\":\"120000\"},{\"day\":"
            "\"2026-09-22 09:30:00\",\"open\":\"3000\",\"high\":\"3001\","
            "\"low\":\"2999\",\"close\":\"3000.5\",\"volume\":\"1730747300\","
            "\"amount\":\"33917796352\"}]);"
        ).encode()

        with patch.object(sina_index.urllib.request, "urlopen", return_value=response) as urlopen:
            result = sina_index.fetch_index_kline(
                "000001.SH",
                period="15m",
                count=10,
                start_date="20260922",
                end_date="20260922",
            )

        self.assertEqual(1, len(result["bars"]))
        self.assertEqual(1730747300, result["bars"][0]["volume"])
        self.assertEqual(33917796352, result["bars"][0]["amount"])
        self.assertEqual("share", result["volume_unit"])
        self.assertEqual("CNY", result["amount_unit"])
        self.assertIn("symbol=sh000001", urlopen.call_args.args[0].full_url)

    def test_eastmoney_stock_adapter_keeps_none_and_qfq_distinct(self):
        payload = {
            "data": {
                "klines": [
                    "2026-09-22,1500,1501,1502,1499,100,1000000,0,0,1,0",
                ]
            }
        }
        calls = []

        def fake_get(endpoint, **kwargs):
            calls.append((endpoint, kwargs))
            return payload, None

        with patch.object(eastmoney_stock, "get_json_payload", side_effect=fake_get):
            none_result = eastmoney_stock.fetch_kline(
                "600519", period="daily", count=1, adjustment="none"
            )
            qfq_result = eastmoney_stock.fetch_kline(
                "600519", period="daily", count=1, adjustment="qfq"
            )

        self.assertEqual("none", none_result["adjustment"])
        self.assertEqual("qfq", qfq_result["adjustment"])
        self.assertEqual(0, calls[0][1]["params"]["fqt"])
        self.assertEqual(1, calls[1][1]["params"]["fqt"])
        self.assertEqual(10000, qfq_result["bars"][0]["volume"])
        self.assertEqual(1000000, qfq_result["bars"][0]["amount"])

    def test_qfq_fallback_uses_eastmoney_stock_before_tencent(self):
        stocktoday = _FakeProvider(
            "stocktoday",
            [_outcome("stocktoday", "provider_error", error_code="UPSTREAM")],
        )
        eastmoney = _FakeProvider(
            "eastmoney_stock",
            [_outcome("eastmoney_stock", "success", _full_kline("qfq"))],
        )
        tencent_provider = _FakeProvider(
            "tencent",
            [_outcome("tencent", "provider_error", error_code="SHOULD_NOT_RUN")],
        )

        result = self._run_with_fakes(
            "stock_kline",
            {
                "stocktoday": stocktoday,
                "eastmoney_stock": eastmoney,
                "tencent": tencent_provider,
            },
            code="600519",
            period="daily",
            adjustment="qfq",
        )

        self.assertEqual("eastmoney_stock", result["_meta"]["provider_used"])
        self.assertEqual("qfq", result["data"]["adjustment"])
        self.assertFalse(tencent_provider.calls)

    def test_qfq_and_none_results_cannot_cross_quality_gate(self):
        stocktoday = _FakeProvider(
            "stocktoday",
            [_outcome("stocktoday", "provider_error", error_code="UPSTREAM")],
        )
        eastmoney = _FakeProvider(
            "eastmoney_stock",
            [_outcome("eastmoney_stock", "success", _full_kline("qfq"))],
        )
        tencent_provider = _FakeProvider(
            "tencent",
            [_outcome("tencent", "success", _full_kline("none"))],
        )

        result = self._run_with_fakes(
            "stock_kline",
            {
                "stocktoday": stocktoday,
                "eastmoney_stock": eastmoney,
                "tencent": tencent_provider,
            },
            code="600519",
            period="daily",
            adjustment="none",
        )

        self.assertEqual("tencent", result["_meta"]["provider_used"])
        self.assertEqual("QUALITY_ADJUSTMENT_MISMATCH", result["_meta"]["attempts"][1]["error_code"])

    def test_production_compat_fetches_project_through_canonical_intents(self):
        calls = []

        def canonical(intent, **params):
            calls.append((intent, params))
            return {
                "data": {"items": []},
                "_meta": {"status": "empty", "intent": intent},
            }

        with patch("ym_stock_data.fetch.canonical_query", side_effect=canonical):
            for data_type, kwargs in (
                ("sector_inflow", {"top_n": 2}),
                ("northbound", {}),
                ("ths_hot", {"date_str": "2026-09-22"}),
                ("kline_15m", {}),
            ):
                result = fetch(data_type, **kwargs)
                self.assertEqual("canonical", result["_meta"]["compatibility_route"])

        self.assertEqual(
            ["industry_flow", "northbound_flow", "legacy_hot_rank", "index_intraday_compare"],
            [intent for intent, _ in calls],
        )
        self.assertEqual({"limit": 2, "use_case": "realtime_poll"}, calls[0][1])
        self.assertEqual({"use_case": "realtime_poll"}, calls[1][1])
        self.assertEqual(
            {"trade_date": "20260922", "use_case": "realtime_poll"},
            calls[2][1],
        )
        self.assertEqual({"use_case": "realtime_poll"}, calls[3][1])

    def test_canonical_industry_realtime_profile_reaches_fallback_as_current_session(self):
        primary = _FakeProvider(
            "stocktoday",
            [_outcome("stocktoday", "provider_error", error_code="UPSTREAM_ERROR")],
        )
        fallback = _FakeProvider(
            "ths_industry",
            [_outcome("ths_industry", "success", {"items": [{"code": "881001"}]})],
        )
        result = self._run_with_fakes(
            "industry_flow",
            {"stocktoday": primary, "ths_industry": fallback},
            use_case="realtime_poll",
        )

        self.assertEqual("ths_industry", result["_meta"]["provider_used"])
        self.assertTrue(fallback.calls[0][1]["current_session"])
        self.assertNotIn("use_case", fallback.calls[0][1])

    def test_canonical_industry_realtime_profile_starts_with_current_source(self):
        primary = _FakeProvider(
            "ths_industry",
            [_outcome("ths_industry", "success", {"items": [{"code": "881001"}]})],
        )
        result = self._public_query_with_providers(
            "industry_flow",
            {"ths_industry": primary, "stocktoday": _FakeProvider("stocktoday", [])},
            use_case="realtime_poll",
        )

        self.assertEqual("ths_industry", result["_meta"]["provider_used"])
        self.assertEqual("success", result["_meta"]["status"])

    def test_query_realtime_compare_rejects_stocktoday_shape_without_pytdx(self):
        compare_row = {
            "t": "09:35",
            "chg": 1.0,
            "vol": 100,
            "volRatio": 1.2,
            "amount": 1000,
            "yesterdayAmt": 900,
        }
        primary = _FakeProvider(
            "eastmoney_index",
            [_outcome("eastmoney_index", "provider_error", error_code="UPSTREAM")],
        )
        fallback = _FakeProvider(
            "stocktoday",
            [_outcome("stocktoday", "success", {"items": [compare_row]})],
        )
        sina = _FakeProvider(
            "sina_index",
            [_outcome("sina_index", "provider_error", error_code="SHOULD_NOT_RUN")],
        )
        result = self._run_with_fakes(
            "index_intraday_compare",
            {"eastmoney_index": primary, "sina_index": sina, "stocktoday": fallback},
            period="15m",
        )

        self.assertIsNone(result["_meta"]["provider_used"])
        self.assertEqual(
            ["provider_error", "provider_error", "quality_failure"],
            [attempt["status"] for attempt in result["_meta"]["attempts"]],
        )
        self.assertEqual("QUALITY_COMPARE_INCOMPLETE", result["_meta"]["attempts"][2]["error_code"])

    def test_eastmoney_compare_builds_three_index_legacy_shape(self):
        def fake_kline(index_code, **_params):
            return {
                "index_code": index_code,
                "bars": [
                    {
                        "datetime": f"{date} {slot}",
                        "open": 3000.0,
                        "high": 3001.0,
                        "low": 2999.0,
                        "close": 3000.5,
                        "volume": volume,
                        "amount": amount,
                    }
                    for date, volume, amount in (
                        ("2026-09-21", 90, 900),
                        ("2026-09-22", 100, 1000),
                    )
                    for slot in ("09:30", "09:35")
                ],
            }

        with patch.object(eastmoney_index, "fetch_index_kline", side_effect=fake_kline):
            result = eastmoney_index.fetch_index_intraday_compare(
                period="15m", trade_date="20260922"
            )

        self.assertEqual(
            ["上证15min", "深证15min", "创业15min"],
            [key for key in result if key.endswith("15min")],
        )
        row = result["上证15min"][0]
        self.assertEqual(
            {"t", "chg", "vol", "volRatio", "amount", "yesterdayAmt"},
            set(row),
        )
        self.assertEqual(100, row["vol"])
        self.assertEqual(1000, row["amount"])
        self.assertEqual(900, row["yesterdayAmt"])
        self.assertTrue(result["上证15min"][-1]["_cum"])

    def test_query_northbound_realtime_profile_preserves_current_minute_shape(self):
        raw = {
            "date": "2026-09-23",
            "minutes": [{"time": "09:30", "hgt_yi": 1.0, "sgt_yi": 2.0}],
        }
        with patch(
            "ym_stock_data.providers.local.northbound.fetch_realtime",
            return_value=raw,
        ):
            result = self._public_query_with_providers(
                "northbound_flow",
                {"northbound": LocalProvider("northbound")},
                use_case="realtime_poll",
            )

        self.assertEqual("northbound", result["_meta"]["provider_used"])
        self.assertEqual("2026-09-23", result["data"]["trade_date"])
        self.assertEqual(raw["minutes"], result["data"]["items"])

    def test_query_legacy_hot_realtime_profile_requires_old_hot_list_shape(self):
        raw = {
            "date": "2026-09-23",
            "stocks": [{"code": "600519"}],
            "reason_stats": {"涨停": 1},
            "zt_count": 3,
        }
        with patch(
            "ym_stock_data.providers.local.ths_hot.fetch_hot_with_zt_count",
            return_value=raw,
        ):
            result = self._public_query_with_providers(
                "legacy_hot_rank",
                {"ths_hot": LocalProvider("ths_hot")},
                use_case="realtime_poll",
            )

        self.assertEqual("ths_hot", result["_meta"]["provider_used"])
        self.assertEqual([{"code": "600519"}], result["data"]["items"])
        self.assertEqual({"涨停": 1}, result["data"]["reason_stats"])
        self.assertEqual(3, result["data"]["zt_count"])

    def test_pytdx_quote_index_and_kline_paths_are_direct_only(self):
        fallback_quotes = Mock(return_value={"600519": {"price": 1}})
        fallback_index = Mock(return_value=_full_index())
        fallback_kline = Mock(return_value=_full_kline())
        with patch.object(pytdx, "_get_api", return_value=None), patch.object(
            pytdx, "_fallback_quotes", fallback_quotes
        ), patch.object(pytdx, "_fallback_index", fallback_index), patch.object(
            pytdx, "_fallback_kline", fallback_kline
        ):
            quotes = pytdx.fetch_quotes(["600519"])
            index = pytdx.fetch_index()
            bars = pytdx.fetch_kline("600519", period="daily")

        self.assertFalse(fallback_quotes.called)
        self.assertFalse(fallback_index.called)
        self.assertFalse(fallback_kline.called)
        self.assertTrue(quotes.get("error") or not quotes)
        self.assertTrue(index.get("error") or not index)
        self.assertTrue(bars.get("error") or not bars)

    def test_pytdx_stock_minute_date_range_pages_beyond_recent_48_rows(self):
        calls = []

        def bar(stamp):
            return {
                "datetime": stamp,
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.1,
                "vol": 100,
                "amount": 10000,
            }

        class PagedApi:
            def get_security_bars(self, bar_type, market, code, start, count):
                calls.append((bar_type, market, code, start, count))
                if start == 0:
                    return [bar("2026-09-23 09:30:00")]
                if start == 800:
                    return [bar("2026-09-22 09:30:00"), bar("2026-09-22 09:35:00")]
                return []

        with patch.object(pytdx, "_get_api", return_value=PagedApi()):
            result = pytdx.fetch_kline(
                "600519",
                period="5m",
                start_date="20260922",
                end_date="20260922",
            )

        self.assertEqual([0, 800], [call[3] for call in calls])
        self.assertTrue(all(call[4] == 800 for call in calls))
        self.assertEqual(
            ["2026-09-22 09:30:00", "2026-09-22 09:35:00"],
            [bar["time"] for bar in result["bars"]],
        )

    def test_pytdx_stock_kline_volume_multiplier_follows_period_units(self):
        raw = {
            "code": "600519",
            "bars": [
                {
                    "time": "2026-09-22 09:35:00",
                    "open": 1400.0,
                    "high": 1401.0,
                    "low": 1399.0,
                    "close": 1400.5,
                    "vol": 2868,
                    "amount": 4012345,
                }
            ],
        }
        with patch.object(pytdx, "fetch_kline", return_value=raw):
            minute = LocalProvider._pytdx_kline(
                {"code": "600519", "period": "5m", "adjustment": "none"}
            )
            daily = LocalProvider._pytdx_kline(
                {"code": "600519", "period": "daily", "adjustment": "none"}
            )

        self.assertEqual(2868, minute["bars"][0]["volume"])
        self.assertEqual(286800, daily["bars"][0]["volume"])

    def test_pytdx_index_minute_is_incompatible_without_canonical_volume(self):
        with patch.object(pytdx, "_get_api") as get_api:
            result = pytdx.fetch_index_kline(
                "000001.SH",
                period="5m",
                start_date="20260921",
                end_date="20260922",
            )

        self.assertEqual("INCOMPATIBLE_PERIOD", result["error_type"])
        get_api.assert_not_called()

    def test_pytdx_index_daily_volume_uses_ten_thousand_multiplier(self):
        class DailyApi:
            def get_index_bars(self, bar_type, market, code, start, count):
                return [
                    {
                        "datetime": "2026-09-22 15:00:00",
                        "open": 3000.0,
                        "high": 3001.0,
                        "low": 2999.0,
                        "close": 3000.5,
                        "vol": 5061480,
                        "amount": 50614800000,
                    }
                ]

        with patch.object(pytdx, "_get_api", return_value=DailyApi()):
            result = pytdx.fetch_index_kline(
                "000001.SH",
                period="daily",
                start_date="20260922",
                end_date="20260922",
            )

        self.assertEqual(50614800000, result["bars"][0]["volume"])

    def test_pytdx_compat_bootstrap_packets_use_current_public_protocol(self):
        packets = pytdx._compat_setup_packets()
        self.assertEqual(
            [
                bytes.fromhex("0c0000000000020002001500"),
                bytes.fromhex("0c0218940100030003000d0001"),
                bytes.fromhex(
                    "0c031899020020002000db0f"
                    "7464786c6576656c000000e17af4404c0000000000000000000000000005"
                ),
            ],
            packets,
        )

    def test_tencent_snapshot_maps_verified_raw_quote_fields_to_canonical_units(self):
        values = [""] * 53
        values[1] = "贵州茅台"
        values[3] = "1500.0"
        values[4] = "1490.0"
        values[5] = "1495.0"
        values[6] = "24572.94"
        values[30] = "20260923092627"
        values[32] = "0.67"
        values[33] = "1510.0"
        values[34] = "1480.0"
        values[37] = "3088526.148"
        raw = 'v_sh600519="' + "~".join(values) + '";'
        response = MagicMock()
        response.read.return_value = raw.encode("gbk")

        with patch.object(tencent.urllib.request, "urlopen", return_value=response):
            result = tencent.fetch_quotes(["600519"])

        row = result["600519"]
        self.assertEqual(2457294, row["volume"])
        self.assertAlmostEqual(30885261480.0, row["amount"])
        self.assertEqual(3088526.148, row["amount_wan"])
        self.assertEqual("2026-09-23T09:26:27+08:00", row["quote_time"])

    def test_pytdx_short_servertime_is_emitted_as_shanghai_iso_datetime(self):
        quote_time = pytdx._quote_time("9:14:27.588")
        self.assertRegex(
            quote_time or "",
            r"^20\d{2}-\d{2}-\d{2}T09:14:27\.588\+08:00$",
        )

    def test_stale_pytdx_snapshot_falls_through_to_tencent_canonical_snapshot(self):
        now = datetime(2026, 9, 23, 10, 0, tzinfo=api.TZ_SHANGHAI)
        stale = _full_snapshot("600519", "000001")
        stale_time = (now - timedelta(minutes=13)).isoformat(timespec="seconds")
        for row in stale.values():
            row["quote_time"] = stale_time
        fresh = _full_snapshot("600519", "000001")
        for row in fresh.values():
            row["quote_time"] = now.isoformat(timespec="seconds")

        with patch.object(pytdx, "fetch_quotes", return_value=stale), patch.object(
            tencent, "fetch_quotes", return_value=fresh
        ), patch.object(
            api, "_now_shanghai", return_value=now
        ), patch(
            "ym_stock_data.providers.local._now_iso", return_value=now.isoformat(timespec="seconds")
        ):
            result = self._run_with_fakes(
                "stock_snapshot",
                {
                    "pytdx": LocalProvider("pytdx"),
                    "tencent": LocalProvider("tencent"),
                },
                codes=["600519", "000001"],
                use_case="realtime_poll",
            )

        self.assertEqual("tencent", result["_meta"]["provider_used"])
        self.assertEqual("quality_failure", result["_meta"]["attempts"][0]["status"])
        self.assertEqual("QUALITY_SNAPSHOT_STALE", result["_meta"]["attempts"][0]["error_code"])
        self.assertEqual("success", result["_meta"]["attempts"][1]["status"])

    def test_stocktoday_daily_budget_is_environment_configurable_without_default_5000_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget_path = Path(tmp) / "budget.json"
            with patch.dict(os.environ, {}, clear=True):
                default_budget = RequestBudget(budget_path)
            self.assertEqual(0, default_budget.per_day)
            self.assertEqual(0, default_budget.per_minute)

            with patch.dict(
                os.environ,
                {
                    "YM_STOCKTODAY_PER_DAY": "7",
                    "YM_STOCKTODAY_PER_MINUTE": "9",
                },
                clear=True,
            ):
                configured_budget = RequestBudget(budget_path)
            self.assertEqual(7, configured_budget.per_day)
            self.assertEqual(9, configured_budget.per_minute)

    def test_quality_gate_rejects_incomplete_three_index_result_and_falls_back(self):
        primary = _FakeProvider(
            "stocktoday",
            [_outcome("stocktoday", "success", {"上证指数": 3200.0})],
        )
        fallback = _FakeProvider(
            "tencent",
            [_outcome("tencent", "success", _full_index())],
        )
        result = self._run_with_fakes(
            "realtime_market",
            {"stocktoday": primary, "tencent": fallback},
        )

        self.assertEqual("tencent", result["_meta"]["source"])
        self.assertEqual("quality_failure", result["_meta"]["attempts"][0]["status"])
        self.assertEqual("success", result["_meta"]["attempts"][1]["status"])

    def test_quality_gate_rejects_partial_snapshot_and_falls_back(self):
        primary = _FakeProvider(
            "stocktoday",
            [_outcome("stocktoday", "success", _full_snapshot("600519"))],
        )
        fallback = _FakeProvider(
            "tencent",
            [_outcome("tencent", "success", _full_snapshot("600519", "000001"))],
        )
        result = self._run_with_fakes(
            "stock_snapshot",
            {"stocktoday": primary, "tencent": fallback},
            codes=["600519", "000001"],
        )

        self.assertEqual("tencent", result["_meta"]["source"])
        self.assertEqual("quality_failure", result["_meta"]["attempts"][0]["status"])

    def test_kline_qfq_does_not_accept_none_result(self):
        primary = _FakeProvider(
            "stocktoday",
            [_outcome("stocktoday", "success", _full_kline("none"))],
        )
        fallback = _FakeProvider(
            "tencent",
            [_outcome("tencent", "success", _full_kline("qfq"))],
        )
        result = self._run_with_fakes(
            "stock_kline",
            {"stocktoday": primary, "tencent": fallback},
            code="600519",
            period="daily",
            adjustment="qfq",
        )

        self.assertEqual("tencent", result["_meta"]["source"])
        self.assertEqual("qfq", result["data"]["adjustment"])
        self.assertEqual("quality_failure", result["_meta"]["attempts"][0]["status"])

    def test_tencent_kline_receives_requested_adjustment_and_returns_contract_units(self):
        provider = LocalProvider("tencent")

        with patch.object(
            pytdx,
            "_fetch_tencent_kline",
            return_value=[
                {
                    "time": "2026-09-22 15:00:00",
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                    "vol": 1000,
                    "amount": 10500.0,
                }
            ],
        ) as fetch:
            result = provider._http_kline(
                provider="tencent",
                params={
                    "code": "600519",
                    "period": "daily",
                    "count": 1,
                    "adjustment": "qfq",
                },
            )

        fetch.assert_called_once_with(
            "600519", period="daily", count=1, adjustment="qfq"
        )
        self.assertEqual("qfq", result["adjustment"])
        self.assertEqual("share", result["volume_unit"])
        self.assertEqual("CNY", result["amount_unit"])
        self.assertIn("datetime", result["bars"][0])
        self.assertIn("volume", result["bars"][0])

    def test_unverified_stocktoday_minute_bar_is_quality_failure_and_reaches_pytdx(self):
        primary_data = _full_kline()
        primary_data["_stocktoday"] = {
            "status": "unverified_bar_time",
            "bar_time_convention": "unverified",
        }
        primary = _FakeProvider(
            "stocktoday",
            [_outcome("stocktoday", "success", primary_data)],
        )
        fallback = _FakeProvider(
            "pytdx",
            [_outcome("pytdx", "success", _full_kline())],
        )

        result = self._run_with_fakes(
            "stock_kline",
            {"stocktoday": primary, "pytdx": fallback},
            code="600519",
            period="5m",
            start_date="20260922",
            end_date="20260922",
        )

        self.assertEqual("pytdx", result["_meta"]["provider_used"])
        self.assertEqual("quality_failure", result["_meta"]["attempts"][0]["status"])
        self.assertEqual("QUALITY_KLINE_BAR_TIME", result["_meta"]["attempts"][0]["error_code"])
        self.assertEqual("success", result["_meta"]["attempts"][1]["status"])

    def test_tencent_kline_scales_lots_but_missing_amount_stays_missing_and_fails_quality(self):
        with patch.object(
            pytdx,
            "_fetch_tencent_kline",
            return_value=[
                {
                    "time": "2026-09-22",
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                    "vol": 1000,
                    "amount": None,
                }
            ],
        ):
            tencent_data = LocalProvider("tencent")._http_kline(
                provider="tencent",
                params={"code": "600519", "period": "daily", "count": 1, "adjustment": "none"},
            )

        self.assertEqual(100000, tencent_data["bars"][0]["volume"])
        self.assertIsNone(tencent_data["bars"][0]["amount"])

        result = self._run_with_fakes(
            "stock_kline",
            {
                "stocktoday": _FakeProvider(
                    "stocktoday",
                    [_outcome("stocktoday", "provider_error", error_code="UPSTREAM")],
                ),
                "eastmoney_stock": _FakeProvider(
                    "eastmoney_stock",
                    [_outcome("eastmoney_stock", "provider_error", error_code="UPSTREAM")],
                ),
                "tencent": _FakeProvider(
                    "tencent", [_outcome("tencent", "success", tencent_data)]
                ),
                "pytdx": _FakeProvider(
                    "pytdx", [_outcome("pytdx", "success", _full_kline())]
                ),
            },
            code="600519",
            period="daily",
        )

        self.assertEqual("pytdx", result["_meta"]["provider_used"])
        self.assertEqual("quality_failure", result["_meta"]["attempts"][2]["status"])
        self.assertEqual("QUALITY_KLINE_FIELDS", result["_meta"]["attempts"][2]["error_code"])

    def test_market_hot_rank_without_date_passes_deterministic_trade_date(self):
        provider = _FakeProvider(
            "stocktoday",
            [_outcome("stocktoday", "success", {"items": [{"code": "x"}]})],
        )
        self._run_with_fakes(
            "market_hot_rank",
            {"stocktoday": provider},
            source="ths",
        )

        self.assertRegex(provider.calls[0][1]["trade_date"], r"^20\d{6}$")

    def test_legacy_industry_flow_does_not_relabel_undated_realtime_rows(self):
        raw = {
            "top": [{"code": "881001", "name": "行业", "change_pct": 1.0}],
            "bottom": [],
        }
        with patch.object(
            __import__("ym_stock_data.providers.local", fromlist=["ths_industry"]).ths_industry,
            "fetch_industry_summary",
            return_value=raw,
        ):
            result = LocalProvider("ths_industry").call(
                "industry_flow", {"trade_date": "20260922"}
            )

        self.assertEqual("incompatible", result.status)
        self.assertEqual("DATE_UNVERIFIED", result.error_code)

    def test_legacy_industry_flow_can_remain_current_session_without_fake_date(self):
        raw = {
            "top": [{"code": "881001", "name": "行业", "change_pct": 1.0}],
            "bottom": [],
        }
        with patch.object(
            __import__("ym_stock_data.providers.local", fromlist=["ths_industry"]).ths_industry,
            "fetch_industry_summary",
            return_value=raw,
        ):
            result = LocalProvider("ths_industry").call(
                "industry_flow", {"current_session": True}
            )

        self.assertEqual("success", result.status)
        self.assertIsNone(result.data["trade_date"])

    def test_legacy_northbound_flow_uses_raw_date_not_requested_date(self):
        raw = {
            "date": "2026-09-23",
            "minutes": [{"time": "09:30", "hgt_yi": 1.0, "sgt_yi": 2.0}],
        }
        with patch.object(
            __import__("ym_stock_data.providers.local", fromlist=["northbound"]).northbound,
            "fetch_realtime",
            return_value=raw,
        ):
            result = LocalProvider("northbound").call(
                "northbound_flow", {"current_session": True}
            )

        self.assertEqual("success", result.status)
        self.assertEqual("2026-09-23", result.data["trade_date"])

    def test_legacy_northbound_history_does_not_claim_a_mismatched_raw_date(self):
        raw = {
            "date": "2026-09-23",
            "minutes": [{"time": "09:30", "hgt_yi": 1.0, "sgt_yi": 2.0}],
        }
        with patch.object(
            __import__("ym_stock_data.providers.local", fromlist=["northbound"]).northbound,
            "fetch_realtime",
            return_value=raw,
        ):
            result = LocalProvider("northbound").call(
                "northbound_flow", {"trade_date": "20260922"}
            )

        self.assertEqual("incompatible", result.status)
        self.assertEqual("DATE_MISMATCH", result.error_code)

    def test_legacy_northbound_flow_rejects_missing_raw_date(self):
        with patch.object(
            __import__("ym_stock_data.providers.local", fromlist=["northbound"]).northbound,
            "fetch_realtime",
            return_value={"minutes": [{"time": "09:30"}]},
        ):
            result = LocalProvider("northbound").call(
                "northbound_flow", {"trade_date": "20260922"}
            )

        self.assertEqual("incompatible", result.status)
        self.assertEqual("DATE_UNVERIFIED", result.error_code)


if __name__ == "__main__":
    unittest.main()
