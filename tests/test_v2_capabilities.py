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
            },
            set(manifest["v2_intents"]),
        )
        self.assertTrue(
            all(
                item["status"] == "stable"
                for item in manifest["v2_intents"].values()
            )
        )

    def test_manifest_exposes_current_v1_sidecars_and_manual_boundary(self):
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
        self.assertEqual(
            "manual_cross_check_only",
            manifest["manual_sources"]["tdx_mcp"]["status"],
        )

    def test_manifest_returns_an_isolated_copy(self):
        first = capability_manifest()
        first["v2_intents"]["realtime_market"]["status"] = "broken"

        second = capability_manifest()

        self.assertEqual(
            "stable", second["v2_intents"]["realtime_market"]["status"]
        )


if __name__ == "__main__":
    unittest.main()
