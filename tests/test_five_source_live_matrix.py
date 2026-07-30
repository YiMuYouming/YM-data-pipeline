from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.smoke import run_live_smoke
from ym_stock_data.smoke_contract import CASE_SPECS, CURRENT_SMOKE_BASELINE, CURRENT_SMOKE_CASE_IDS


EXPECTED_CASE_IDS = (
    "zero_realtime_market", "zero_sector_index", "zero_stock_snapshot",
    "zero_stock_kline", "zero_review_sentiment", "zero_market_limit_state",
    "zero_stock_event", "explicit_wencai", "explicit_structured_screener",
    "tdx_probe", "wind_probe", "direct_openapi_screener",
    "direct_pywencai_screener", "tdx_screener_probe", "tdx_kline_probe",
    "tdx_report_probe", "tdx_notice_probe", "tdx_news_probe",
    "wind_screener_probe", "wind_filings_probe", "canonical_five_source_fallback",
)

TDX_PROTOCOL = {
    "initialize": "pass", "tools_list": "pass", "schema": "pass",
    "read_only": "pass", "tool_call": "pass", "page_count": 1,
    "session_count": 1, "refresh_count": 0, "call_count": 1,
}


def canonical_result(intent: str) -> dict:
    if intent == "stock_snapshot":
        data = {"600519": {"price": 1}}
    elif intent == "stock_kline":
        data = {"bars": [{"close": 1}]}
    elif intent in {"stock_event", "news"}:
        data = {"items": [{"fixture": True}]}
    elif intent == "sector_index":
        data = {"items": [{"fixture": True}]}
    elif intent == "research":
        data = {"reports": [{"fixture": True}]}
    elif intent == "filings":
        data = {"filings": [{"fixture": True}]}
    elif intent == "wind_enrichment":
        data = {"items": [{"fixture": True}]}
    elif intent == "review_sentiment":
        data = {"datas": [{"fixture": True}], "row_count": 1}
    elif intent == "market_limit_state":
        data = {"zt_count": 1, "zb_count": 0, "dt_count": 0, "break_rate": 0, "max_board": 1, "pools": {}}
    else:
        data = {"fixture": True}
    return {
        "data": data,
        "_meta": {
            "intent": intent, "status": "success", "provider_used": "fixture",
            "attempts": [{"provider": "fixture", "status": "success", "error_code": None, "latency_ms": 1}],
            "quality": {"returned_count": 1},
        },
    }


def outcome_for(name: str, intent: str) -> ProviderOutcome:
    provenance = {"smoke_protocol": TDX_PROTOCOL} if name.startswith("tdx_") else None
    return ProviderOutcome(
        provider=name, status="success", data=canonical_result(intent)["data"],
        quality={"returned_count": 1}, provenance=provenance,
    )


class FakeProvider:
    def __init__(self, name: str, calls: list[tuple[str, str, dict]], *, empty=False):
        self.name = name
        self.calls = calls
        self.empty = empty

    def call(self, intent: str, params: dict) -> ProviderOutcome:
        self.calls.append((self.name, intent, params))
        if self.empty:
            container = {"filings": "filings", "review_sentiment": "datas"}.get(intent, "items")
            return ProviderOutcome(provider=self.name, status="empty", data={container: []}, quality={"returned_count": 0})
        return outcome_for(self.name, intent)


class FiveSourceLiveMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.output_dir = Path(self.temp_dir.name) / "smoke"

    def _run(self, *, empty_provider: str | None = None) -> tuple[dict, list]:
        calls: list[tuple[str, str, dict]] = []

        def provider_loader(name: str):
            return FakeProvider(name, calls, empty=name == empty_provider)

        diagnostics = {"providers": {
            "iwencai_openapi": {"status": "configured_unverified"},
            "pywencai": {"status": "configured_unverified"},
            "pytdx_screener": {"status": "configured_unverified"},
            "tdx_mcp": {"status": "configured_unverified"},
            "wind_mcp": {"status": "configured_unverified"},
        }}
        result = run_live_smoke(
            output_dir=self.output_dir,
            query_fn=lambda intent, **_params: canonical_result(intent),
            diagnostics_fn=lambda: diagnostics,
            provider_loader=provider_loader,
            now_fn=lambda: datetime(2026, 7, 30, 16, 20, tzinfo=TZ_SHANGHAI),
            case_timeout_sec=1, total_timeout_sec=30,
        )
        report = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
        return report, calls

    def test_contract_locks_twenty_one_cases_and_sanitized_metadata(self) -> None:
        self.assertEqual("five-source-capabilities-v1", CURRENT_SMOKE_BASELINE)
        self.assertEqual(EXPECTED_CASE_IDS, CURRENT_SMOKE_CASE_IDS)
        self.assertEqual(21, len(CASE_SPECS))
        for spec in CASE_SPECS:
            self.assertTrue(spec.evidence_kind)
            self.assertTrue(spec.capability)
            self.assertNotIn("code", spec.safe_params())
            self.assertNotIn("codes", spec.safe_params())

    def test_live_matrix_probes_all_direct_capabilities_and_passes_gate(self) -> None:
        report, calls = self._run()
        self.assertEqual(21, report["summary"]["total"])
        self.assertEqual({"iwencai_openapi": "pass", "pywencai": "pass", "tdx": "pass", "wind": "pass", "pytdx": "pass"}, report["source_status"])
        self.assertEqual("pass", report["chain_status"])
        self.assertEqual("pass", report["gate_status"])
        tdx_ids = {"tdx_probe", "tdx_screener_probe", "tdx_kline_probe", "tdx_report_probe", "tdx_notice_probe", "tdx_news_probe"}
        tdx_cases = [case for case in report["cases"] if case["case_id"] in tdx_ids]
        self.assertEqual(6, len(tdx_cases))
        for case in tdx_cases:
            self.assertEqual(TDX_PROTOCOL, case["protocol_evidence"])
        counts = {name: sum(call[0] == name for call in calls) for name in {
            "iwencai_openapi", "pywencai", "tdx_screener", "wind_screener", "pytdx_screener",
        }}
        self.assertEqual(1, counts["iwencai_openapi"])
        self.assertEqual(1, counts["pywencai"])
        self.assertEqual(1, counts["tdx_screener"])
        self.assertEqual(1, counts["wind_screener"])
        self.assertEqual(2, counts["pytdx_screener"])

    def test_controlled_fallback_uses_real_router_and_marks_injected_origins(self) -> None:
        report, _calls = self._run()
        case = next(item for item in report["cases"] if item["case_id"] == "canonical_five_source_fallback")
        self.assertEqual("degraded", case["status"])
        self.assertEqual("pytdx_screener", case["provider_used"])
        self.assertEqual(["iwencai_openapi", "pywencai", "tdx_screener", "wind_screener", "pytdx_screener"], [a["provider"] for a in case["attempts"]])
        self.assertEqual(["injected", "injected", "injected", "injected", "live"], [a["origin"] for a in case["attempts"]])
        self.assertEqual(["auth_error", "provider_error", "auth_error", "empty", "success"], [a["status"] for a in case["attempts"]])

    def test_empty_direct_capability_fails_source_and_gate_without_stopping_matrix(self) -> None:
        report, calls = self._run(empty_provider="wind_documents")
        self.assertEqual("fail", report["source_status"]["wind"])
        self.assertEqual("fail", report["gate_status"])
        self.assertEqual(21, len(report["cases"]))
        self.assertTrue(any(name == "pytdx_screener" for name, _intent, _params in calls))

    def test_receipt_has_no_business_selectors_or_forbidden_payloads(self) -> None:
        report, _calls = self._run()
        serialized = json.dumps(report, ensure_ascii=False).lower()
        for forbidden in ('"data"', '"rows"', '"query"', '"code"', '"codes"', '"stdout"', '"stderr"', '"session"', "bearer ", "600519", "沪深a股"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
