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
from ..sources import (
    eastmoney_index,
    eastmoney_stock,
    filings,
    news,
    northbound,
    pytdx,
    research,
    sina_index,
    stock_events,
    tencent,
    ths_hot,
    ths_industry,
)
from ..sources.limit_state import fetch_limit_state
from .base import ProviderOutcome


LOCAL_PROVIDER_NAMES = frozenset(
    {
        "pytdx",
        "eastmoney",
        "tencent",
        "sina",
        "ths_industry",
        "northbound",
        "ths_hot",
        "pytdx_index",
        "sina_index",
        "pytdx_breadth",
        "eastmoney_breadth",
        "eastmoney_limit_pool",
        "eastmoney_index",
        "eastmoney_stock",
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


def _compact_date(value: object) -> str | None:
    text = str(value or "")[:10]
    if re.fullmatch(r"\d{8}", text):
        return text
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text.replace("-", "")
    return None


def _actual_source(provider: str, raw: dict) -> str:
    meta = raw.get("_meta", {}) if isinstance(raw.get("_meta"), dict) else {}
    source = meta.get("fallback_to") or meta.get("source") or raw.get("source")
    marker = raw.get("_source")
    if isinstance(marker, str):
        source = marker
    if not isinstance(source, str) or not source or source == "none":
        return provider
    if provider in {"pytdx_breadth", "eastmoney_breadth"} and source in {
        "eastmoney",
        "eastmoney_fallback",
        "eastmoney_index_fallback",
    }:
        return "eastmoney_breadth"
    aliases = {
        "eastmoney_fallback": "eastmoney",
        "eastmoney_index_fallback": "eastmoney",
        # research.fetch_reports 返回的 source 是 eastmoney_reportapi，
        # 必须映射到注册的 eastmoney_research，否则被判 INCOMPATIBLE_PROVIDER。
        "eastmoney_reportapi": "eastmoney_research",
        "tencent_fallback": "tencent",
        "tencent_index_fallback": "tencent",
        "sina_fallback": "sina",
        "cls_telegraph": "cls",
        "northbound_hsgt": "northbound",
    }
    return aliases.get(source, source.removesuffix("_fallback"))


def _row_sources(provider: str, raw: dict) -> set[str]:
    """Return effective sources for quote rows, including unmarked PyTDX rows."""

    sources: set[str] = set()
    default_source = _actual_source(provider, raw)
    for key, value in raw.items():
        if key in {"_meta", "_source", "error", "error_type"}:
            continue
        if not isinstance(value, dict) or value.get("error"):
            continue
        marker = value.get("_source")
        if isinstance(marker, str) and marker:
            sources.add(_actual_source(provider, {"_source": marker}))
        else:
            sources.add(default_source)
    return sources


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
    if intent in {"market_limit_state", "review_sentiment"} and {
        "zt_count", "zb_count", "dt_count"
    }.issubset(raw):
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


def _project_limit_board(params: dict) -> dict:
    """Project the existing Eastmoney pool aggregate into board semantics."""

    raw = fetch_limit_state(date=params.get("date"))
    if raw.get("error"):
        return raw
    pool_name = {
        "up": "zt",
        "down": "dt",
        "broken": "zb",
        "yesterday": "yzt",
    }[params["kind"]]
    pools = raw.get("pools") if isinstance(raw.get("pools"), dict) else {}
    return {
        "kind": params["kind"],
        "date": raw.get("date"),
        "items": list(pools.get(pool_name) or []),
        "source": "eastmoney_limit_pool",
    }


def _legacy_industry_flow(params: dict) -> dict:
    raw = ths_industry.fetch_industry_summary(top_n=params.get("limit", 20))
    if raw.get("error"):
        return raw
    raw_date = raw.get("trade_date") or raw.get("date")
    requested_date = _compact_date(params.get("trade_date"))
    observed_date = _compact_date(raw_date)
    current_session = params.get("current_session") is True or params.get("use_case") == "realtime_poll"
    if not raw_date:
        if not (
            current_session
        ) or "trade_date" in params:
            return ProviderOutcome(
                provider="ths_industry",
                status="incompatible",
                error_code="DATE_UNVERIFIED",
            )
    elif current_session and observed_date != datetime.now(TZ_SHANGHAI).strftime("%Y%m%d"):
        return ProviderOutcome(
            provider="ths_industry",
            status="incompatible",
            error_code="DATE_MISMATCH",
        )
    elif requested_date and observed_date != requested_date:
        return ProviderOutcome(
            provider="ths_industry",
            status="incompatible",
            error_code="DATE_MISMATCH",
        )
    rows = []
    seen = set()
    for row in [*(raw.get("top") or []), *(raw.get("bottom") or [])]:
        code = row.get("code")
        if code in seen:
            continue
        seen.add(code)
        rows.append(row)
    return {
        "trade_date": raw_date,
        "items": rows,
        "source": "ths_industry",
    }


def _legacy_northbound(params: dict) -> dict:
    raw = northbound.fetch_realtime()
    if raw.get("error"):
        return raw
    raw_date = raw.get("date") or raw.get("trade_date")
    if not raw_date:
        return ProviderOutcome(
            provider="northbound",
            status="incompatible",
            error_code="DATE_UNVERIFIED",
        )
    observed_date = _compact_date(raw_date)
    requested_date = _compact_date(params.get("trade_date"))
    current_session = params.get("current_session") is True or params.get("use_case") == "realtime_poll"
    if current_session and observed_date != datetime.now(TZ_SHANGHAI).strftime("%Y%m%d"):
        return ProviderOutcome(
            provider="northbound",
            status="incompatible",
            error_code="DATE_MISMATCH",
        )
    if requested_date and not current_session and observed_date != requested_date:
        return ProviderOutcome(
            provider="northbound",
            status="incompatible",
            error_code="DATE_MISMATCH",
        )
    return {
        "trade_date": raw_date,
        "items": list(raw.get("minutes") or []),
        "summary": {
            key: raw.get(key)
            for key in ("hgt_current_yi", "sgt_current_yi", "hgt_trend", "sgt_trend")
        },
        "source": "northbound_hsgt",
    }


def _legacy_hot_rank(params: dict) -> dict:
    date_value = params.get("trade_date")
    date_str = (
        f"{date_value[:4]}-{date_value[4:6]}-{date_value[6:]}"
        if isinstance(date_value, str) and len(date_value) == 8
        else None
    )
    raw = ths_hot.fetch_hot_with_zt_count(date_str)
    if raw.get("error"):
        return raw
    raw_date = raw.get("date") or raw.get("trade_date")
    observed_date = _compact_date(raw_date)
    requested_date = _compact_date(params.get("trade_date"))
    current_session = params.get("current_session") is True or params.get("use_case") == "realtime_poll"
    if not raw_date:
        return ProviderOutcome(
            provider="ths_hot",
            status="incompatible",
            error_code="DATE_UNVERIFIED",
        )
    if current_session and observed_date != datetime.now(TZ_SHANGHAI).strftime("%Y%m%d"):
        return ProviderOutcome(
            provider="ths_hot",
            status="incompatible",
            error_code="DATE_MISMATCH",
        )
    if requested_date and not current_session and observed_date != requested_date:
        return ProviderOutcome(
            provider="ths_hot",
            status="incompatible",
            error_code="DATE_MISMATCH",
        )
    rows = list(raw.get("stocks") or [])
    if params.get("limit") is not None:
        rows = rows[: params["limit"]]
    return {
        "trade_date": raw_date,
        "items": rows,
        "reason_stats": raw.get("reason_stats", {}),
        "zt_count": raw.get("zt_count"),
        "source": "ths_hot",
    }


def _legacy_index_kline(params: dict, *, provider: str = "pytdx_index") -> dict | ProviderOutcome:
    codes = params.get("codes") or [params.get("index_code")]
    if provider == "pytdx_index" and params.get("period", "daily") not in {
        "daily",
        "weekly",
        "monthly",
    }:
        return ProviderOutcome(
            provider=provider,
            status="incompatible",
            error_code="INDEX_MINUTE_VOLUME_UNVERIFIED",
        )
    fetcher = {
        "eastmoney_index": eastmoney_index.fetch_index_kline,
        "sina_index": sina_index.fetch_index_kline,
        "pytdx_index": pytdx.fetch_index_kline,
    }[provider]
    items = []
    last_error = None
    for code in codes:
        result = fetcher(
            code,
            period=params.get("period", "daily"),
            count=params.get("count"),
            start_date=params.get("start_date"),
            end_date=params.get("end_date"),
        )
        if isinstance(result, dict) and not result.get("error"):
            items.append(result)
        elif isinstance(result, dict):
            last_error = result
    if len(codes) == 1:
        return items[0] if items else (last_error or {"error": "index kline unavailable", "error_type": "NO_DATA"})
    by_code = {
        item["index_code"]: {"rows": item.get("bars", [])}
        for item in items
    }
    for item in items:
        by_code[item["index_code"].split(".")[0]] = by_code[item["index_code"]]
    return {
        "items": items,
        "by_code": by_code,
        **{key.split(".")[0]: value for key, value in by_code.items() if "." in key},
        "source": provider,
    }


def _legacy_index_intraday_compare(
    params: dict, *, provider: str = "pytdx_index"
) -> dict | ProviderOutcome:
    if provider == "pytdx_index":
        return ProviderOutcome(
            provider=provider,
            status="incompatible",
            error_code="INDEX_MINUTE_VOLUME_UNVERIFIED",
        )
    fetcher = {
        "eastmoney_index": eastmoney_index.fetch_index_intraday_compare,
        "sina_index": sina_index.fetch_index_intraday_compare,
    }.get(provider)
    if fetcher is None:
        return ProviderOutcome(
            provider=provider,
            status="incompatible",
            error_code="INCOMPATIBLE_INTENT",
        )
    raw = fetcher(
        period=params.get("period", "15m"),
        trade_date=params.get("trade_date"),
    )
    if not isinstance(raw, dict) or raw.get("error"):
        return raw
    return raw


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
        nested_data = raw.get("data")
        if isinstance(nested_data, dict):
            flattened = dict(nested_data)
            flattened.update({key: value for key, value in raw.items() if key != "data"})
            raw = flattened

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
        actual_source = _actual_source(self.name, raw)
        if intent == "stock_snapshot":
            row_sources = _row_sources(self.name, raw)
            if len(row_sources) > 1:
                return self._failure(started, "provider_error", "MIXED_PROVENANCE")
            if row_sources:
                actual_source = next(iter(row_sources))
        provenance = None
        if actual_source != self.name:
            provenance = {
                "verified": True,
                "fallback_from": self.name,
                "kind": "source_internal",
            }
        return ProviderOutcome(
            provider=actual_source,
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
            provenance=provenance,
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
            ("pytdx", "stock_kline"): lambda: self._pytdx_kline(params),
            ("tencent", "stock_kline"): lambda: self._http_kline(
                provider="tencent", params=params
            ),
            ("sina", "stock_kline"): lambda: self._http_kline(
                provider="sina", params=params
            ),
            ("eastmoney_stock", "stock_kline"): lambda: eastmoney_stock.fetch_kline(
                params["code"],
                period=params.get("period", "daily"),
                count=params.get("count"),
                start_date=params.get("start_date"),
                end_date=params.get("end_date"),
                adjustment=params.get("adjustment", "none"),
            ),
            ("ths_industry", "sector_index"): lambda: ths_industry.fetch_sector_index(
                codes=params.get("codes"), names=params.get("names")
            ),
            ("ths_industry", "industry_flow"): lambda: _legacy_industry_flow(params),
            ("northbound", "northbound_flow"): lambda: _legacy_northbound(params),
            ("ths_hot", "legacy_hot_rank"): lambda: _legacy_hot_rank(params),
            ("pytdx_index", "index_kline"): lambda: _legacy_index_kline(params),
            ("eastmoney_index", "index_kline"): lambda: _legacy_index_kline(
                params, provider="eastmoney_index"
            ),
            ("sina_index", "index_kline"): lambda: _legacy_index_kline(
                params, provider="sina_index"
            ),
            ("pytdx_index", "index_intraday_compare"): lambda: _legacy_index_intraday_compare(
                params
            ),
            ("eastmoney_index", "index_intraday_compare"): lambda: _legacy_index_intraday_compare(
                params, provider="eastmoney_index"
            ),
            ("sina_index", "index_intraday_compare"): lambda: _legacy_index_intraday_compare(
                params, provider="sina_index"
            ),
            ("pytdx_breadth", "review_sentiment"): pytdx.fetch_breadth,
            ("eastmoney_breadth", "review_sentiment"): pytdx._fallback_breadth,
            ("eastmoney_limit_pool", "review_sentiment"): lambda: fetch_limit_state(
                date=params.get("date")
            ),
            ("eastmoney_limit_pool", "market_limit_state"): lambda: fetch_limit_state(
                date=params.get("date")
            ),
            ("eastmoney_limit_pool", "market_limit_board"): lambda: _project_limit_board(
                params
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
    def _pytdx_kline(params: dict) -> dict:
        if params.get("adjustment", "none") != "none":
            return ProviderOutcome(
                provider="pytdx",
                status="incompatible",
                error_code="ADJUSTMENT_UNSUPPORTED",
            )
        fetch_params = {"period": params.get("period", "daily")}
        for key in ("count", "start_date", "end_date"):
            if params.get(key) is not None:
                fetch_params[key] = params[key]
        raw = pytdx.fetch_kline(params["code"], **fetch_params)
        if not isinstance(raw, dict):
            return raw
        nested_data = raw.get("data")
        if isinstance(nested_data, dict):
            flattened = dict(nested_data)
            flattened.update({key: value for key, value in raw.items() if key != "data"})
            raw = flattened
        result = dict(raw)
        result.setdefault("period", params.get("period", "daily"))
        period = params.get("period", "daily")
        volume_multiplier = 100 if period in {"daily", "weekly", "monthly"} else 1
        result = LocalProvider._canonicalize_kline(
            result, params, volume_multiplier=volume_multiplier
        )
        if params.get("count") is None:
            return result
        bars = result.get("bars")
        if isinstance(bars, list):
            count = params["count"]
            result["bars"] = list(bars[-count:])
            result["requested_count"] = count
            result["returned_bars"] = len(result["bars"])
        return result

    @staticmethod
    def _http_kline(*, provider: str, params: dict) -> dict:
        period = params.get("period", "daily")
        count = params.get("count") or (30 if period in {"daily", "60m"} else 48)
        adjustment = params.get("adjustment", "none")
        if adjustment != "none" and provider != "tencent":
            return ProviderOutcome(
                provider=provider,
                status="incompatible",
                error_code="ADJUSTMENT_UNSUPPORTED",
            )
        if provider == "tencent":
            bars = pytdx._fetch_tencent_kline(
                params["code"],
                period=period,
                count=count,
                adjustment=adjustment,
            )
        else:
            bars = pytdx._fetch_sina_kline(params["code"], period=period, count=count)
        result = pytdx._build_kline_result(
            params["code"],
            bars,
            source=provider,
        )
        volume_multiplier = (
            100
            if provider == "tencent" and period in {"daily", "weekly", "monthly"}
            else 1
        )
        return LocalProvider._canonicalize_kline(
            result, params, volume_multiplier=volume_multiplier
        )

    @staticmethod
    def _canonicalize_kline(
        raw: dict, params: dict, *, volume_multiplier: float
    ) -> dict:
        if not isinstance(raw, dict) or raw.get("error"):
            return raw
        normalized = dict(raw)
        bars = []
        for row in raw.get("bars", []):
            if not isinstance(row, dict):
                continue
            stamp = row.get("datetime", row.get("time"))
            volume = row.get("volume", row.get("vol"))
            amount = row.get("amount")
            try:
                volume = float(volume) * volume_multiplier if volume is not None else None
            except (TypeError, ValueError):
                volume = None
            try:
                amount = float(amount) if amount is not None else None
            except (TypeError, ValueError):
                amount = None
            bars.append(
                {
                    "datetime": str(stamp or ""),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": volume,
                    "amount": amount,
                }
            )
        normalized["bars"] = bars
        normalized["total_bars"] = len(bars)
        normalized["adjustment"] = params.get("adjustment", "none")
        normalized["volume_unit"] = "share"
        normalized["amount_unit"] = "CNY"
        return normalized
