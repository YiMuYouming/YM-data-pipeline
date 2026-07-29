"""Cross-process provider breaker state backed by SQLite."""

from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import PROVIDER_STATE_PATH


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_state (
    provider TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    failure_type TEXT,
    error_code TEXT,
    opened_at REAL,
    expires_at REAL,
    updated_at REAL NOT NULL
)
"""


def _safe_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} must be a short machine-readable code")
    return value


class ProviderState:
    """Persist only sanitized provider health and breaker metadata."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else PROVIDER_STATE_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def active_breaker(self, provider: str) -> dict | None:
        provider = _safe_identifier(provider, "provider")
        now = time.time()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM provider_state WHERE provider = ?",
                (provider,),
            ).fetchone()
            if row is None or row["status"] != "breaker_open":
                return None
            if row["expires_at"] is None or row["expires_at"] <= now:
                connection.execute(
                    "DELETE FROM provider_state WHERE provider = ?",
                    (provider,),
                )
                return None
            return dict(row)

    def record_failure(
        self,
        *,
        provider: str,
        failure_type: str,
        error_code: str,
        breaker_seconds: int,
    ) -> None:
        provider = _safe_identifier(provider, "provider")
        failure_type = _safe_identifier(failure_type, "failure_type")
        error_code = _safe_identifier(error_code, "error_code")
        if (
            not isinstance(breaker_seconds, int)
            or isinstance(breaker_seconds, bool)
            or breaker_seconds <= 0
        ):
            raise ValueError("breaker_seconds must be a positive integer")

        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO provider_state (
                    provider, status, failure_type, error_code,
                    opened_at, expires_at, updated_at
                ) VALUES (?, 'breaker_open', ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    status = excluded.status,
                    failure_type = excluded.failure_type,
                    error_code = excluded.error_code,
                    opened_at = excluded.opened_at,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    provider,
                    failure_type,
                    error_code,
                    now,
                    now + breaker_seconds,
                    now,
                ),
            )

    def record_success(self, provider: str) -> None:
        provider = _safe_identifier(provider, "provider")
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO provider_state (
                    provider, status, failure_type, error_code,
                    opened_at, expires_at, updated_at
                ) VALUES (?, 'ready', NULL, NULL, NULL, NULL, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    status = excluded.status,
                    failure_type = NULL,
                    error_code = NULL,
                    opened_at = NULL,
                    expires_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (provider, now),
            )

    def snapshot(self) -> dict[str, dict]:
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM provider_state
                WHERE status = 'breaker_open'
                  AND (expires_at IS NULL OR expires_at <= ?)
                """,
                (now,),
            )
            rows = connection.execute(
                "SELECT * FROM provider_state ORDER BY provider"
            ).fetchall()
        return {row["provider"]: dict(row) for row in rows}
