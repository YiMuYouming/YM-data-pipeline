import unittest

from ym_stock_data.contracts import ProviderAttempt, build_result, validate_result


class ResultContractTests(unittest.TestCase):
    def test_success_uses_actual_provider_and_preserves_attempt_order(self):
        result = build_result(
            intent="review_sentiment",
            data={"rows": [{"股票代码": "600519"}]},
            status="degraded",
            provider_used="pywencai",
            attempts=[
                ProviderAttempt("iwencai_openapi", "auth_error", "HTTP_401", 10),
                ProviderAttempt("pywencai", "success", None, 20),
            ],
            data_scope="问财自然语言选股口径",
            trade_usage="辅助，不单独触发交易",
            quality={"status": "normal", "returned_count": 1, "reason_codes": []},
            max_age_sec=1800,
        )
        self.assertEqual("pywencai", result["_meta"]["provider_used"])
        self.assertEqual("pywencai", result["_meta"]["source"])
        self.assertEqual(
            ["iwencai_openapi", "pywencai"],
            result["_meta"]["source_chain"],
        )
        self.assertEqual("1.0", result["_meta"]["contract_version"])
        validate_result(result)

    def test_total_failure_has_no_provider_used(self):
        result = build_result(
            intent="review_sentiment",
            data=None,
            status="error",
            provider_used=None,
            attempts=[ProviderAttempt("iwencai_openapi", "auth_error", "HTTP_401", 10)],
            data_scope="问财自然语言选股口径",
            trade_usage="辅助，不单独触发交易",
            quality={
                "status": "error",
                "returned_count": 0,
                "reason_codes": ["source_error"],
            },
            max_age_sec=1800,
        )
        self.assertIsNone(result["_meta"]["provider_used"])
        validate_result(result)


if __name__ == "__main__":
    unittest.main()
