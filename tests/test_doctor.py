import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import ym_stock_data.api as api
from ym_stock_data.__main__ import main
from ym_stock_data.doctor import (
    ALLOWED_PROVIDER_STATES,
    collect_diagnostics,
    report_tdx_import_unavailable,
    setup_pywencai,
)


class ProbeProvider:
    def __init__(self, name, status):
        self.name = name
        self.status = status

    def probe(self):
        return {"provider": self.name, "status": self.status}


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def test_provider_states_are_independent_and_json_serializable(self):
        wind_config = self.root / "wind-config"
        wind_config.write_text("present", encoding="utf-8")
        tdx_auth = self.root / "missing-tdx.json"

        def provider_loader(name):
            if name == "pywencai":
                return ProbeProvider(name, "dependency_missing")
            return ProbeProvider(name, "configured_unverified")

        report = collect_diagnostics(
            provider_names=("pytdx", "iwencai_openapi", "pywencai"),
            provider_loader=provider_loader,
            tdx_auth_path=tdx_auth,
            wind_config_path=wind_config,
        )

        json.loads(json.dumps(report))
        self.assertEqual("dependency_missing", report["providers"]["pywencai"]["status"])
        self.assertEqual("configured_unverified", report["providers"]["wind_mcp"]["status"])
        self.assertTrue(
            all(item["status"] in ALLOWED_PROVIDER_STATES for item in report["providers"].values())
        )
        self.assertNotIn("pipeline unavailable", json.dumps(report))

    def test_doctor_instantiates_registry_provider_classes_before_probe(self):
        class RegisteredProbeProvider:
            def probe(self):
                return {"provider": "registered_probe", "status": "ready"}

        with patch.dict(
            api.PROVIDER_REGISTRY,
            {"registered_probe": RegisteredProbeProvider},
        ):
            report = collect_diagnostics(
                provider_names=("registered_probe",),
                provider_loader=api._provider_for,
                tdx_auth_path=self.root / "tdx.json",
                wind_config_path=self.root / "wind",
            )

        self.assertEqual("ready", report["providers"]["registered_probe"]["status"])

    def test_doctor_never_prints_exception_text_tokens_or_query_rows(self):
        class BrokenProvider:
            def probe(self):
                raise RuntimeError("secret-token query-row 600519")

        report = collect_diagnostics(
            provider_names=("broken",),
            provider_loader=lambda _name: BrokenProvider(),
            tdx_auth_path=self.root / "tdx.json",
            wind_config_path=self.root / "wind",
        )
        serialized = json.dumps(report)

        self.assertEqual("unavailable", report["providers"]["broken"]["status"])
        for forbidden in ("secret-token", "query-row", "600519"):
            self.assertNotIn(forbidden, serialized)

    def test_setup_pywencai_prints_target_before_safe_subprocess_lists(self):
        events = []
        runner = Mock(side_effect=lambda command, **kwargs: events.append(("run", command, kwargs)))
        target = self.root / "managed-pywencai"

        setup_pywencai(
            target=target,
            uv_executable="/opt/bin/uv",
            runner=runner,
            emit=lambda value: events.append(("print", value)),
        )

        self.assertEqual(("print", str(target)), events[0])
        self.assertEqual(2, runner.call_count)
        self.assertEqual(
            ["/opt/bin/uv", "venv", "--python", "3.12", str(target)],
            events[1][1],
        )
        self.assertEqual(
            [
                "/opt/bin/uv",
                "pip",
                "install",
                "--python",
                str(target / "bin" / "python"),
                "pywencai==0.13.1",
                "pandas",
                "numpy",
            ],
            events[2][1],
        )
        for _, _command, kwargs in events[1:]:
            self.assertEqual(
                {
                    "check": True,
                    "shell": False,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL,
                },
                kwargs,
            )
        self.assertFalse(target.exists())

    def test_cli_setup_failure_is_sanitized_and_never_prints_subprocess_stderr(self):
        failure = subprocess.CalledProcessError(
            17,
            ["uv", "pip"],
            stderr="secret-token credential-row",
        )
        output = io.StringIO()
        with patch(
            "ym_stock_data.__main__.setup_pywencai",
            side_effect=failure,
        ), redirect_stdout(output):
            exit_code = main(["setup", "pywencai"])

        payload = json.loads(output.getvalue())
        self.assertEqual(1, exit_code)
        self.assertEqual({"exit_code": 17, "status": "unavailable"}, payload)
        self.assertNotIn("secret-token", output.getvalue())
        self.assertNotIn("credential-row", output.getvalue())

    def test_tdx_import_reports_target_but_never_scans_or_writes_in_task8(self):
        target = self.root / "auth" / "tdx.json"
        events = []

        result = report_tdx_import_unavailable(
            target=target,
            from_workbuddy=True,
            emit=events.append,
        )

        self.assertEqual(str(target), events[0])
        self.assertEqual("unavailable", result["status"])
        self.assertFalse(target.exists())

    def test_cli_doctor_json_is_parseable(self):
        report = {"schema_version": "1", "providers": {}}
        output = io.StringIO()
        with patch("ym_stock_data.__main__.collect_diagnostics", return_value=report), redirect_stdout(output):
            exit_code = main(["doctor", "--json"])

        self.assertEqual(0, exit_code)
        self.assertEqual(report, json.loads(output.getvalue()))

    def test_cli_query_parses_json_lists_and_scalars(self):
        output = io.StringIO()
        result = {"data": {}, "_meta": {"status": "success"}}
        with patch("ym_stock_data.__main__.canonical_query", return_value=result) as query_call, redirect_stdout(output):
            exit_code = main(
                ["query", "stock_snapshot", 'codes=["600519"]']
            )

        self.assertEqual(0, exit_code)
        query_call.assert_called_once_with("stock_snapshot", codes=["600519"])
        self.assertEqual(result, json.loads(output.getvalue()))


if __name__ == "__main__":
    unittest.main()
