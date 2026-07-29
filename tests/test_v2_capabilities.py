import unittest

from ym_stock_data.v2 import capability_manifest
from ym_stock_data.v2.capabilities import CAPABILITY_SCHEMA_VERSION


class CapabilityManifestTests(unittest.TestCase):
    def test_manifest_has_stable_version_and_existing_v2_intents(self):
        manifest = capability_manifest()

        self.assertEqual("1.0", CAPABILITY_SCHEMA_VERSION)
        self.assertEqual("1.0", manifest["schema_version"])
        self.assertEqual(
            {
                "realtime_market",
                "sector_index",
                "stock_snapshot",
                "stock_kline",
                "review_sentiment",
                "market_limit_state",
                "stock_event",
            },
            set(manifest["v2_intents"]),
        )
        for intent in (
            "realtime_market",
            "sector_index",
            "stock_snapshot",
            "stock_kline",
            "review_sentiment",
        ):
            self.assertEqual(
                "stable", manifest["v2_intents"][intent]["status"]
            )

    def test_manifest_exposes_new_v2_intents_as_experimental(self):
        manifest = capability_manifest()

        self.assertEqual(
            "experimental",
            manifest["v2_intents"]["market_limit_state"]["status"],
        )
        self.assertEqual(
            "experimental",
            manifest["v2_intents"]["stock_event"]["status"],
        )

    def test_manifest_exposes_current_v1_compatibility_and_registered_tdx(self):
        manifest = capability_manifest()

        for route in (
            "limit_state",
            "market_limit_state",
            "stock_event",
            "iwencai_content",
            "industry_research",
        ):
            self.assertEqual(
                "experimental", manifest["v1_routes"][route]["status"]
            )
        tdx = manifest["providers"]["tdx_mcp"]
        self.assertTrue(tdx["registered"])
        self.assertEqual("registered_optional", tdx["status"])
        self.assertEqual(tdx, manifest["manual_sources"]["tdx_mcp"])

    def test_manifest_exposes_wind_from_registered_routes(self):
        manifest = capability_manifest()

        wind = manifest["providers"]["wind_mcp"]
        self.assertEqual("registered_experimental", wind["status"])
        self.assertTrue(wind["registered"])
        self.assertEqual(["filings"], wind["automatic_fallback_intents"])
        self.assertEqual(["wind_enrichment"], wind["explicit_intents"])
        self.assertFalse(wind["default_route"])
        self.assertEqual(
            sorted(
                [
                "company_profile",
                "fundamentals",
                "equity_holders",
                "company_events",
                "risk_metrics",
                "index_fundamentals",
                "announcements",
                ]
            ),
            wind["capabilities"],
        )
        self.assertEqual(wind, manifest["manual_sources"]["wind_mcp"])

    def test_manifest_returns_an_isolated_copy(self):
        first = capability_manifest()
        first["v2_intents"]["realtime_market"]["status"] = "broken"

        second = capability_manifest()

        self.assertEqual(
            "stable", second["v2_intents"]["realtime_market"]["status"]
        )


if __name__ == "__main__":
    unittest.main()
