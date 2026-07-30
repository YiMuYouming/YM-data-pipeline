import unittest

from ym_stock_data.routing import route_for


class RoutingTests(unittest.TestCase):
    def test_arbitrary_explicit_screen_uses_four_compatible_providers(self):
        spec = route_for("review_sentiment", {"query": "今日涨停 非ST"})
        self.assertEqual(
            ("iwencai_openapi", "pywencai", "tdx_screener", "wind_screener"),
            spec.providers,
        )
        self.assertEqual("continue_until_exhausted", spec.empty_policy)

    def test_structured_screen_adds_no_auth_pytdx_fifth(self):
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
                "pytdx_screener",
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

    def test_sector_index_keeps_terminal_empty_policy(self):
        spec = route_for("sector_index", {"names": ["不存在板块"]})
        self.assertEqual("stop", spec.empty_policy)

    def test_wind_is_not_a_realtime_market_fallback(self):
        self.assertNotIn("wind_mcp", route_for("realtime_market", {}).providers)


if __name__ == "__main__":
    unittest.main()
