"""Read-only provider for the dated, independently collected market fact store."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from ..contracts import TZ_SHANGHAI
from ..market_facts import DEFAULT_DB, MarketFactStore
from ..trading_calendar import TradeCalendarUnavailable, latest_completed_trade_date
from .base import ProviderOutcome


class MarketFactsProvider:
    name = "market_facts"

    def __init__(self):
        self.path = Path(os.environ.get("YM_MARKET_FACTS_DB") or DEFAULT_DB)

    def probe(self) -> dict:
        return {"provider": self.name, "status": "configured_unverified" if self.path.is_file() else "unavailable"}

    def call(self, intent: str, params: dict) -> ProviderOutcome:
        if intent != "market_facts":
            return ProviderOutcome(self.name, "incompatible", error_code="UNSUPPORTED_INTENT")
        try:
            day = params.get("trade_date") or latest_completed_trade_date(datetime.now(TZ_SHANGHAI))
            report = MarketFactStore(self.path, read_only=True).report(day)
        except FileNotFoundError:
            return ProviderOutcome(self.name, "provider_error", error_code="FACT_STORE_MISSING")
        except TradeCalendarUnavailable:
            return ProviderOutcome(self.name, "provider_error", error_code="CALENDAR_UNAVAILABLE")
        except (sqlite3.DatabaseError, ValueError):
            return ProviderOutcome(self.name, "provider_error", error_code="FACT_STORE_INVALID")
        if report.get("counts") is None:
            return ProviderOutcome(self.name, "provider_error", error_code="FACT_DAY_MISSING")
        evidence = report.get("limit_evidence") or {}
        current = evidence.get("current") or {}
        returns = report.get("return_evidence") or {}
        stamps = [current.get("fetched_at")]
        stamps.extend(item.get("fetched_at") for item in returns.values() if isinstance(item, dict))
        stamps = [stamp for stamp in stamps if isinstance(stamp, str)]
        return ProviderOutcome(self.name, "success", data=report, fetched_at=max(stamps) if stamps else None)
