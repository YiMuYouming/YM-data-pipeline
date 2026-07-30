import io
import json
import os
import stat
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from ym_stock_data.__main__ import main
from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.smoke import run_live_smoke, summarize_query_result
from ym_stock_data.smoke_contract import CURRENT_SMOKE_CASE_IDS


CURRENT_CASE_IDS = CURRENT_SMOKE_CASE_IDS
CURRENT_CASE_METADATA = {
    "explicit_structured_screener": (
        "five_source_fallback",
        "review_sentiment",
        {"sample_id": "structured_hs_a", "limit": 3},
    ),
    "tdx_probe": ("owned_oauth", "stock_snapshot", {"fixture_id": "large_cap_a"}),
    "wind_probe": (
        "official_cli",
        "wind_enrichment",
        {"capability": "company_profile", "fixture_id": "large_cap_a"},
    ),
}


def result(intent, *, status="success", provider="fake", rows=None):
    rows = rows if rows is not None else [{"SECRET_ROW": "must-not-persist"}]
    return {
        "data": {"items": rows},
        "_meta": {
            "intent": intent,
            "status": status,
            "provider_used": provider,
            "attempts": [
                {
                    "provider": provider,
                    "status": "success",
                    "error_code": None,
                    "latency_ms": 7,
                }
            ],
            "quality": {"returned_count": len(rows)},
        },
    }


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "smoke"

    def test_smoke_without_live_never_calls_or_writes(self):
        output = io.StringIO()
        with patch("ym_stock_data.__main__.run_live_smoke") as run, redirect_stdout(output):
            exit_code = main(["smoke"])

        self.assertEqual(2, exit_code)
        run.assert_not_called()
        self.assertEqual("not_run", json.loads(output.getvalue())["status"])

    def test_general_summary_keeps_legacy_attempt_shape_until_smoke_opts_in(self):
        value = result("review_sentiment")

        general = summarize_query_result(value)
        smoke = summarize_query_result(value, include_origin=True)

        self.assertNotIn("origin", general["attempts"][0])
        self.assertEqual("live", smoke["attempts"][0]["origin"])

    def test_smoke_live_cli_prints_receipt_summary_without_cases(self):
        output = io.StringIO()
        receipt = {
            "receipt": str(self.root / "receipt.json"),
            "summary": {"total": 21, "status_counts": {"success": 21}},
            "source_status": {
                "iwencai_openapi": "pass",
                "pywencai": "pass",
                "tdx": "pass",
                "wind": "pass",
                "pytdx": "pass",
            },
            "chain_status": "pass",
            "gate_status": "pass",
            "cases": [{"SECRET_ROW": True}],
        }
        with patch(
            "ym_stock_data.__main__.run_live_smoke", return_value=receipt
        ) as run, redirect_stdout(output):
            exit_code = main(
                ["smoke", "--live", "--case-timeout", "12", "--total-timeout", "90"]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(0, exit_code)
        run.assert_called_once_with(case_timeout_sec=12.0, total_timeout_sec=90.0)
        self.assertEqual("complete", payload["status"])
        self.assertIn("gate_status", payload)
        self.assertEqual("pass", payload["gate_status"])
        self.assertEqual("pass", payload["chain_status"])
        self.assertEqual(receipt["source_status"], payload["source_status"])
        self.assertNotIn("cases", payload)
        self.assertNotIn("SECRET_ROW", output.getvalue())

    def test_smoke_live_cli_keeps_failed_gate_receipt_and_returns_nonzero(self):
        output = io.StringIO()
        receipt = {
            "receipt": str(self.root / "failed.json"),
            "summary": {"total": 21, "status_counts": {"timeout": 21}},
            "source_status": {
                "iwencai_openapi": "fail",
                "pywencai": "fail",
                "tdx": "fail",
                "wind": "fail",
                "pytdx": "fail",
            },
            "chain_status": "fail",
            "gate_status": "fail",
            "cases": [{"SECRET_ROW": True}],
        }
        with patch(
            "ym_stock_data.__main__.run_live_smoke", return_value=receipt
        ), redirect_stdout(output):
            exit_code = main(["smoke", "--live"])

        payload = json.loads(output.getvalue())
        self.assertEqual(2, exit_code)
        self.assertEqual("complete", payload["status"])
        self.assertEqual(str(self.root / "failed.json"), payload["receipt"])
        self.assertEqual("fail", payload["gate_status"])
        self.assertNotIn("cases", payload)
        self.assertNotIn("SECRET_ROW", output.getvalue())

    def test_live_matrix_is_sanitized_independent_and_privately_written(self):
        calls = []
        providers = {}

        def provider_loader(name):
            if name not in providers:
                provider = Mock()
                container = {
                    "tdx_quotes": {"items": []},
                    "tdx_kline": {"bars": []},
                    "tdx_report": {"reports": []},
                    "tdx_notice": {"filings": []},
                    "tdx_news": {"items": []},
                    "wind_mcp": {"items": []},
                    "wind_documents": {"filings": []},
                }.get(name, {"datas": [], "row_count": 0})
                provider.call.return_value = ProviderOutcome(
                    provider=name,
                    status="empty",
                    data=container,
                    quality={"returned_count": 0},
                    auth={"required": False, "status": "not_required"},
                    latency_ms=9,
                )
                providers[name] = provider
            return providers[name]

        def fake_query(intent, **params):
            calls.append((intent, params))
            if intent == "sector_index":
                raise RuntimeError("SECRET_EXCEPTION token query-row")
            return result(intent)

        diagnostics = {
            "providers": {
                "tdx_mcp": {"status": "auth_missing"},
                "wind_mcp": {"status": "configured_unverified"},
                "iwencai_openapi": {"status": "configured_unverified"},
                "pywencai": {"status": "configured_unverified"},
                "pytdx_screener": {"status": "configured_unverified"},
            }
        }
        receipt = run_live_smoke(
            output_dir=self.root,
            query_fn=fake_query,
            diagnostics_fn=lambda: diagnostics,
            provider_loader=provider_loader,
            now_fn=lambda: datetime(2026, 7, 29, 12, 34, 56, tzinfo=TZ_SHANGHAI),
            case_timeout_sec=1,
            total_timeout_sec=20,
        )

        path = Path(receipt["receipt"])
        report = json.loads(path.read_text(encoding="utf-8"))
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertEqual("2", report["schema_version"])
        self.assertEqual("five-source-capabilities-v1", report["baseline"])
        self.assertEqual(21, len(report["cases"]))
        self.assertEqual(CURRENT_CASE_IDS, tuple(case["case_id"] for case in report["cases"]))
        for case_id, (category, intent, params) in CURRENT_CASE_METADATA.items():
            case = next(item for item in report["cases"] if item["case_id"] == case_id)
            self.assertEqual(category, case["category"])
            self.assertEqual(intent, case["intent"])
            self.assertEqual(params, case["params"])
        self.assertEqual("auth_missing", next(
            case["status"] for case in report["cases"] if case["case_id"] == "tdx_probe"
        ))
        self.assertEqual("error", next(
            case["status"] for case in report["cases"] if case["case_id"] == "zero_sector_index"
        ))
        self.assertIn("zero_stock_snapshot", {case["case_id"] for case in report["cases"]})
        self.assertIn("wind_probe", {case["case_id"] for case in report["cases"]})
        self.assertIn(
            "explicit_structured_screener",
            {case["case_id"] for case in report["cases"]},
        )
        self.assertEqual(8, len(calls))
        self.assertEqual(2, providers["pytdx_screener"].call.call_count)
        for forbidden in (
            "SECRET_ROW",
            "SECRET_EXCEPTION",
            "token",
            "response",
            "stderr",
            "query-row",
            "A股 非ST 涨停",
            "沪深A股 非ST 非停牌 最新价>=1",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(0o700, stat.S_IMODE(self.root.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual([], list(self.root.glob("*.tmp")))
        self.assertIn("source_status", receipt)
        self.assertEqual(report["source_status"], receipt["source_status"])
        self.assertEqual(report["chain_status"], receipt["chain_status"])
        self.assertEqual(report["gate_status"], receipt["gate_status"])

    def test_each_case_timeout_is_sanitized_and_matrix_continues(self):
        def slow_query(intent, **params):
            if intent == "realtime_market":
                time.sleep(0.05)
            return result(intent, rows=[])

        receipt = run_live_smoke(
            output_dir=self.root,
            query_fn=slow_query,
            diagnostics_fn=lambda: {
                "providers": {
                    "tdx_mcp": {"status": "auth_missing"},
                    "wind_mcp": {"status": "dependency_missing"},
                }
            },
            provider_loader=lambda name: Mock(
                call=Mock(
                    return_value=ProviderOutcome(
                        provider=name,
                        status="empty",
                        data={"datas": [], "row_count": 0},
                        quality={"returned_count": 0},
                        auth={"required": False, "status": "not_required"},
                    )
                )
            ),
            now_fn=lambda: datetime(2026, 7, 29, 12, 34, 56, tzinfo=TZ_SHANGHAI),
            case_timeout_sec=0.01,
            total_timeout_sec=2,
        )

        report = json.loads(Path(receipt["receipt"]).read_text(encoding="utf-8"))
        first = next(case for case in report["cases"] if case["case_id"] == "zero_realtime_market")
        later = next(case for case in report["cases"] if case["case_id"] == "zero_stock_snapshot")
        self.assertEqual("timeout", first["status"])
        self.assertEqual("SMOKE_TIMEOUT", first["error_code"])
        self.assertIn(later["status"], {"success", "empty"})

    def test_hanging_diagnostics_obeys_total_deadline_and_writes_failed_matrix(self):
        def hanging_diagnostics():
            time.sleep(0.2)
            return {"providers": {}}

        started = time.monotonic()
        receipt = run_live_smoke(
            output_dir=self.root,
            query_fn=lambda intent, **_params: result(intent),
            diagnostics_fn=hanging_diagnostics,
            provider_loader=lambda name: Mock(
                call=Mock(return_value=ProviderOutcome(name, "provider_error"))
            ),
            now_fn=lambda: datetime(2026, 7, 29, 12, 34, 56, tzinfo=TZ_SHANGHAI),
            case_timeout_sec=1,
            total_timeout_sec=0.02,
        )
        elapsed = time.monotonic() - started

        report = json.loads(Path(receipt["receipt"]).read_text(encoding="utf-8"))
        self.assertLess(elapsed, 0.15)
        self.assertEqual(21, len(report["cases"]))
        self.assertEqual("fail", report["gate_status"])
        self.assertEqual("fail", receipt["gate_status"])
        self.assertTrue(all(case["status"] == "timeout" for case in report["cases"]))
        self.assertTrue(all(case["error_code"] == "TOTAL_TIMEOUT" for case in report["cases"]))


if __name__ == "__main__":
    unittest.main()
