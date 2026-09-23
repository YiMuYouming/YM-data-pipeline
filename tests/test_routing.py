import unittest
from unittest.mock import patch

import ym_stock_data.api as api
from ym_stock_data.providers.local import LocalProvider
from ym_stock_data.providers.tdx_mcp import TDX_PROVIDER_SPECS
from ym_stock_data.routing import route_for


class RoutingTests(unittest.TestCase):
    def test_arbitrary_explicit_screen_uses_four_compatible_providers(self):
        spec = route_for("review_sentiment", {"query": "今日涨停 非ST"})
        self.assertEqual(
            ("iwencai_openapi", "pywencai", "tdx_screener", "wind_screener"),
            spec.providers,
        )
        self.assertEqual("continue_until_exhausted", spec.empty_policy)

    def test_structured_screen_keeps_pytdx_experimental_and_uses_four_sources(self):
        spec = route_for(
            "review_sentiment",
            {"query": "沪深A股 非ST 非停牌 最新价>=10"},
        )
        self.assertEqual(
            (
                "iwencai_openapi",
                "pywencai",
                "tdx_screener",
                "wind_screener",
            ),
            spec.providers,
        )

    def test_default_sentiment_never_calls_natural_language_sources(self):
        spec = route_for("review_sentiment", {})
        self.assertEqual(
            ("pytdx_breadth", "eastmoney_breadth", "eastmoney_limit_pool"),
            spec.providers,
        )
        self.assertEqual("stop", spec.empty_policy)

    def test_sector_index_keeps_its_existing_price_semantics(self):
        spec = route_for("sector_index", {"names": ["不存在板块"]})
        self.assertEqual(("ths_industry",), spec.providers)
        self.assertEqual("stop", spec.empty_policy)

    def test_stocktoday_is_first_only_for_new_explicit_limit_board(self):
        limit = route_for("market_limit_board", {"kind": "up"})
        self.assertEqual(("stocktoday", "eastmoney_limit_pool"), limit.providers)
        self.assertEqual("continue_until_exhausted", limit.empty_policy)
        hot = route_for("market_hot_rank", {"source": "ths"})
        self.assertEqual(("stocktoday",), hot.providers)
        self.assertEqual("stop", hot.empty_policy)
        sector = route_for("sector_index", {"names": ["半导体"]})
        self.assertEqual(("ths_industry",), sector.providers)
        self.assertEqual("stop", sector.empty_policy)

    def test_wind_is_not_a_realtime_market_fallback(self):
        self.assertNotIn("wind_mcp", route_for("realtime_market", {}).providers)

    def test_stocktoday_is_first_for_realtime_snapshot_and_daily_kline_routes(self):
        self.assertEqual(
            ("stocktoday", "tencent", "pytdx", "eastmoney"),
            route_for("realtime_market", {}).providers,
        )
        self.assertEqual(
            ("stocktoday", "tencent", "pytdx", "tdx_quotes"),
            route_for("stock_snapshot", {}).providers,
        )
        for period in ("daily", "weekly", "monthly"):
            with self.subTest(period=period):
                spec = route_for("stock_kline", {"period": period})
                self.assertEqual(
                    ("stocktoday", "eastmoney_stock", "tencent", "pytdx", "tdx_kline"),
                    spec.providers,
                )
                self.assertEqual("continue_until_exhausted", spec.empty_policy)
        minute = route_for("stock_kline", {"period": "60m"})
        self.assertEqual(
            ("stocktoday", "pytdx", "sina", "tdx_kline"),
            minute.providers,
        )
        self.assertEqual("continue_until_exhausted", minute.empty_policy)

    def test_stock_snapshot_route_matches_real_adapter_intents_without_network(self):
        params = {"codes": ["600519"]}
        with patch(
            "ym_stock_data.providers.local.pytdx.fetch_quotes",
            return_value={"600519": {"price": 1}},
        ), patch(
            "ym_stock_data.providers.local.tencent.fetch_quotes",
            return_value={"600519": {"price": 1}},
        ):
            local_outcomes = {
                provider: LocalProvider(provider).call("stock_snapshot", params)
                for provider in ("pytdx", "tencent", "sina")
            }

        adapter_contracts = {
            "stocktoday": "stocktoday" in api.PROVIDER_REGISTRY,
            "pytdx": local_outcomes["pytdx"].error_code != "PROVIDER_ADAPTER_MISSING",
            "tencent": local_outcomes["tencent"].error_code != "PROVIDER_ADAPTER_MISSING",
            "sina": local_outcomes["sina"].error_code != "PROVIDER_ADAPTER_MISSING",
            "tdx_quotes": TDX_PROVIDER_SPECS.get("tdx_quotes")
            == ("stock_snapshot", "tdx_quotes"),
        }
        route = route_for("stock_snapshot", {})

        self.assertEqual(
            ("stocktoday", "tencent", "pytdx", "tdx_quotes"),
            route.providers,
        )
        self.assertTrue(all(provider in api.PROVIDER_REGISTRY for provider in route.providers))
        self.assertTrue(all(adapter_contracts[provider] for provider in route.providers))
        self.assertEqual(
            "PROVIDER_ADAPTER_MISSING",
            local_outcomes["sina"].error_code,
        )
        self.assertIn("sina", route_for("stock_kline", {"period": "60m"}).providers)


if __name__ == "__main__":
    unittest.main()
