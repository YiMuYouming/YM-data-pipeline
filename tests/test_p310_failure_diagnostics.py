"""P10/#9：失败信封与失败诊断必须带 reason_code/http_status/attempts。

改前 RED（2026-10-27 真实巡检）：record_fact industry_flow 连报 7 次 internal_error，
无 reason_code/http_status/attempts；DP 原始响应里已有 attempts（stocktoday TIMEOUT、
ths_industry DATE_UNVERIFIED）与 quality.reason_codes（source_error/empty_result），
但信封没有顶层 reason_code/http_status，TB 网关路线又把 attempts 丢掉。
"""

from __future__ import annotations

import unittest

from ym_stock_data import api
from ym_stock_data.contracts import ProviderAttempt, build_result


def failure_envelope(**overrides):
    result = build_result(
        intent="industry_flow", data=None, status="error", provider_used=None,
        attempts=[ProviderAttempt("stocktoday", "timeout", "TIMEOUT", 15079),
                  ProviderAttempt("ths_industry", "incompatible", "DATE_UNVERIFIED", 0)],
        data_scope="行业资金流", trade_usage="observation_only",
        quality={"status": "error", "returned_count": 0,
                 "reason_codes": ["source_error", "empty_result"]},
        max_age_sec=300, fetched_at="2026-10-27T11:25:00+08:00")
    result["_meta"].update(overrides)
    return result


class FailureDiagnosticsTests(unittest.TestCase):
    def test_reason_code_comes_from_the_first_source_error(self):
        result = api.annotate_failure_diagnostics(failure_envelope())
        self.assertEqual("TIMEOUT", result["_meta"]["reason_code"])
        self.assertFalse(result["ok"])
        self.assertEqual(2, result["_meta"]["attempts_used"])

    def test_http_status_is_propagated_when_the_attempt_has_one(self):
        result = api.annotate_failure_diagnostics(failure_envelope(
            attempt_diagnostics=[{"provider": "stocktoday", "error_code": "TIMEOUT",
                                  "http_status": 504, "elapsed_ms": 15079}]))
        self.assertEqual(504, result["_meta"]["http_status"])

    def test_missing_evidence_stays_none_not_fabricated(self):
        result = api.annotate_failure_diagnostics(failure_envelope())
        self.assertIsNone(result["_meta"]["http_status"])

    def test_existing_reason_code_is_not_overwritten(self):
        result = api.annotate_failure_diagnostics(
            failure_envelope(reason_code="UPSTREAM_TIMEOUT"))
        self.assertEqual("UPSTREAM_TIMEOUT", result["_meta"]["reason_code"])


if __name__ == "__main__":
    unittest.main()
