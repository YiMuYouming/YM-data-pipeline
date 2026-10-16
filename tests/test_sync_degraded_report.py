"""A degraded derivative report is not proof that the core store is unsealed."""

import copy
import unittest
from unittest.mock import patch

from ym_stock_data import market_facts_sync as sync


DAY = "20261016"


def report():
    # Production envelope shape; tests own the fixture and never read live files.
    return {
        "data": {
            "trade_date": DAY,
            "counts": {"up": 61, "down": 10, "broken": 49},
            "yimu_emotion": {"up": 3301, "flat": 138, "down": 2128},
            "source_gaps": ["break_risk_price_missing"],
            "limit_evidence": {"current": {
                "phase": "post_close_observed", "payload_sha256": "a" * 64}},
            "limit_daily_quality": {key: {
                "checked": True, "missing_codes": [], "nonpositive_codes": []}
                for key in ("current", "previous")},
        },
        "_meta": {
            "status": "degraded", "provider_used": "market_facts",
            "data_as_of": "2026-10-16T15:00:00+08:00",
            "attempts": [{"provider": "market_facts", "status": "success",
                          "error_code": None}],
            "quality": {"status": "partial",
                        "reason_codes": ["break_risk_price_missing"]},
        },
    }


def raw_seal():
    return {"ready": True, "probe": "ssh", "reason": "sealed", "fingerprint": {
        "trade_date": DAY, "tables": {
            "limit_runs": {"runs": 1, "rows": 120, "payload_sha256": "a" * 64},
            "daily_runs": {"runs": 1, "rows": 5567, "payload_sha256": "b" * 64},
        }}}


class DegradedReportSealTest(unittest.TestCase):
    def probe(self, payload, raw=None):
        with patch.object(sync, "http_seal_status", return_value=sync.parse_live_payload(payload, DAY)), \
                patch.object(sync, "ssh_seal_status", return_value=raw or raw_seal()) as ssh:
            result = sync.seal_status(DAY)
        return result, ssh

    def test_degraded_report_is_not_ready_without_raw_verification(self):
        result = sync.parse_live_payload(report(), DAY)
        self.assertFalse(result["ready"])
        self.assertEqual("core_seal_verification_required", result["reason"])

    def test_raw_core_proof_allows_sync_and_retains_degradation(self):
        payload = report()
        before = copy.deepcopy(payload)
        result, ssh = self.probe(payload)
        self.assertTrue(ssh.called)
        self.assertTrue(result["ready"], result)
        self.assertEqual("degraded", result["http_report"]["status"])
        self.assertEqual(["break_risk_price_missing"], result["http_report"]["source_gaps"])
        self.assertEqual(payload, before, "readiness must not mutate report quality")

    def test_other_or_undisclosed_quality_reasons_do_not_probe_around_rejection(self):
        for gap in ("current_limit_daily_universe_conflict", "break_risk_window_day_missing", ""):
            with self.subTest(gap=gap):
                payload = report()
                payload["_meta"]["quality"]["reason_codes"].append(gap)
                result, ssh = self.probe(payload)
                self.assertFalse(result["ready"])
                ssh.assert_not_called()

    def test_invalid_core_or_failed_attempt_keeps_explicit_rejection(self):
        for field in ("daily_quality", "phase", "date", "attempt", "gaps", "counts"):
            with self.subTest(field=field):
                payload = report()
                if field == "daily_quality":
                    payload["data"]["limit_daily_quality"]["current"]["missing_codes"] = ["000001"]
                elif field == "phase":
                    payload["data"]["limit_evidence"]["current"]["phase"] = "intraday"
                elif field == "date":
                    payload["_meta"]["data_as_of"] = "2026-10-15T15:00:00+08:00"
                elif field == "attempt":
                    payload["_meta"]["attempts"][0]["error_code"] = "FACT_DAY_MISSING"
                elif field == "gaps":
                    payload["data"]["source_gaps"].append("other_missing")
                else:
                    payload["data"]["counts"]["up"] = None
                result, ssh = self.probe(payload)
                self.assertFalse(result["ready"])
                ssh.assert_not_called()

    def test_missing_raw_seal_is_not_ready(self):
        result, ssh = self.probe(report(), {"ready": False, "reason": "not_sealed", "probe": "ssh"})
        self.assertTrue(ssh.called)
        self.assertFalse(result["ready"])

    def test_malformed_core_evidence_fails_closed(self):
        for field in ("limit_evidence", "limit_daily_quality", "source_gaps"):
            with self.subTest(field=field):
                payload = report()
                payload["data"][field] = [dict(unexpected=True)]
                result, ssh = self.probe(payload)
                self.assertFalse(result["ready"])
                ssh.assert_not_called()

    def test_http_and_raw_core_must_match(self):
        for table, key, value in (("daily_runs", "rows", 5566),
                                  ("limit_runs", "rows", 119),
                                  ("limit_runs", "payload_sha256", "c" * 64),
                                  ("daily_runs", "runs", 0)):
            with self.subTest(table=table, key=key):
                raw = raw_seal()
                raw["fingerprint"]["tables"][table][key] = value
                result, _ = self.probe(report(), raw)
                self.assertFalse(result["ready"], result)
                self.assertEqual("core_seal_evidence_mismatch", result["reason"])


if __name__ == "__main__":
    unittest.main()
