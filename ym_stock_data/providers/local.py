"""Adapters over existing local source modules.

This module owns no HTTP implementation.  It only maps a canonical provider
name and intent to the source functions that already implement that transport.
"""

from __future__ import annotations

import re
import socket
from datetime import datetime
from typing import Callable

from ..contracts import TZ_SHANGHAI
from ..sources import filings, news, pytdx, research, stock_events, tencent, ths_industry
from ..sources.limit_state import fetch_limit_state
from .base import ProviderOutcome


LOCAL_PROVIDER_NAMES = frozenset(
    {
        "pytdx",
        "eastmoney",
        "tencent",
        "sina",
        "ths_industry",
        "pytdx_breadth",
        "eastmoney_limit_pool",
        "eastmoney_datacenter",
        "eastmoney_research",
        "cninfo",
        "cls",
    }
)
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _now_iso() -> str:
    return datetime.now(TZ_SHANGHAI).isoformat(timespec="seconds")


def _error_code(value: object, default: str = "PROVIDER_ERROR") -> str:
    candidate = str(value or "")
    return candidate if _SAFE_CODE.fullmatch(candidate) else default


def _actual_source(provider: str, raw: dict) -> str:
    meta = raw.get("_meta", {}) if isinstance(raw.get("_meta"), dict) else {}
    source = meta.get("fallback_to") or meta.get("source") or raw.get("source")
    marker = raw.get("_source")
    if isinstance(marker, str):
        source = marker
    if not isinstance(source, str) or not source or source == "none":
        return provider
    aliases = {
        "eastmoney_fallback": "eastmoney",
        "eastmoney_index_fallback": "eastmoney",
        "tencent_fallback": "tencent",
        "tencent_index_fallback": "tencent",
        "sina_fallback": "sina",
        "cls_telegraph": "cls",
    }
    return aliases.get(source, source.removesuffix("_fallback"))


def _row_count(intent: str, raw: dict) -> int:
    if intent == "review_sentiment" and "_total" in raw:
        try:
            return max(0, int(raw.get("_total") or 0))
        except (TypeError, ValueError):
            return 0
    for key in ("datas", "items", "bars", "reports", "filings"):
        value = raw.get(key)
        if isinstance(value, list):
            return len(value)
    if intent == "market_limit_state":
        return sum(int(raw.get(key, 0) or 0) for key in ("zt_count", "zb_count", "dt_count"))
    if intent == "stock_snapshot":
        return sum(
            1
            for key, value in raw.items()
            if key not in {"_meta", "_source", "error", "error_type"}
            and isinstance(value, dict)
            and not value.get("error")
        )
    if intent == "realtime_market":
        return int(any(key not in {"_meta", "_source"} for key in raw))
    return 0


