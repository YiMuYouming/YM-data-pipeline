import unittest

from scripts.compare_external_sources import build_comparison


class CompareExternalSourcesTests(unittest.TestCase):
    def test_comparison_uses_public_metadata_and_actual_doctor_state(self):
        def query_fn(intent, **params):
            return {
                "data": {"rows": [{"SECRET_ROW": True}]},
                "_meta": {
                    "status": "degraded",
                    "provider_used": "pywencai",
                    "attempts": [
                        {
                            "provider": "iwencai_openapi",
                            "status": "auth_error",
                            "error_code": "HTTP_401",
                            "latency_ms": 3,
                        },
                        {
                            "provider": "pywencai",
                            "status": "success",
                            "error_code": None,
                            "latency_ms": 4,
                        },
                    ],
                    "quality": {"returned_count": 1},
                },
            }

        result = build_comparison(
            query_fn=query_fn,
            diagnostics_fn=lambda: {
                "providers": {
                    "tdx_mcp": {"status": "auth_missing"},
                    "wind_mcp": {"status": "configured_unverified"},
                }
            },
        )

        self.assertNotIn("manual_tdx_mcp", result)
        self.assertEqual("auth_missing", result["providers"]["tdx_mcp"]["status"])
        self.assertEqual("degraded", result["queries"]["review_sentiment"]["status"])
        self.assertNotIn("SECRET_ROW", str(result))


if __name__ == "__main__":
    unittest.main()
