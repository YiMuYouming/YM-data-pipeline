import unittest
from unittest.mock import patch

import ym_stock_data.api as api
from ym_stock_data.providers.local import LocalProvider
from ym_stock_data.routing import route_for


class RoutingTests(unittest.TestCase):
    def test_arbitrary_explicit_screen_is_research_only_without_tdx(self):
        spec = route_for("review_sentiment", {"query": "今日涨停 非ST"})
        self.assertEqual(
            ("iwencai_openapi", "pywencai", "wind_screener"),
            spec.providers,
        )
        self.assertEqual("continue_until_exhausted", spec.empty_policy)

    def test_default_sentiment_reads_the_indicator_ssot(self):
        spec = route_for("review_sentiment", {})
        self.assertEqual(("stocktoday",), spec.providers)
        self.assertEqual("stop", spec.empty_policy)

    def test_market_intraday_state_has_no_fallback(self):
        spec = route_for("market_intraday_state", {})
        self.assertEqual(("stocktoday",), spec.providers)
        self.assertEqual("stop", spec.empty_policy)

    def test_sector_index_keeps_its_existing_price_semantics(self):
        spec = route_for("sector_index", {"names": ["不存在板块"]})
        self.assertEqual(("ths_industry",), spec.providers)
        self.assertEqual("stop", spec.empty_policy)

    def test_limit_facts_are_stocktoday_only(self):
        for intent, params in (
            ("market_limit_board", {"kind": "up"}),
            ("market_limit_state", {}),
        ):
            with self.subTest(intent=intent):
                self.assertEqual(("stocktoday",), route_for(intent, params).providers)
        hot = route_for("market_hot_rank", {"source": "ths"})
        self.assertEqual(("stocktoday",), hot.providers)
        self.assertEqual("stop", hot.empty_policy)
        sector = route_for("sector_index", {"names": ["半导体"]})
        self.assertEqual(("ths_industry",), sector.providers)
        self.assertEqual("stop", sector.empty_policy)

    def test_wind_is_not_a_realtime_market_fallback(self):
        self.assertNotIn("wind_mcp", route_for("realtime_market", {}).providers)

    def test_stocktoday_first_and_tencent_is_the_only_quote_fallback(self):
        for params in ({}, {"use_case": "realtime_poll"}):
            with self.subTest(params=params):
                self.assertEqual(
                    ("stocktoday", "tencent"),
                    route_for("realtime_market", params).providers,
                )
                self.assertEqual(
                    ("stocktoday", "tencent"),
                    route_for("stock_snapshot", params).providers,
                )
        for period in ("daily", "weekly", "monthly"):
            with self.subTest(period=period):
                spec = route_for("stock_kline", {"period": period})
                self.assertEqual(("stocktoday", "tencent"), spec.providers)
                self.assertEqual("continue_until_exhausted", spec.empty_policy)
        minute = route_for("stock_kline", {"period": "60m"})
        self.assertEqual(("stocktoday",), minute.providers)

    def test_no_default_route_uses_tdx_pytdx_or_iwencai_breadth(self):
        from ym_stock_data.routing import all_route_specs

        banned = {"pytdx", "pytdx_breadth", "pytdx_index", "eastmoney",
                  "eastmoney_breadth", "eastmoney_stock", "eastmoney_limit_pool", "sina"}
        for spec in all_route_specs():
            with self.subTest(intent=spec.intent, providers=spec.providers):
                self.assertFalse([p for p in spec.providers if p.startswith("tdx_")])
                self.assertFalse(banned & set(spec.providers))

    def test_stock_snapshot_route_matches_real_adapter_intents_without_network(self):
        route = route_for("stock_snapshot", {})
        self.assertEqual(("stocktoday", "tencent"), route.providers)
        self.assertTrue(all(provider in api.PROVIDER_REGISTRY for provider in route.providers))
        with patch(
            "ym_stock_data.providers.local.tencent.fetch_quotes",
            return_value={"600519": {"price": 1}},
        ):
            outcome = LocalProvider("tencent").call("stock_snapshot", {"codes": ["600519"]})
        self.assertNotEqual("PROVIDER_ADAPTER_MISSING", outcome.error_code)


    def test_packaged_policy_lists_exactly_the_live_routes(self):
        """The inactive policy file is read by people and agents; it must match routing.py."""
        from ym_stock_data.provider_policy import load_policy
        from ym_stock_data.routing import capability_for

        samples = {
            "stock_snapshot": ("stock_snapshot", {}),
            "stock_kline_daily": ("stock_kline", {"period": "daily"}),
            "stock_kline_weekly": ("stock_kline", {"period": "weekly"}),
            "stock_kline_monthly": ("stock_kline", {"period": "monthly"}),
            "stock_kline_60m": ("stock_kline", {"period": "60m"}),
            "stock_kline_15m": ("stock_kline", {"period": "15m"}),
            "stock_kline_5m": ("stock_kline", {"period": "5m"}),
            "sector_index": ("sector_index", {}),
            "review_sentiment": ("review_sentiment", {}),
            "market_limit_state": ("market_limit_state", {}),
            "stocktoday_data": ("stocktoday_data", {}),
        }
        policy = load_policy()
        self.assertFalse(policy["active"])
        for capability, config in policy["capabilities"].items():
            with self.subTest(capability=capability):
                intent, params = samples[capability]
                self.assertEqual(capability, capability_for(intent, params))
                spec = route_for(intent, params)
                self.assertEqual(spec.providers, tuple(config["provider_order"]))
                self.assertEqual(spec.empty_policy, config["empty_policy"])
                self.assertEqual(spec.max_age_sec, config["max_age_sec"])


if __name__ == "__main__":
    unittest.main()
