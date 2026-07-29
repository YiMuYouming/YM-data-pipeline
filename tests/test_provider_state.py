import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ym_stock_data.provider_state import ProviderState


class ProviderStateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "providers.sqlite3"

    def test_breaker_is_shared_by_independent_instances(self):
        first = ProviderState(self.path)
        second = ProviderState(self.path)

        first.record_failure(
            provider="iwencai_openapi",
            failure_type="auth_error",
            error_code="HTTP_401",
            breaker_seconds=300,
        )

        self.assertEqual(
            "HTTP_401",
            second.active_breaker("iwencai_openapi")["error_code"],
        )

    def test_breaker_is_visible_across_processes(self):
        script = """
import sys
from ym_stock_data.provider_state import ProviderState

ProviderState(sys.argv[1]).record_failure(
    provider="iwencai_openapi",
    failure_type="auth_error",
    error_code="HTTP_401",
    breaker_seconds=300,
)
"""
        subprocess.run(
            [sys.executable, "-c", script, os.fspath(self.path)],
            check=True,
            capture_output=True,
            text=True,
        )

        state = ProviderState(self.path)
        self.assertEqual(
            "HTTP_401",
            state.active_breaker("iwencai_openapi")["error_code"],
        )

    def test_expired_breaker_is_cleared(self):
        state = ProviderState(self.path)
        with patch("ym_stock_data.provider_state.time.time", return_value=1_000.0):
            state.record_failure(
                provider="iwencai_openapi",
                failure_type="auth_error",
                error_code="HTTP_401",
                breaker_seconds=300,
            )
        with patch("ym_stock_data.provider_state.time.time", return_value=1_301.0):
            self.assertIsNone(state.active_breaker("iwencai_openapi"))
            self.assertNotIn("iwencai_openapi", state.snapshot())

    def test_schema_cannot_store_tokens_queries_or_error_bodies(self):
        state = ProviderState(self.path)
        state.record_failure(
            provider="iwencai_openapi",
            failure_type="auth_error",
            error_code="HTTP_401",
            breaker_seconds=300,
        )

        with closing(sqlite3.connect(self.path)) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(provider_state)")
            }
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

        self.assertEqual(
            {
                "provider",
                "status",
                "failure_type",
                "error_code",
                "opened_at",
                "expires_at",
                "updated_at",
            },
            columns,
        )
        self.assertEqual("wal", journal_mode.lower())
        with self.assertRaises(ValueError):
            state.record_failure(
                provider="iwencai_openapi",
                failure_type="auth_error",
                error_code="HTTP_401 bearer secret-token",
                breaker_seconds=300,
            )
        self.assertNotIn(b"secret-token", self.path.read_bytes())


if __name__ == "__main__":
    unittest.main()
