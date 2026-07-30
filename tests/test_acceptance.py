from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ym_stock_data.__main__ import main
from ym_stock_data.smoke_contract import CASE_SPECS


CURRENT_SMOKE_CASE_IDS = tuple(spec.case_id for spec in CASE_SPECS)
CURRENT_SMOKE_SPECS = {
    spec.case_id: (spec.category, spec.intent, spec.safe_params()) for spec in CASE_SPECS
}
LEGACY_SMOKE_CASE_IDS = (
    "zero_realtime_market", "zero_sector_index", "zero_stock_snapshot",
    "zero_stock_kline", "zero_review_sentiment", "zero_market_limit_state",
    "zero_stock_event", "explicit_wencai", "tdx_probe", "wind_probe",
)


try:
    acceptance = importlib.import_module("ym_stock_data.acceptance")
except ModuleNotFoundError:
    acceptance = None


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path.parent, 0o700)
    os.chmod(path, mode)


class AcceptanceTests(unittest.TestCase):
    shanghai = timezone(timedelta(hours=8))

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "codex/test-acceptance"],
            cwd=self.repo,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=self.repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Acceptance Test"],
            cwd=self.repo,
            check=True,
        )
        (self.repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        package = self.repo / "ym_stock_data"
        package.mkdir()
        (package / "marker.py").write_text("MARKER = True\n", encoding="utf-8")
        launcher = self.repo / "ym-data"
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o755)
        subprocess.run(
            ["git", "add", ".gitignore", "tracked.txt", "ym-data", "ym_stock_data/marker.py"],
            cwd=self.repo,
            check=True,
        )
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", "baseline"],
            cwd=self.repo,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        protected = self.repo / "ym_stock_data/experimental/__pycache__"
        protected.mkdir(parents=True)
        (protected / "__init__.cpython-314.pyc").write_bytes(b"protected-init")
        (protected / "wind_sidecar.cpython-314.pyc").write_bytes(b"protected-wind")

        self.state = self.root / "state/acceptance"
        self.doctor_path = self.root / "inputs/doctor.json"
        self.smoke_path = self.root / "smoke/2026-07-30T161500+0800.json"
        self.downstream_path = self.root / "inputs/downstream.json"
        self.calendar_path = self.root / "inputs/calendar.json"
        self.write_inputs("2026-07-30")

    def require_module(self):
        if acceptance is None:
            self.fail("ym_stock_data.acceptance must exist")
        return acceptance

    def git_head(self) -> str:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def smoke_report(self, date: str, *, current: bool = True) -> dict:
        case_ids = CURRENT_SMOKE_CASE_IDS if current else LEGACY_SMOKE_CASE_IDS
        latencies = list(range(1, len(case_ids) + 1))
        cases = []
        for index, (case_id, latency) in enumerate(zip(case_ids, latencies), start=1):
            category, intent, params = CURRENT_SMOKE_SPECS[case_id]
            spec = next((item for item in CASE_SPECS if item.case_id == case_id), None)
            direct_provider = spec.direct_provider if current and spec else None
            provider = direct_provider or "pytdx"
            case = {
                "case_id": case_id,
                "category": category,
                "intent": intent,
                "params": params,
                "status": "success",
                "provider_used": provider,
                "attempts": [
                    {
                        "provider": provider,
                        "status": "success",
                        "error_code": None,
                        "latency_ms": latency,
                        **({"origin": "live"} if current else {}),
                    }
                ],
                "row_count": 1,
                "error_code": None,
                "latency_ms": latency,
            }
            if current:
                case.update(
                    direct_provider=direct_provider,
                    evidence_kind=spec.evidence_kind,
                    capability=spec.capability,
                    protocol_evidence=(
                        {
                            "initialize": "pass", "tools_list": "pass",
                            "schema": "pass", "read_only": "pass",
                            "tool_call": "pass", "page_count": 1,
                            "session_count": 1, "refresh_count": 0, "call_count": 1,
                        }
                        if spec.evidence_kind == "tdx_protocol_result"
                        else None
                    ),
                )
            cases.append(case)
        by_id = {case["case_id"]: case for case in cases}
        by_id["explicit_wencai"].update(
            {
                "category": "api_key",
                "intent": "review_sentiment",
                "status": "error",
                "provider_used": None,
                "attempts": [
                    {
                        "provider": "iwencai_openapi",
                        "status": "auth_error",
                        "error_code": "HTTP_401",
                        "latency_ms": 8,
                        **({"origin": "live"} if current else {}),
                    },
                    {
                        "provider": "pywencai",
                        "status": "dependency_missing",
                        "error_code": "PYWENCAI_RUNTIME_MISSING",
                        "latency_ms": 0,
                        **({"origin": "live"} if current else {}),
                    },
                ],
                "row_count": 0,
                "error_code": "PYWENCAI_RUNTIME_MISSING",
            }
        )
        if current:
            by_id["canonical_five_source_fallback"].update(
                status="degraded",
                provider_used="pytdx_screener",
                attempts=[
                    {"provider": provider, "status": status, "error_code": error_code, "latency_ms": 1, "origin": origin}
                    for provider, status, error_code, origin in (
                        ("iwencai_openapi", "auth_error", "HTTP_401", "injected"),
                        ("pywencai", "provider_error", "PYWENCAI_PROVIDER_ERROR", "injected"),
                        ("tdx_screener", "auth_error", "AUTH_EXPIRED", "injected"),
                        ("wind_screener", "empty", None, "injected"),
                        ("pytdx_screener", "success", None, "live"),
                    )
                ],
            )
        counts: dict[str, int] = {}
        for case in cases:
            counts[case["status"]] = counts.get(case["status"], 0) + 1
        report = {
            "schema_version": "2" if current else "1",
            "live": True,
            "started_at": f"{date}T16:14:00+08:00",
            "completed_at": f"{date}T16:15:00+08:00",
            "summary": {"total": len(cases), "status_counts": counts},
            "cases": cases,
        }
        if current:
            report.update(
                baseline="five-source-capabilities-v1",
                source_status={
                    "iwencai_openapi": "pass", "pywencai": "pass", "tdx": "pass",
                    "wind": "pass", "pytdx": "pass",
                },
                chain_status="pass",
                gate_status="pass",
            )
        return report

    def doctor_report(self) -> dict:
        providers = {
            "iwencai_openapi": {
                "provider": "iwencai_openapi",
                "status": "configured_unverified",
                "breaker": False,
                "auth": {"required": True, "status": "present"},
            },
            "pywencai": {
                "provider": "pywencai",
                "status": "dependency_missing",
                "action": "ym-data setup pywencai",
            },
            "pytdx_screener": {
                "provider": "pytdx_screener",
                "status": "configured_unverified",
                "auth": {"required": False, "status": "not_required"},
            },
            "tdx_mcp": {
                "provider": "tdx_mcp",
                "status": "auth_missing",
                "auth": {"required": True, "status": "missing"},
            },
            "tdx_quotes": {
                "provider": "tdx_quotes",
                "status": "auth_missing",
                "auth": {"required": True, "status": "missing"},
            },
            "wind_mcp": {
                "provider": "wind_mcp",
                "status": "configured_unverified",
                "runtime_scope": "project_compat",
                "auth": {"required": True, "status": "unverified"},
            },
        }
        counts: dict[str, int] = {}
        for item in providers.values():
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return {"schema_version": "1", "providers": providers, "summary": counts}

    def downstream_report(self) -> dict:
        return {
            "schema_version": "1",
            "breaker_verification": {
                "status": "error",
                "provider_used": None,
                "attempts": [
                    {
                        "provider": "iwencai_openapi",
                        "status": "breaker_open",
                        "error_code": "HTTP_401",
                        "latency_ms": 0,
                    }
                ],
                "row_count": 0,
                "error_code": "HTTP_401",
                "latency_ms": 2,
            },
            "market_watch": {
                "status": "success",
                "provider_used": "pytdx_breadth",
                "attempts": [
                    {
                        "provider": "pytdx_breadth",
                        "status": "success",
                        "error_code": None,
                        "latency_ms": 20,
                    }
                ],
                "quality_status": "partial",
                "returned_count": 1,
                "observation_only": True,
            },
            "live_dashboard": {
                "status": "error",
                "provider_used": None,
                "attempts": [
                    {
                        "provider": "iwencai_openapi",
                        "status": "breaker_open",
                        "error_code": "HTTP_401",
                        "latency_ms": 0,
                    }
                ],
                "row_count": 0,
                "api_mode_tested": "unified",
                "default_api_mode": "legacy",
                "comparison_status": "exact_code_set_match",
                "saved": False,
            },
            "safety": {
                "broker_or_trading_call": False,
                "business_or_production_data_write": False,
                "business_rows_stored": False,
                "credential_values_stored": False,
                "deployment": False,
                "exception_or_stderr_text_stored": False,
                "git_push": False,
                "http_8088_post": False,
                "metadata_only": True,
                "zero_secret_scan": "pass",
            },
        }

    def calendar_report(self, date: str) -> dict:
        previous = (datetime.fromisoformat(date) - timedelta(days=1)).date().isoformat()
        return {
            "schema_version": "1",
            "date": date,
            "timezone": "Asia/Shanghai",
            "weekday": datetime.fromisoformat(date).strftime("%A"),
            "is_trading_day": True,
            "confirmed": True,
            "previous_trading_date": previous,
            "official_calendar": {
                "exchange": "Shanghai Stock Exchange",
                "url": "https://www.sse.com.cn/example",
                "basis": f"{date} is not listed as a market closure",
            },
        }

    def write_inputs(self, date: str) -> None:
        write_json(self.doctor_path, self.doctor_report())
        self.smoke_path = self.root / f"smoke/{date}T161500+0800.json"
        write_json(self.smoke_path, self.smoke_report(date))
        write_json(self.downstream_path, self.downstream_report())
        write_json(self.calendar_path, self.calendar_report(date))

    def build(
        self,
        date: str = "2026-07-30",
        *,
        now: datetime | None = None,
    ) -> dict:
        module = self.require_module()
        return module.build_daily_acceptance(
            date=date,
            doctor_path=self.doctor_path,
            smoke_path=self.smoke_path,
            downstream_path=self.downstream_path,
            calendar_path=self.calendar_path,
            output_dir=self.state,
            repo_root=self.repo,
            now_fn=lambda: now
            or datetime(2026, 7, 30, 16, 20, tzinfo=self.shanghai),
        )

    def write_legacy_day1(self, date: str = "2026-07-29") -> Path:
        receipt = self.root / f"smoke/{date}T161500+0800.json"
        legacy_smoke = self.smoke_report(date, current=False)
        write_json(receipt, legacy_smoke)
        destination = self.state / f"{date}.json"
        report = {
            "schema": "ym-stock-data.acceptance.daily",
            "schema_version": "1.0",
            "generated_at": f"{date}T16:20:00+08:00",
            "observation": {
                "date": date,
                "timezone": "Asia/Shanghai",
                "weekday": "Wednesday",
                "is_trading_day": True,
                "day_count": 1,
                "required_trading_days": 5,
                "window_complete": False,
            },
            "canonical_checkout": {
                "branch": "codex/test-acceptance",
                "head": self.git_head(),
                "tracked_clean": True,
                "staged_clean": True,
            },
            "smoke_evidence": {
                "path": str(receipt),
                "sha256": sha256(receipt),
                "file_mode": "0600",
                "started_at": f"{date}T16:14:00+08:00",
                "completed_at": f"{date}T16:15:00+08:00",
                "total_cases": 10,
                "status_counts": legacy_smoke["summary"]["status_counts"],
                "cases": legacy_smoke["cases"],
            },
            "safety": {
                **self.downstream_report()["safety"],
                "smoke_rerun": False,
            },
        }
        write_json(destination, report)
        return destination

    def test_build_binds_git_receipt_permissions_latency_and_sanitized_metadata(self) -> None:
        built = self.build("2026-07-30")
        path = Path(built["path"])
        report = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual("1.3", report["schema_version"])
        self.assertEqual("codex/test-acceptance", report["canonical_checkout"]["branch"])
        self.assertEqual(self.git_head(), report["canonical_checkout"]["head"])
        self.assertTrue(report["canonical_checkout"]["tracked_clean"])
        self.assertTrue(report["canonical_checkout"]["staged_clean"])
        self.assertEqual(sha256(self.smoke_path), report["smoke_evidence"]["sha256"])
        self.assertEqual("five-source-capabilities-v1", report["smoke_evidence"]["baseline"])
        self.assertEqual(21, report["smoke_evidence"]["total_cases"])
        self.assertEqual(11, report["latency"]["p50"])
        self.assertEqual(20, report["latency"]["p95"])
        self.assertEqual(
            "success",
            report["provider_acceptance"]["pytdx_screener"]["live_status"],
        )
        self.assertEqual("nearest-rank", report["latency"]["method"])
        self.assertEqual(0o700, stat.S_IMODE(self.state.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual([], list(self.state.glob("*.tmp")))
        serialized = json.dumps(report, ensure_ascii=False)
        for forbidden_value in ("SECRET_ROW", "Bearer ", "Traceback"):
            self.assertNotIn(forbidden_value, serialized)

    def test_legacy_day_is_readable_but_does_not_count_new_baseline(self) -> None:
        module = self.require_module()
        day1 = self.write_legacy_day1()
        self.assertEqual("valid", module.validate_daily_acceptance(day1)["status"])

        built = self.build("2026-07-30")
        report = json.loads(Path(built["path"]).read_text(encoding="utf-8"))
        self.assertEqual(1, report["observation"]["observation_day_count"])
        self.assertEqual(1, report["observation"]["pass_day_count"])
        self.assertEqual(5, report["observation"]["required_trading_days"])
        self.assertFalse(report["observation"]["window_complete"])

    def test_current_baseline_history_counts_consecutive_new_receipts(self) -> None:
        first = self.build("2026-07-30")
        self.assertEqual(1, first["pass_day_count"])

        self.write_inputs("2026-07-31")
        second = self.build(
            "2026-07-31",
            now=datetime(2026, 7, 31, 16, 20, tzinfo=self.shanghai),
        )
        self.assertEqual(2, second["pass_day_count"])

    def test_failed_gate_writes_no_acceptance_and_does_not_advance_pass_count(self) -> None:
        module = self.require_module()
        smoke = self.smoke_report("2026-07-30")
        wind_case = next(
            case for case in smoke["cases"] if case["case_id"] == "wind_filings_probe"
        )
        wind_case.update(
            status="empty", provider_used="wind_documents", row_count=0
        )
        wind_case["attempts"][0]["status"] = "empty"
        smoke["summary"]["status_counts"] = {
            "degraded": 1,
            "empty": 1,
            "error": 1,
            "success": 18,
        }
        smoke["source_status"]["wind"] = "fail"
        smoke["gate_status"] = "fail"
        write_json(self.smoke_path, smoke)

        with self.assertRaises(module.AcceptanceError) as caught:
            self.build()

        self.assertEqual("SMOKE_GATE_FAILED", caught.exception.code)
        self.assertFalse((self.state / "2026-07-30.json").exists())

    def test_missing_previous_trading_day_closes_epoch_and_restarts_day_one(self) -> None:
        first = self.build("2026-07-30")
        self.assertEqual(1, first["pass_day_count"])

        self.write_inputs("2026-08-03")
        calendar = self.calendar_report("2026-08-03")
        calendar["previous_trading_date"] = "2026-07-31"
        write_json(self.calendar_path, calendar)
        restarted = self.build(
            "2026-08-03",
            now=datetime(2026, 8, 3, 16, 20, tzinfo=self.shanghai),
        )

        self.assertEqual(2, restarted["observation_day_count"])
        self.assertEqual(1, restarted["pass_day_count"])

    def test_five_consecutive_gate_passes_complete_window(self) -> None:
        schedule = (
            ("2026-07-30", "2026-07-29"),
            ("2026-07-31", "2026-07-30"),
            ("2026-08-03", "2026-07-31"),
            ("2026-08-04", "2026-08-03"),
            ("2026-08-05", "2026-08-04"),
        )
        built = None
        for index, (observed, previous) in enumerate(schedule, start=1):
            self.write_inputs(observed)
            calendar = self.calendar_report(observed)
            calendar["previous_trading_date"] = previous
            write_json(self.calendar_path, calendar)
            built = self.build(
                observed,
                now=datetime.fromisoformat(f"{observed}T16:20:00+08:00"),
            )
            self.assertEqual(index, built["pass_day_count"])
        report = json.loads(Path(built["path"]).read_text(encoding="utf-8"))
        self.assertTrue(report["observation"]["window_complete"])
        self.assertEqual("complete", report["observation"]["epoch_status"])

    def test_unpublished_v12_baseline_is_ignored_not_counted(self) -> None:
        unpublished = self.state / "2026-07-29.json"
        write_json(
            unpublished,
            {
                "schema": "ym-stock-data.acceptance.daily",
                "schema_version": "1.2",
                "smoke_evidence": {"baseline": "five-source-structured-v1"},
            },
        )

        built = self.build("2026-07-30")

        self.assertEqual(1, built["observation_day_count"])
        self.assertEqual(1, built["pass_day_count"])

    def test_current_smoke_contract_locks_baseline_and_complete_case_specs(self) -> None:
        module = self.require_module()
        mutations = (
            ("schema", lambda value: value.update(schema_version="1"), "INVALID_SMOKE_RECEIPT"),
            ("baseline", lambda value: value.update(baseline="other"), "INVALID_SMOKE_BASELINE"),
            ("missing", lambda value: value["cases"].pop(), "INVALID_CASE_IDS"),
            (
                "renamed",
                lambda value: value["cases"][0].update(case_id="renamed_case"),
                "INVALID_CASE_IDS",
            ),
            (
                "reordered",
                lambda value: value["cases"].__setitem__(
                    slice(0, 2), list(reversed(value["cases"][:2]))
                ),
                "INVALID_CASE_IDS",
            ),
            (
                "duplicate",
                lambda value: value["cases"][1].update(
                    case_id=value["cases"][0]["case_id"]
                ),
                "INVALID_CASE_IDS",
            ),
            (
                "category",
                lambda value: value["cases"][8].update(category="zero_auth"),
                "INVALID_CASE_SPEC",
            ),
            (
                "intent",
                lambda value: value["cases"][8].update(intent="stock_snapshot"),
                "INVALID_CASE_SPEC",
            ),
            (
                "params",
                lambda value: value["cases"][8]["params"].update(limit=4),
                "INVALID_CASE_SPEC",
            ),
            (
                "provider",
                lambda value: (
                    value["cases"][8].update(provider_used="wind_screener"),
                    value["cases"][8]["attempts"][0].update(
                        provider="wind_screener"
                    ),
                ),
                "INVALID_DIRECT_PROVIDER",
            ),
            (
                "unattempted_provider_spoof",
                lambda value: value["cases"][8].update(
                    status="auth_missing",
                    provider_used=None,
                    attempts=[],
                    error_code=None,
                ),
                "INVALID_DIRECT_PROVIDER",
            ),
            (
                "tdx_provider",
                lambda value: value["cases"][9].update(
                    provider_used="tdx_screener"
                ),
                "INVALID_DIRECT_PROVIDER",
            ),
            (
                "wind_provider",
                lambda value: (
                    value["cases"][10].update(provider_used="wind_documents"),
                    value["cases"][10]["attempts"][0].update(
                        provider="wind_documents"
                    ),
                ),
                "INVALID_DIRECT_PROVIDER",
            ),
            (
                "tdx_ready_without_attempt",
                lambda value: value["cases"][9].update(
                    status="ready",
                    provider_used=None,
                    attempts=[],
                    error_code=None,
                ),
                "INVALID_DIRECT_PROVIDER",
            ),
            (
                "wind_configured_without_attempt",
                lambda value: value["cases"][10].update(
                    status="configured_unverified",
                    provider_used=None,
                    attempts=[],
                    error_code=None,
                ),
                "INVALID_DIRECT_PROVIDER",
            ),
            (
                "success_without_success_attempt",
                lambda value: (
                    value["cases"][8].update(
                        status="success",
                        provider_used="pytdx_screener",
                    ),
                    value["cases"][8]["attempts"][0].update(
                        status="provider_error",
                        error_code="PYTDX_PROVIDER_ERROR",
                    ),
                ),
                "INVALID_DIRECT_PROVIDER",
            ),
            (
                "empty_without_empty_attempt",
                lambda value: value["cases"][8].update(status="empty"),
                "INVALID_DIRECT_PROVIDER",
            ),
            (
                "failure_with_provider_used",
                lambda value: value["cases"][8].update(
                    status="provider_error",
                    provider_used="pytdx_screener",
                ),
                "INVALID_DIRECT_PROVIDER",
            ),
        )
        for name, mutate, error_code in mutations:
            with self.subTest(name=name):
                self.write_inputs("2026-07-30")
                smoke = json.loads(self.smoke_path.read_text(encoding="utf-8"))
                mutate(smoke)
                smoke["summary"]["total"] = len(smoke["cases"])
                write_json(self.smoke_path, smoke)
                with self.assertRaises(module.AcceptanceError) as caught:
                    self.build()
                self.assertEqual(error_code, caught.exception.code)

    def test_direct_probe_auth_short_circuits_remain_compatible(self) -> None:
        module = self.require_module()
        for status in ("auth_missing", "auth_expired"):
            with self.subTest(status=status):
                self.write_inputs("2026-07-30")
                smoke = json.loads(self.smoke_path.read_text(encoding="utf-8"))
                tdx = smoke["cases"][9]
                tdx.update(
                    status=status,
                    provider_used=None,
                    attempts=[],
                    error_code=None,
                )
                counts: dict[str, int] = {}
                for case in smoke["cases"]:
                    counts[case["status"]] = counts.get(case["status"], 0) + 1
                smoke["summary"]["status_counts"] = counts
                smoke["source_status"]["tdx"] = "fail"
                smoke["gate_status"] = "fail"
                write_json(self.smoke_path, smoke)

                projected = module._project_smoke(
                    self.smoke_path,
                    "2026-07-30",
                    current=True,
                )
                self.assertEqual(status, projected["cases"][9]["status"])

    def test_build_rejects_forbidden_keys_and_sensitive_values(self) -> None:
        module = self.require_module()
        mutations = (
            (self.doctor_path, lambda value: value["providers"]["wind_mcp"].update(token="SECRET")),
            (self.smoke_path, lambda value: value["cases"][0].update(rows=[{"code": "600519"}])),
            (self.downstream_path, lambda value: value["market_watch"].update(query="raw query")),
            (
                self.calendar_path,
                lambda value: value["official_calendar"].update(basis="Bearer SECRET"),
            ),
        )
        for path, mutate in mutations:
            with self.subTest(path=path.name):
                self.write_inputs("2026-07-30")
                value = json.loads(path.read_text(encoding="utf-8"))
                mutate(value)
                write_json(path, value)
                with self.assertRaises(module.AcceptanceError) as caught:
                    self.build("2026-07-30")
                self.assertEqual("FORBIDDEN_INPUT", caught.exception.code)
                self.assertFalse((self.state / "2026-07-30.json").exists())

    def test_build_rejects_dirty_repo_without_writing(self) -> None:
        module = self.require_module()
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(module.AcceptanceError) as caught:
            self.build()
        self.assertEqual("GIT_NOT_CLEAN", caught.exception.code)
        self.assertFalse((self.state / "2026-07-30.json").exists())

    def test_build_never_overwrites_existing_target(self) -> None:
        module = self.require_module()
        target = self.state / "2026-07-30.json"
        write_json(target, {"sentinel": "keep"})
        before = target.read_bytes()
        with self.assertRaises(module.AcceptanceError) as caught:
            self.build()
        self.assertEqual("TARGET_EXISTS", caught.exception.code)
        self.assertEqual(before, target.read_bytes())

    def test_build_rejects_unconfirmed_nontrading_and_nonincreasing_dates(self) -> None:
        module = self.require_module()
        calendar = self.calendar_report("2026-07-30")
        calendar["confirmed"] = False
        write_json(self.calendar_path, calendar)
        with self.assertRaises(module.AcceptanceError) as caught:
            self.build()
        self.assertEqual("TRADING_DAY_UNCONFIRMED", caught.exception.code)

        self.write_inputs("2026-07-30")
        self.write_legacy_day1("2026-07-31")
        with self.assertRaises(module.AcceptanceError) as caught:
            self.build()
        self.assertEqual("DATE_NOT_INCREASING", caught.exception.code)

    def test_build_rejects_non_sse_calendar_metadata(self) -> None:
        module = self.require_module()
        mutations = (
            lambda value: value["official_calendar"].update(exchange="Other Exchange"),
            lambda value: value["official_calendar"].update(url="https://example.com/calendar"),
            lambda value: value.update(weekday="Wednesday"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.write_inputs("2026-07-30")
                calendar = json.loads(self.calendar_path.read_text(encoding="utf-8"))
                mutate(calendar)
                write_json(self.calendar_path, calendar)
                with self.assertRaises(module.AcceptanceError) as caught:
                    self.build()
                self.assertEqual("INVALID_CALENDAR", caught.exception.code)

    def test_build_rejects_before_close_and_wrong_local_date(self) -> None:
        module = self.require_module()
        with self.assertRaises(module.AcceptanceError) as caught:
            self.build(now=datetime(2026, 7, 30, 16, 9, 59, tzinfo=self.shanghai))
        self.assertEqual("OBSERVATION_TOO_EARLY", caught.exception.code)

        with self.assertRaises(module.AcceptanceError) as caught:
            self.build(now=datetime(2026, 7, 31, 16, 20, tzinfo=self.shanghai))
        self.assertEqual("OBSERVATION_DATE_MISMATCH", caught.exception.code)

    def test_build_requires_both_protected_ignored_artifacts(self) -> None:
        module = self.require_module()
        protected = self.repo / "ym_stock_data/experimental/__pycache__"
        missing = protected / "wind_sidecar.cpython-314.pyc"
        missing.unlink()
        with self.assertRaises(module.AcceptanceError) as caught:
            self.build()
        self.assertEqual("PROTECTED_ARTIFACT_MISSING", caught.exception.code)

        missing.write_bytes(b"protected-wind")
        (self.repo / ".gitignore").write_text("other-generated-file\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", "change ignore"],
            cwd=self.repo,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with self.assertRaises(module.AcceptanceError) as caught:
            self.build()
        self.assertEqual("PROTECTED_ARTIFACT_NOT_IGNORED", caught.exception.code)

    def test_validator_detects_receipt_tampering_permissions_and_day_sequence(self) -> None:
        module = self.require_module()
        self.write_legacy_day1()
        built = self.build()
        path = Path(built["path"])
        self.assertEqual("valid", module.validate_daily_acceptance(path)["status"])

        self.smoke_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(module.AcceptanceError) as caught:
            module.validate_daily_acceptance(path)
        self.assertEqual("RECEIPT_HASH_MISMATCH", caught.exception.code)

        self.write_inputs("2026-07-30")
        report = json.loads(path.read_text(encoding="utf-8"))
        report["smoke_evidence"]["sha256"] = sha256(self.smoke_path)
        report["observation"]["observation_day_count"] = 3
        report["integrity"] = module._report_integrity(report)
        write_json(path, report)
        with self.assertRaises(module.AcceptanceError) as caught:
            module.validate_daily_acceptance(path)
        self.assertEqual("INVALID_DAY_SEQUENCE", caught.exception.code)

        report["observation"]["observation_day_count"] = 1
        report["integrity"] = module._report_integrity(report)
        write_json(path, report, mode=0o644)
        with self.assertRaises(module.AcceptanceError) as caught:
            module.validate_daily_acceptance(path)
        self.assertEqual("INVALID_PERMISSIONS", caught.exception.code)

    def test_validator_rejects_forbidden_fields_in_acceptance(self) -> None:
        module = self.require_module()
        built = self.build()
        path = Path(built["path"])
        report = json.loads(path.read_text(encoding="utf-8"))
        report["raw"] = {"rows": [{"token": "SECRET"}]}
        write_json(path, report)
        with self.assertRaises(module.AcceptanceError) as caught:
            module.validate_daily_acceptance(path)
        self.assertEqual("FORBIDDEN_FIELD", caught.exception.code)

    def test_validator_checks_git_head_object_and_directory_mode(self) -> None:
        module = self.require_module()
        built = self.build()
        path = Path(built["path"])
        report = json.loads(path.read_text(encoding="utf-8"))
        report["canonical_checkout"]["head"] = "0" * 40
        write_json(path, report)
        with self.assertRaises(module.AcceptanceError) as caught:
            module.validate_daily_acceptance(path)
        self.assertEqual("INVALID_HEAD_BINDING", caught.exception.code)

        report["canonical_checkout"]["head"] = self.git_head()
        write_json(path, report)
        os.chmod(self.state, 0o755)
        with self.assertRaises(module.AcceptanceError) as caught:
            module.validate_daily_acceptance(path)
        self.assertEqual("INVALID_PERMISSIONS", caught.exception.code)

    def test_validator_recomputes_every_v11_core_projection(self) -> None:
        module = self.require_module()
        built = self.build()
        path = Path(built["path"])
        original = json.loads(path.read_text(encoding="utf-8"))
        mutations = (
            ("top_level", lambda value: value.update(unexpected_metadata=True)),
            ("doctor", lambda value: value["doctor"]["summary"].update(ready=99)),
            ("provider_acceptance", lambda value: value["provider_acceptance"]["iwencai_openapi"].update(
                http_401_count=99
            )),
            ("latency", lambda value: value["latency"].update(p50=99)),
            ("downstream", lambda value: value["downstream_checks"]["market_watch"].update(
                quality_status="normal"
            )),
            ("weekday", lambda value: value["observation"].update(weekday="Wednesday")),
            ("calendar", lambda value: value["observation"]["official_calendar"].update(
                url="https://example.com/calendar"
            )),
            ("tree", lambda value: value["canonical_checkout"].update(ym_stock_data_tree="0" * 40)),
            ("launcher", lambda value: value["canonical_checkout"]["launcher"].update(sha256="0" * 64)),
            ("ignored_artifact", lambda value: value["canonical_checkout"]["ignored_artifacts"][0].update(
                sha256="0" * 64
            )),
        )
        for field, mutate in mutations:
            with self.subTest(field=field):
                report = json.loads(json.dumps(original))
                mutate(report)
                write_json(path, report)
                with self.assertRaises(module.AcceptanceError):
                    module.validate_daily_acceptance(path)
        write_json(path, original)
        self.assertEqual("valid", module.validate_daily_acceptance(path)["status"])

    def test_real_day1_is_validated_without_rewrite(self) -> None:
        module = self.require_module()
        path = Path("/Users/yimu/.ym-stock-data/acceptance/2026-07-29.json")
        if not path.exists():
            self.skipTest("existing Day1 receipt is not present on this host")
        before = path.read_bytes()
        before_mode = stat.S_IMODE(path.stat().st_mode)
        result = module.validate_daily_acceptance(path)
        self.assertEqual("1.0", result["schema_version"])
        self.assertEqual("2026-07-29", result["date"])
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(before_mode, stat.S_IMODE(path.stat().st_mode))

    def test_previous_v11_receipt_remains_read_only_compatible(self) -> None:
        module = self.require_module()
        built = self.build()
        path = Path(built["path"])
        report = json.loads(path.read_text(encoding="utf-8"))
        legacy_smoke = self.smoke_report("2026-07-30", current=False)
        write_json(self.smoke_path, legacy_smoke)

        report["schema_version"] = "1.1"
        report["observation"]["day_count"] = report["observation"].pop(
            "pass_day_count"
        )
        for key in (
            "observation_day_count", "previous_trading_date", "gate_status",
            "epoch_start_date", "epoch_status",
        ):
            report["observation"].pop(key)
        report["smoke_evidence"] = module._project_smoke(
            self.smoke_path,
            "2026-07-30",
            current=False,
        )
        report["provider_acceptance"] = module._provider_acceptance(
            report["doctor"],
            report["smoke_evidence"],
            report["downstream_checks"],
            include_pytdx_screener=False,
        )
        report["latency"] = module._latency(report["smoke_evidence"]["cases"])
        report["integrity"] = module._report_integrity(report)
        write_json(path, report)

        result = module.validate_daily_acceptance(path)
        self.assertEqual("valid", result["status"])
        self.assertEqual("1.1", result["schema_version"])

    def test_template_reuses_exact_builder_keys_and_fails_closed(self) -> None:
        module = self.require_module()
        self.assertTrue(
            hasattr(module, "acceptance_template"),
            "acceptance_template must be the single offline template owner",
        )
        template = module.acceptance_template("2026-07-30")
        calendar = template["calendar"]
        downstream = template["downstream"]
        self.assertEqual("2", template["template_meta"]["smoke_schema_version"])
        self.assertEqual(
            "five-source-capabilities-v1",
            template["template_meta"]["smoke_baseline"],
        )
        self.assertEqual(
            list(CURRENT_SMOKE_CASE_IDS),
            template["template_meta"]["smoke_case_ids"],
        )

        self.assertEqual(set(self.calendar_report("2026-07-30")), set(calendar))
        expected_downstream = self.downstream_report()
        self.assertEqual(set(expected_downstream), set(downstream))
        for key in ("breaker_verification", "market_watch", "live_dashboard", "safety"):
            self.assertEqual(set(expected_downstream[key]), set(downstream[key]))
        self.assertEqual("Thursday", calendar["weekday"])
        self.assertFalse(calendar["confirmed"])
        self.assertFalse(calendar["is_trading_day"])
        self.assertEqual("pending", downstream["safety"]["zero_secret_scan"])
        self.assertEqual("pending", template["template_meta"]["pending_sentinel"])
        self.assertEqual(
            sorted(module._CASE_STATUSES),
            template["template_meta"]["allowed_result_statuses"],
        )
        self.assertEqual(
            sorted(module._ATTEMPT_STATUSES),
            template["template_meta"]["allowed_attempt_statuses"],
        )
        with self.assertRaises(module.AcceptanceError):
            module._project_calendar(calendar, "2026-07-30")
        with self.assertRaises(module.AcceptanceError):
            module._project_downstream(downstream)

    def test_template_and_calendar_validator_share_sse_constants(self) -> None:
        module = self.require_module()
        with patch.object(module, "SSE_CALENDAR_BASE_URL", "https://calendar.example.invalid/"), patch.object(
            module, "SSE_EXCHANGE", "Example Exchange"
        ):
            calendar = module.acceptance_template("2026-07-30")["calendar"]
            calendar["is_trading_day"] = True
            calendar["confirmed"] = True
            calendar["previous_trading_date"] = "2026-07-29"
            calendar["official_calendar"]["basis"] = "verified fixture"
            try:
                projected = module._project_calendar(calendar, "2026-07-30")
            except module.AcceptanceError:
                self.fail("calendar validator must reuse the template SSE constants")
        self.assertEqual("Example Exchange", projected["official_calendar"]["exchange"])
        self.assertEqual(
            "https://calendar.example.invalid/",
            projected["official_calendar"]["url"],
        )

    def test_template_provider_attempt_lists_are_independent(self) -> None:
        module = self.require_module()
        downstream = module.acceptance_template("2026-07-30")["downstream"]
        breaker_attempts = downstream["breaker_verification"]["attempts"]
        market_attempts = downstream["market_watch"]["attempts"]
        dashboard_attempts = downstream["live_dashboard"]["attempts"]

        self.assertIsNot(breaker_attempts, market_attempts)
        self.assertIsNot(breaker_attempts, dashboard_attempts)
        self.assertIsNot(market_attempts, dashboard_attempts)
        breaker_attempts.append({"provider": "fixture"})
        self.assertEqual([], market_attempts)
        self.assertEqual([], dashboard_attempts)

    def test_v11_downstream_requires_reviewed_mode_comparison_pairs(self) -> None:
        module = self.require_module()
        legacy = self.downstream_report()
        self.assertEqual(
            "exact_code_set_match",
            module._project_downstream(legacy)["live_dashboard"]["comparison_status"],
        )

        unified = json.loads(json.dumps(legacy))
        unified["live_dashboard"]["default_api_mode"] = "unified"
        unified["live_dashboard"]["comparison_status"] = "unified_default_observed"
        self.assertEqual(
            "unified_default_observed",
            module._project_downstream(unified)["live_dashboard"]["comparison_status"],
        )

        invalid_pairs = (
            ("legacy", "not_comparable", "unified"),
            ("legacy", "unified_default_observed", "unified"),
            ("unified", "exact_code_set_match", "unified"),
            ("future", "unified_default_observed", "unified"),
            ("legacy", "exact_code_set_match", "legacy"),
        )
        for default_mode, comparison, tested_mode in invalid_pairs:
            report = json.loads(json.dumps(legacy))
            report["live_dashboard"].update(
                default_api_mode=default_mode,
                comparison_status=comparison,
                api_mode_tested=tested_mode,
            )
            with self.subTest(
                default_mode=default_mode,
                comparison=comparison,
                tested_mode=tested_mode,
            ), self.assertRaises(module.AcceptanceError):
                module._project_downstream(report)

    def test_template_cli_is_stdout_only_offline_and_sanitized(self) -> None:
        module = self.require_module()
        output = io.StringIO()
        stderr = io.StringIO()
        try:
            with patch("ym_stock_data.__main__.run_live_smoke") as smoke, patch(
                "ym_stock_data.__main__.collect_diagnostics"
            ) as doctor, patch(
                "ym_stock_data.__main__.canonical_query"
            ) as provider, redirect_stdout(output), redirect_stderr(stderr):
                exit_code = main(["acceptance", "template", "--date", "2026-07-30"])
        except SystemExit:
            self.fail("acceptance template CLI must be registered")
        self.assertEqual(0, exit_code)
        smoke.assert_not_called()
        doctor.assert_not_called()
        provider.assert_not_called()
        self.assertEqual("", stderr.getvalue())
        value = json.loads(output.getvalue())
        self.assertEqual({"calendar", "downstream", "template_meta"}, set(value))
        self.assertFalse((self.root / "2026-07-30.json").exists())

        output = io.StringIO()
        with patch(
            "ym_stock_data.__main__.acceptance_template",
            side_effect=RuntimeError("Bearer SECRET"),
            create=True,
        ), redirect_stdout(output):
            exit_code = main(["acceptance", "template", "--date", "2026-07-30"])
        self.assertEqual(1, exit_code)
        self.assertEqual(
            {"status": "unavailable", "error_code": "ACCEPTANCE_FAILED"},
            json.loads(output.getvalue()),
        )
        self.assertNotIn("SECRET", output.getvalue())

    def test_cli_build_and_validate_are_sanitized_and_never_call_live_functions(self) -> None:
        module = self.require_module()
        output = io.StringIO()
        stderr = io.StringIO()
        arguments = [
            "acceptance",
            "build",
            "--date",
            "2026-07-30",
            "--doctor",
            str(self.doctor_path),
            "--smoke",
            str(self.smoke_path),
            "--downstream",
            str(self.downstream_path),
            "--calendar",
            str(self.calendar_path),
            "--output-dir",
            str(self.state),
            "--repo-root",
            str(self.repo),
        ]
        try:
            def offline_build(**kwargs):
                return module.build_daily_acceptance(
                    **kwargs,
                    now_fn=lambda: datetime(
                        2026, 7, 30, 16, 20, tzinfo=self.shanghai
                    ),
                )

            with patch("ym_stock_data.__main__.run_live_smoke") as smoke, patch(
                "ym_stock_data.__main__.collect_diagnostics"
            ) as doctor, patch(
                "ym_stock_data.__main__.build_daily_acceptance",
                side_effect=offline_build,
            ), redirect_stdout(output), redirect_stderr(stderr):
                exit_code = main(arguments)
        except SystemExit:
            self.fail("acceptance CLI must be registered")
        self.assertEqual(0, exit_code)
        smoke.assert_not_called()
        doctor.assert_not_called()
        built = json.loads(output.getvalue())
        self.assertEqual("complete", built["status"])

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["acceptance", "validate", built["path"]])
        self.assertEqual(0, exit_code)
        self.assertEqual("valid", json.loads(output.getvalue())["status"])

        output = io.StringIO()
        with patch(
            "ym_stock_data.__main__.build_daily_acceptance",
            side_effect=module.AcceptanceError("FORBIDDEN_INPUT"),
        ), redirect_stdout(output):
            exit_code = main(arguments)
        self.assertEqual(2, exit_code)
        self.assertEqual(
            {"status": "unavailable", "error_code": "FORBIDDEN_INPUT"},
            json.loads(output.getvalue()),
        )
        self.assertNotIn("SECRET", output.getvalue())

        output = io.StringIO()
        with patch(
            "ym_stock_data.__main__.build_daily_acceptance",
            side_effect=RuntimeError("Bearer SECRET"),
        ), redirect_stdout(output):
            exit_code = main(arguments)
        self.assertEqual(1, exit_code)
        self.assertEqual(
            {"status": "unavailable", "error_code": "ACCEPTANCE_FAILED"},
            json.loads(output.getvalue()),
        )
        self.assertNotIn("SECRET", output.getvalue())

    def test_module_has_no_provider_or_network_owners(self) -> None:
        module = self.require_module()
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "collect_diagnostics",
            "run_live_smoke",
            "canonical_query",
            "urllib",
            "requests",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
