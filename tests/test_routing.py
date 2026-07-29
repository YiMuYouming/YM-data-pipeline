import unittest

from ym_stock_data.routing import route_for


class RoutingTests(unittest.TestCase):
    def test_explicit_screen_uses_three_compatible_providers(self):
        spec = route_for("review_sentiment", {"query": "今日涨停 非ST"})
        self.assertEqual(
            ("iwencai_openapi", "pywencai", "tdx_screener"),
            spec.providers,
        )

    def test_default_sentiment_never_calls_natural_language_sources(self):
        spec = route_for("review_sentiment", {})
        self.assertEqual(
            ("pytdx_breadth", "eastmoney_limit_pool"),
            spec.providers,
        )

    def test_wind_is_not_a_realtime_market_fallback(self):
        self.assertNotIn("wind_mcp", route_for("realtime_market", {}).providers)


if __name__ == "__main__":
    unittest.main()
