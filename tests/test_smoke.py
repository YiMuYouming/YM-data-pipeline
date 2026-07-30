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
from ym_stock_data.smoke import run_live_smoke


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

    def test_smoke_live_cli_prints_receipt_summary_without_cases(self):
        output = io.StringIO()
        receipt = {
            "receipt": str(self.root / "receipt.json"),
            "summary": {"total": 11, "status_counts": {"success": 11}},
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
        self.assertNotIn("cases", payload)
        self.assertNotIn("SECRET_ROW", output.getvalue())

    def test_live_matrix_is_sanitized_independent_and_privately_written(self):
        calls = []

        def fake_query(intent, **params):
            calls.append((intent, params))
            if intent == "sector_index":
                raise RuntimeError("SECRET_EXCEPTION token query-row")
            return result(intent)

        diagnostics = {
            "providers": {
                "tdx_mcp": {"status": "auth_missing"},
                "wind_mcp": {"status": "configured_unverified"},
            }
        }
        receipt = run_live_smoke(
            output_dir=self.root,
            query_fn=fake_query,
            diagnostics_fn=lambda: diagnostics,
            provider_loader=Mock(side_effect=AssertionError("TDX must not run")),
            now_fn=lambda: datetime(2026, 7, 29, 12, 34, 56, tzinfo=TZ_SHANGHAI),
            case_timeout_sec=1,
            total_timeout_sec=20,
        )

        path = Path(receipt["receipt"])
        report = json.loads(path.read_text(encoding="utf-8"))
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertEqual(11, len(report["cases"]))
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
        self.assertEqual(10, len(calls))
        self.assertEqual("wind_enrichment", calls[-1][0])
        for forbidden in (
            "SECRET_ROW",
            "SECRET_EXCEPTION",
            "token",
            "response",
            "stderr",
            "query-row",
            "A股 非ST 涨停",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(0o700, stat.S_IMODE(self.root.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual([], list(self.root.glob("*.tmp")))

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


if __name__ == "__main__":
    unittest.main()