class LocalProvider:
    def __init__(self, name: str):
        if name not in LOCAL_PROVIDER_NAMES:
            raise ValueError(f"unknown local provider: {name}")
        self.name = name

    def probe(self) -> dict:
        return {
            "provider": self.name,
            "status": "configured_unverified",
            "auth": {"required": False, "status": "not_required"},
        }

    def call(self, intent: str, params: dict) -> ProviderOutcome:
        started = datetime.now().timestamp()
        try:
            raw = self._dispatch(intent, params)
        except (TimeoutError, socket.timeout):
            return self._failure(started, "timeout", "TIMEOUT")
        except ImportError:
            return self._failure(started, "dependency_missing", "DEPENDENCY_MISSING")
        except Exception as error:
            return self._failure(
                started,
                "provider_error",
                _error_code(type(error).__name__),
            )
        if isinstance(raw, ProviderOutcome):
            return raw
        if not isinstance(raw, dict):
            return self._failure(started, "provider_error", "INVALID_RESPONSE")

        meta = raw.get("_meta", {}) if isinstance(raw.get("_meta"), dict) else {}
        if raw.get("error") or meta.get("error"):
            error_type = raw.get("error_type") or meta.get("error_type")
            status = {
                "breaker_open": "breaker_open",
                "timeout": "timeout",
                "auth_error": "auth_error",
            }.get(str(error_type), "provider_error")
            return self._failure(started, status, _error_code(error_type))

        count = _row_count(intent, raw)
        return ProviderOutcome(
            provider=_actual_source(self.name, raw),
            status="success" if count else "empty",
            data=raw,
            fetched_at=meta.get("fetched_at") or _now_iso(),
            latency_ms=max(0, int((datetime.now().timestamp() - started) * 1000)),
            quality={
                "status": "normal" if count else "empty",
                "returned_count": count,
                "reason_codes": [],
            },
            auth={"required": False, "status": "not_required"},
        )

    def _failure(self, started: float, status: str, error_code: str) -> ProviderOutcome:
        return ProviderOutcome(
            provider=self.name,
            status=status,
            error_code=error_code,
            latency_ms=max(0, int((datetime.now().timestamp() - started) * 1000)),
            auth={"required": False, "status": "not_required"},
        )

    def _dispatch(self, intent: str, params: dict) -> dict | ProviderOutcome:
        dispatch: dict[tuple[str, str], Callable[[], dict | ProviderOutcome]] = {
            ("pytdx", "realtime_market"): pytdx.fetch_index,
            ("eastmoney", "realtime_market"): pytdx._fallback_index,
            ("tencent", "realtime_market"): pytdx._fallback_index_tencent,
            ("pytdx", "stock_snapshot"): lambda: pytdx.fetch_quotes(params["codes"]),
            ("tencent", "stock_snapshot"): lambda: tencent.fetch_quotes(params["codes"]),
            ("pytdx", "stock_kline"): lambda: pytdx.fetch_kline(
                params["code"], period=params.get("period", "daily")
            ),
            ("tencent", "stock_kline"): lambda: self._http_kline(
                provider="tencent", params=params
            ),
            ("sina", "stock_kline"): lambda: self._http_kline(
                provider="sina", params=params
            ),
            ("ths_industry", "sector_index"): lambda: ths_industry.fetch_sector_index(
                codes=params.get("codes"), names=params.get("names")
            ),
            ("pytdx_breadth", "review_sentiment"): pytdx.fetch_breadth,
            ("eastmoney_limit_pool", "review_sentiment"): lambda: fetch_limit_state(
                date=params.get("date")
            ),
            ("eastmoney_limit_pool", "market_limit_state"): lambda: fetch_limit_state(
                date=params.get("date")
            ),
            ("eastmoney_datacenter", "stock_event"): lambda: stock_events.fetch_stock_event(
                event=params["event"],
                code=params["code"],
                page_size=params.get("page_size", 30),
            ),
            ("eastmoney_research", "research"): lambda: research.fetch_reports(
                code=params["code"],
                days=params.get("days", 90),
                max_pages=params.get("max_pages", 15),
            ),
            ("cninfo", "filings"): lambda: filings.fetch_filings(
                code=params["code"],
                days=params.get("days", 90),
                max_pages=params.get("max_pages", 3),
            ),
            ("cls", "news"): lambda: news.fetch_news(limit=params.get("limit", 20)),
        }
        callback = dispatch.get((self.name, intent))
        if callback is None:
            return ProviderOutcome(
                provider=self.name,
                status="dependency_missing"
                if self.name == "sina" and intent == "stock_snapshot"
                else "incompatible",
                error_code="PROVIDER_ADAPTER_MISSING"
                if self.name == "sina" and intent == "stock_snapshot"
                else "INCOMPATIBLE_INTENT",
            )
        return callback()

    @staticmethod
    def _http_kline(*, provider: str, params: dict) -> dict:
        period = params.get("period", "daily")
        count = params.get("count") or (30 if period in {"daily", "60m"} else 48)
        if provider == "tencent":
            bars = pytdx._fetch_tencent_kline(params["code"], period=period, count=count)
        else:
            bars = pytdx._fetch_sina_kline(params["code"], period=period, count=count)
        return pytdx._build_kline_result(
            params["code"],
            bars,
            source=provider,
        )
