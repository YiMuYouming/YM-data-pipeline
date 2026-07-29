"""Provider interfaces shared by the unified router."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProviderOutcome:
    provider: str
    status: str
    data: object = None
    error_code: str | None = None
    detail: str | None = None
    fetched_at: str | None = None
    latency_ms: int = 0
    quality: dict | None = None
    auth: dict | None = None
    provenance: dict | None = None


class Provider(Protocol):
    name: str

    def probe(self) -> dict: ...

    def call(self, intent: str, params: dict) -> ProviderOutcome: ...
