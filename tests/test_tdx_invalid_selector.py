from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import ym_stock_data.api as api
import ym_stock_data.providers.tdx_auth as tdx_auth
from ym_stock_data.__main__ import create_tdx_auth, main
from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.doctor import collect_diagnostics
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.providers.tdx_auth import TdxAuthExpired, TdxOwnedAuth
from ym_stock_data.providers.tdx_mcp import TdxMcpProvider
from ym_stock_data.smoke import run_live_smoke


class FailedProvider:
    def __init__(self, name: str):
        self.name = name

    def call(self, _intent: str, _params: dict) -> ProviderOutcome:
        return ProviderOutcome(
            provider=self.name,
            status="dependency_missing",
            error_code="TEST_UNAVAILABLE",
        )


def successful_result(intent: str) -> dict:
    return {
        "data": {"items": []},
        "_meta": {
            "intent": intent,
            "status": "empty",
            "provider_used": "test_provider",
            "attempts": [
                {
                    "provider": "test_provider",
                    "status": "empty",
                    "error_code": None,
                    "latency_ms": 0,
                }
            ],
            "quality": {"returned_count": 0},
        },
    }


class InvalidSelectorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.selector_path = self.root / "auth" / "tdx-store.json"
        self._write_invalid_selector()

    def _write_invalid_selector(self) -> None:
        self.selector_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.selector_path.parent, 0o700)
        self.selector_path.write_text(
            json.dumps({"schema_version": "1", "mode": "invalid"}),
            encoding="utf-8",
        )
        os.chmod(self.selector_path, 0o600)

    def selector_patch(self):
        return patch.object(tdx_auth, "DEFAULT_SELECTOR_PATH", self.selector_path)

    def test_default_login_fails_before_callback_discovery_or_browser(self):
        with self.selector_patch():
            auth = create_tdx_auth()
        auth.callback_factory = Mock(side_effect=AssertionError("callback must not run"))
        auth.request_json = Mock(side_effect=AssertionError("DCR must not run"))
        auth.browser_open = Mock(side_effect=AssertionError("browser must not run"))

        with self.assertRaises(TdxAuthExpired):
            auth.login()

        auth.callback_factory.assert_not_called()
        auth.request_json.assert_not_called()
        auth.browser_open.assert_not_called()

    def test_status_reports_expired_for_invalid_selector(self):
        output = io.StringIO()
        with self.selector_patch(), redirect_stdout(output):
            exit_code = main(["auth", "status-tdx"])

        self.assertEqual(2, exit_code)
        self.assertEqual(
            {"scope": "mcp.read", "status": "auth_expired", "store": "selected"},
            json.loads(output.getvalue()),
        )

    def test_canonical_query_reports_expired_auth_for_invalid_selector(self):
        def provider_loader(name: str):
            if name == "tdx_quotes":
                return TdxMcpProvider(name)
            return FailedProvider(name)

        state = ProviderState(self.root / "state.sqlite3")
        with (
            self.selector_patch(),
            patch.object(api, "_STATE", state),
            patch.object(api, "_provider_for", side_effect=provider_loader),
        ):
            result = api.query("stock_snapshot", codes=["600519"])

        meta = result["_meta"]
        self.assertEqual("error", meta["status"])
        self.assertEqual("auth_error", meta["attempts"][-1]["status"])
        self.assertEqual("AUTH_EXPIRED", meta["attempts"][-1]["error_code"])
        self.assertEqual({"required": True, "status": "expired"}, meta["auth"])

    def test_doctor_reports_expired_for_invalid_selector(self):
        with self.selector_patch():
            report = collect_diagnostics(
                provider_names=("tdx_mcp",),
                provider_loader=api._provider_for,
            )

        self.assertEqual("auth_expired", report["providers"]["tdx_mcp"]["status"])
        self.assertEqual(
            {"required": True, "status": "expired"},
            report["providers"]["tdx_mcp"]["auth"],
        )

    def test_smoke_reports_the_same_expired_state_without_tdx_network(self):
        tdx_network = Mock(side_effect=AssertionError("TDX network must not run"))

        def diagnostics():
            return collect_diagnostics(
                provider_names=("tdx_mcp",),
                provider_loader=api._provider_for,
            )

        with self.selector_patch():
            receipt = run_live_smoke(
                output_dir=self.root / "smoke",
                query_fn=lambda intent, **_params: successful_result(intent),
                diagnostics_fn=diagnostics,
                provider_loader=tdx_network,
                now_fn=lambda: datetime(
                    2026, 7, 30, 10, 0, 0, tzinfo=TZ_SHANGHAI
                ),
                case_timeout_sec=1,
                total_timeout_sec=20,
            )

        report = json.loads(Path(receipt["receipt"]).read_text(encoding="utf-8"))
        tdx_case = next(
            item for item in report["cases"] if item["case_id"] == "tdx_probe"
        )
        self.assertEqual("auth_expired", tdx_case["status"])
        tdx_network.assert_not_called()

    def test_explicit_store_login_recovers_and_replaces_invalid_selector(self):
        custom_path = self.root / "owned" / "tdx.json"
        cases = (
            (["--store", "keychain"], "keychain", None),
            (
                ["--store", "file", "--file-path", str(custom_path)],
                "file",
                str(custom_path),
            ),
        )

        for arguments, expected_mode, expected_path in cases:
            with self.subTest(mode=expected_mode):
                self._write_invalid_selector()
                output = io.StringIO()
                with (
                    self.selector_patch(),
                    patch.object(
                        TdxOwnedAuth,
                        "login",
                        return_value="configured_unverified",
                    ),
                    redirect_stdout(output),
                ):
                    exit_code = main(["auth", "login-tdx", *arguments])
                    selected = tdx_auth.CredentialStoreSelector(
                        self.selector_path
                    ).load()

                self.assertEqual(0, exit_code)
                self.assertEqual(expected_mode, selected["mode"])
                self.assertEqual(expected_path, selected.get("file_path"))


if __name__ == "__main__":
    unittest.main()
