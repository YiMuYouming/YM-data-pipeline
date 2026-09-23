import io
import json
import secrets
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from ym_stock_data.__main__ import main
from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.doctor import collect_diagnostics
from ym_stock_data.providers.tdx_auth import (
    FileCredentialStore,
    TdxAuthExpired,
    TdxOwnedAuth,
)
from ym_stock_data.smoke import run_live_smoke


class TdxSecretBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.secret = "runtime-only-" + secrets.token_hex(24)

    def test_runtime_secret_never_reaches_cli_doctor_receipt_exception_or_diff(self):
        argv = ["auth", "login-tdx"]
        output = io.StringIO()
        errors = io.StringIO()
        auth = Mock()
        auth.login.side_effect = TdxAuthExpired(self.secret)
        with patch(
            "ym_stock_data.__main__.create_tdx_auth", return_value=auth
        ), redirect_stdout(output), redirect_stderr(errors):
            exit_code = main(argv)

        self.assertEqual(2, exit_code)
        self.assertNotIn(self.secret, " ".join(argv))
        self.assertNotIn(self.secret, output.getvalue())
        self.assertNotIn(self.secret, errors.getvalue())

        class BrokenProvider:
            def probe(self):
                raise RuntimeError(self_secret)

        self_secret = self.secret
        doctor = collect_diagnostics(
            provider_names=("tdx_mcp",),
            provider_loader=lambda _name: BrokenProvider(),
        )
        self.assertNotIn(self.secret, json.dumps(doctor))

        receipt = run_live_smoke(
            output_dir=self.root / "smoke",
            query_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError(self.secret)
            ),
            diagnostics_fn=lambda: {
                "providers": {
                    "tdx_mcp": {"status": "auth_missing"},
                    "wind_mcp": {"status": "dependency_missing"},
                }
            },
            now_fn=lambda: datetime(2026, 7, 30, 12, 0, tzinfo=TZ_SHANGHAI),
            case_timeout_sec=1,
            total_timeout_sec=10,
        )
        receipt_text = Path(receipt["receipt"]).read_text(encoding="utf-8")
        self.assertNotIn(self.secret, receipt_text)

        store = FileCredentialStore(self.root / "auth" / "tdx.json")
        store.save(
            {
                "schema_version": "1",
                "client_id": "client-" + secrets.token_hex(8),
                "access_token": "access-" + secrets.token_hex(16),
                "refresh_token": "refresh-" + secrets.token_hex(16),
                "token_type": "Bearer",
                "scope": "mcp.read",
                "expires_at_ms": int(time.time() * 1000) - 1,
                "issuer": "https://auth.example.test",
                "token_endpoint": "https://auth.example.test/token",
            }
        )
        owned = TdxOwnedAuth(
            store=store,
            resource_url="https://mcp.example.test/tdx",
            resource_metadata_url=(
                "https://mcp.example.test/.well-known/oauth-protected-resource"
            ),
            request_json=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError(self.secret)
            ),
            browser_open=lambda _url: None,
            callback_factory=lambda: None,
        )
        with self.assertRaises(TdxAuthExpired) as caught:
            owned.authorization()
        self.assertNotIn(self.secret, str(caught.exception))

        diff = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--", "."],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertNotIn(self.secret, diff.stdout)
        self.assertNotIn(self.secret, diff.stderr)


if __name__ == "__main__":
    unittest.main()
