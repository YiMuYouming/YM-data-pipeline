"""One canonical public query router."""

from __future__ import annotations

import re
import socket
import math
from datetime import datetime, time as datetime_time, timedelta
from typing import Callable

from .contracts import ProviderAttempt, TZ_SHANGHAI, build_result
from .intent_normalizers import normalize_success
from .provider_state import ProviderState
from .providers.base import ProviderOutcome
from .providers.iwencai import IWenCaiOpenAPIProvider, PyWenCaiProvider
from .providers.local import LOCAL_PROVIDER_NAMES, LocalProvider
from .providers.pytdx_screener import PytdxScreenerProvider
from .providers.stocktoday import StockTodayProvider, validate_dataset, validate_source
from .providers.wind_mcp import (
    WIND_ENRICHMENT_CAPABILITIES,
    WIND_PROVIDER_NAMES,
    WindMcpProvider,
)
from .provider_policy import load_compiled_policy
from .quality import assess_quality
from .routing import EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED, RouteSpec, route_for
from .sources.stock_events import EVENTS as STOCK_EVENTS
from .trading_calendar import (
    TradeCalendarUnavailable,
    is_trading_day,
    latest_completed_trade_date,
    market_fact_age_seconds,
    previous_trading_day,
    session_seconds,
)


_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_CONTINUE_STATUSES = {
    "auth_error",
    "dependency_missing",
    "timeout",
    "network_error",
    "provider_error",
    "breaker_open",
    "incompatible",
}
_AUTH_STATUS_SEVERITY = {
    "not_required": 0,
    "ok": 1,
    "present": 2,
    "unverified": 3,
    "missing": 4,
    "expired": 5,
    "error": 6,
}
TDX_DIAGNOSTIC_NAMES = (
    "tdx_mcp",
    "tdx_screener",
    "tdx_quotes",
    "tdx_kline",
    "tdx_report",
    "tdx_notice",
    "tdx_news",
)
_ALLOWED_PARAMS = {
    "stocktoday_data": frozenset({"api_name", "params", "fields", "max_rows"}),
    "realtime_market": frozenset({"use_case"}),
    "sector_index": frozenset({"codes", "names"}),
    "stock_snapshot": frozenset({"codes", "source", "use_case"}),
    "stock_kline": frozenset(
        {"code", "period", "count", "source", "adjustment", "use_case", "start_date", "end_date"}
    ),
    "review_sentiment": frozenset(
        {
            "query",
            "limit",
            "date",
            "expected_row_shape",
            "expected_count",
            "lang",
            "version",
        }
    ),
    "market_limit_state": frozenset({"date", "limit_type"}),
    "market_limit_board": frozenset({"kind", "date"}),
    "market_hot_rank": frozenset({"source", "trade_date", "limit"}),
    "industry_flow": frozenset({"trade_date", "limit", "use_case"}),
    "fund_flow": frozenset({"trade_date", "limit"}),
    "northbound_flow": frozenset({"trade_date", "limit", "use_case"}),
    "legacy_hot_rank": frozenset({"trade_date", "limit", "use_case"}),
    "index_kline": frozenset(
        {
            "index_code",
            "codes",
            "period",
            "count",
            "start_date",
            "end_date",
            "adjustment",
        }
    ),
    "index_intraday_compare": frozenset({"trade_date", "period", "use_case"}),
    "stock_event": frozenset({"event", "code", "page_size"}),
    "research": frozenset({"code", "days", "max_pages"}),
    "filings": frozenset({"code", "days", "max_pages"}),
    "news": frozenset({"limit"}),
    "wind_enrichment": frozenset({"capability", "code", "codes", "fields", "params"}),
}
_USE_CASES = frozenset({"realtime_poll", "agent", "research", "history"})


class UnavailableProvider:
    """Auditable placeholder for a provider implemented in a later task."""

    def __init__(self, name: str):
        self.name = name

    def probe(self) -> dict:
        return {"provider": self.name, "status": "unavailable"}

    def call(self, intent: str, params: dict) -> ProviderOutcome:
        return ProviderOutcome(
            provider=self.name,
            status="dependency_missing",
            error_code="PROVIDER_NOT_IMPLEMENTED",
        )


def _local_factory(name: str) -> Callable[[], LocalProvider]:
    return lambda: LocalProvider(name)


def _tdx_factory(name: str) -> Callable[[], object]:
    def factory():
        try:
            from .providers.tdx_mcp import TdxMcpProvider
        except ImportError:
            return UnavailableProvider(name)
        return TdxMcpProvider(name)

    return factory


def _wind_factory(name: str) -> Callable[[], WindMcpProvider]:
    return lambda: WindMcpProvider(name)


PROVIDER_REGISTRY: dict[str, object] = {
    **{name: _local_factory(name) for name in LOCAL_PROVIDER_NAMES},
    **{name: _tdx_factory(name) for name in TDX_DIAGNOSTIC_NAMES},
    **{name: _wind_factory(name) for name in WIND_PROVIDER_NAMES},
    "iwencai_openapi": IWenCaiOpenAPIProvider,
    "pywencai": PyWenCaiProvider,
    "pytdx_screener": PytdxScreenerProvider,
    "stocktoday": StockTodayProvider,
}
_STATE: ProviderState | None = None


def _provider_state() -> ProviderState:
    global _STATE
    if _STATE is None:
        _STATE = ProviderState()
    return _STATE


def _provider_for(name: str):
    registered = PROVIDER_REGISTRY.get(name)
    if registered is None:
        return UnavailableProvider(name)
    if isinstance(registered, type):
        return registered()
    if callable(registered) and not hasattr(registered, "call"):
        return registered()
    return registered


def _safe_error_code(value: object, default: str) -> str:
    candidate = str(value or "")
    return candidate if _SAFE_CODE.fullmatch(candidate) else default


def _more_severe_auth(current: dict | None, candidate: dict | None) -> dict | None:
    """Keep the most severe sanitized auth state seen on an exhausted route."""

    if not isinstance(candidate, dict):
        return current
    if not isinstance(current, dict):
        return dict(candidate)
    current_score = _AUTH_STATUS_SEVERITY.get(str(current.get("status")), 3)
    candidate_score = _AUTH_STATUS_SEVERITY.get(str(candidate.get("status")), 3)
    return dict(candidate) if candidate_score > current_score else current


def _latest_completed_trade_date(now: datetime | None = None) -> str:
    """Return the latest completed exchange session, or fail if unconfirmed."""
    current = now or datetime.now(TZ_SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TZ_SHANGHAI)
    return latest_completed_trade_date(current.astimezone(TZ_SHANGHAI))


def _normalize_ymd(value: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value.replace("-", "")
    return value


def _validate_params(intent: str, params: dict) -> None:
    unknown = set(params) - _ALLOWED_PARAMS[intent]
    if unknown:
        raise ValueError(f"unsupported {intent} params: {', '.join(sorted(unknown))}")
    if "use_case" in params and params["use_case"] not in _USE_CASES:
        raise ValueError("unsupported use_case")
    if intent == "stocktoday_data":
        validate_dataset(params)
    if intent in {"stock_snapshot", "stock_kline"} and "source" in params:
        validate_source(intent, params)
    if intent == "stock_snapshot":
        codes = params.get("codes")
        if isinstance(codes, str):
            codes = [codes]
        if not isinstance(codes, (list, tuple)) or not codes:
            raise ValueError("stock_snapshot requires non-empty codes")
        if not all(str(code).strip() for code in codes):
            raise ValueError("stock_snapshot codes cannot contain empty values")
        params["codes"] = [str(code) for code in codes]
    elif intent == "sector_index":
        for key in ("codes", "names"):
            value = params.get(key)
            if isinstance(value, str):
                params[key] = [value]
            elif value is not None and not isinstance(value, (list, tuple)):
                raise ValueError(f"sector_index {key} must be a list")
        if not params.get("codes") and not params.get("names"):
            raise ValueError("sector_index requires codes or names")
        if any(not str(code).startswith("881") for code in params.get("codes") or []):
            raise ValueError("sector_index codes must use the THS 881 prefix")
    elif intent == "stock_kline":
        if not str(params.get("code") or "").strip():
            raise ValueError("stock_kline requires code")
        period = str(params.get("period", "daily"))
        if period not in {"daily", "weekly", "monthly", "60m", "15m", "5m", "1m"}:
            raise ValueError("unsupported stock_kline period")
        params.update({"code": str(params["code"]), "period": period})
        adjustment = str(params.get("adjustment", "none")).lower()
        if adjustment not in {"none", "qfq"}:
            raise ValueError("stock_kline adjustment must be none or qfq")
        if adjustment == "qfq" and period in {"1m", "5m", "15m", "60m"}:
            raise ValueError("qfq is only supported for daily, weekly, or monthly K-lines")
        params["adjustment"] = adjustment
        if params.get("count") is not None:
            count = int(params["count"])
            if count <= 0:
                raise ValueError("stock_kline count must be positive")
            params["count"] = count
        for key in ("start_date", "end_date"):
            value = params.get(key)
            if value is not None and (
                not isinstance(value, str)
                or not re.fullmatch(r"(?:\d{8}|\d{4}-\d{2}-\d{2})", value)
            ):
                raise ValueError(f"stock_kline {key} must use YYYYMMDD")
            if value is not None:
                params[key] = _normalize_ymd(value)
        if params.get("start_date") and params.get("end_date"):
            if params["start_date"] > params["end_date"]:
                raise ValueError("stock_kline start_date must not exceed end_date")
    elif intent == "review_sentiment":
        value = params.get("query")
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("review_sentiment query must be a non-empty string")
        limit = int(params.get("limit", 50))
        if limit <= 0:
            raise ValueError("review_sentiment limit must be positive")
        params["limit"] = limit
        shape = params.get("expected_row_shape")
        if shape not in {None, "stock_rows", "sector_rows"}:
            raise ValueError("unsupported review_sentiment expected_row_shape")
        if params.get("expected_count") is not None:
            expected = int(params["expected_count"])
            if expected <= 0:
                raise ValueError("review_sentiment expected_count must be positive")
            params["expected_count"] = expected
        lang = params.get("lang")
        if lang is not None and lang not in {"English", "中文"}:
            raise ValueError("review_sentiment lang must be English or 中文")
        version = params.get("version")
        if version is not None and (
            not isinstance(version, str) or not version.strip()
        ):
            raise ValueError("review_sentiment version must be a non-empty string")
        if isinstance(version, str):
            params["version"] = version.strip()
    elif intent == "stock_event":
        if params.get("event") not in STOCK_EVENTS:
            raise ValueError("stock_event requires a supported event")
        if not str(params.get("code") or "").strip():
            raise ValueError("stock_event requires code")
        page_size = int(params.get("page_size", 30))
        if page_size <= 0:
            raise ValueError("stock_event page_size must be positive")
        params.update({"code": str(params["code"]), "page_size": page_size})
    elif intent in {"research", "filings"}:
        if not str(params.get("code") or "").strip():
            raise ValueError(f"{intent} requires code")
        params["code"] = str(params["code"])
        for key in ("days", "max_pages"):
            if key in params:
                value = int(params[key])
                if value <= 0:
                    raise ValueError(f"{intent} {key} must be positive")
                params[key] = value
    elif intent == "news":
        limit = int(params.get("limit", 20))
        if limit <= 0:
            raise ValueError("news limit must be positive")
        params["limit"] = limit
    elif intent == "market_limit_state":
        date = params.get("date")
        if date is not None and (
            not isinstance(date, str) or not re.fullmatch(r"\d{8}", date)
        ):
            raise ValueError("market_limit_state date must use YYYYMMDD")
        limit_type = params.get("limit_type")
        if limit_type is not None and limit_type not in {"U", "D"}:
            raise ValueError("market_limit_state limit_type must be U or D")
    elif intent == "market_limit_board":
        if params.get("kind") not in {"up", "down", "broken", "yesterday"}:
            raise ValueError(
                "market_limit_board kind must be up, down, broken, or yesterday"
            )
        date = params.get("date")
        if date is not None and (
            not isinstance(date, str) or not re.fullmatch(r"\d{8}", date)
        ):
            raise ValueError("market_limit_board date must use YYYYMMDD")
    elif intent == "market_hot_rank":
        if params.get("source") not in {"ths", "dc"}:
            raise ValueError("market_hot_rank source must be ths or dc")
        trade_date = params.get("trade_date")
        if trade_date is not None and (
            not isinstance(trade_date, str) or not re.fullmatch(r"\d{8}", trade_date)
        ):
            raise ValueError("market_hot_rank trade_date must use YYYYMMDD")
        if trade_date is None:
            params["trade_date"] = _latest_completed_trade_date()
        if params.get("limit") is not None:
            limit = int(params["limit"])
            if limit <= 0:
                raise ValueError("market_hot_rank limit must be positive")
            params["limit"] = limit
    elif intent in {"industry_flow", "fund_flow", "northbound_flow", "legacy_hot_rank"}:
        trade_date = params.get("trade_date")
        if trade_date is not None and (
            not isinstance(trade_date, str) or not re.fullmatch(r"\d{8}", trade_date)
        ):
            raise ValueError(f"{intent} trade_date must use YYYYMMDD")
        if trade_date is None and not (
            intent in {"industry_flow", "northbound_flow", "legacy_hot_rank"}
            and params.get("use_case") == "realtime_poll"
        ):
            params["trade_date"] = _latest_completed_trade_date()
        if params.get("limit") is not None:
            limit = int(params["limit"])
            if limit <= 0:
                raise ValueError(f"{intent} limit must be positive")
            params["limit"] = limit
    elif intent == "index_kline":
        index_code = str(params.get("index_code") or "")
        codes = params.get("codes")
        if isinstance(codes, str):
            codes = [codes]
        if codes is not None and (
            not isinstance(codes, (list, tuple)) or not codes
        ):
            raise ValueError("index_kline codes must be a non-empty list")
        if codes is not None and index_code:
            raise ValueError("index_kline accepts index_code or codes, not both")
        if codes is None and not index_code:
            raise ValueError("index_kline requires index_code or codes")

        def normalize_index_code(value):
            text = str(value).upper()
            if re.fullmatch(r"\d{6}", text):
                text = f"{text}.SH" if text == "000001" else f"{text}.SZ"
            if not re.fullmatch(r"\d{6}\.(?:SH|SZ)", text):
                raise ValueError("index_kline codes must use 6 digits with SH or SZ")
            return text

        if codes is not None:
            params["codes"] = [normalize_index_code(value) for value in codes]
        else:
            index_code = normalize_index_code(index_code)
            params["index_code"] = index_code
        period = str(params.get("period", "daily")).lower()
        if period not in {"daily", "weekly", "monthly", "1m", "5m", "15m", "60m"}:
            raise ValueError("unsupported index_kline period")
        adjustment = str(params.get("adjustment", "none")).lower()
        if adjustment != "none":
            raise ValueError("index_kline supports adjustment=none only")
        params.update({"index_code": index_code.upper(), "period": period, "adjustment": adjustment})
        if params.get("count") is not None:
            count = int(params["count"])
            if count <= 0:
                raise ValueError("index_kline count must be positive")
            params["count"] = count
        for key in ("start_date", "end_date"):
            value = params.get(key)
            if value is not None and (
                not isinstance(value, str)
                or not re.fullmatch(r"(?:\d{8}|\d{4}-\d{2}-\d{2})", value)
            ):
                raise ValueError(f"index_kline {key} must use YYYYMMDD")
            if value is not None:
                params[key] = _normalize_ymd(value)
        if params.get("start_date") and params.get("end_date") and params["start_date"] > params["end_date"]:
            raise ValueError("index_kline start_date must not exceed end_date")
    elif intent == "index_intraday_compare":
        period = str(params.get("period", "15m"))
        if period not in {"5m", "15m", "60m"}:
            raise ValueError("index_intraday_compare period must be 5m, 15m, or 60m")
        params["period"] = period
        trade_date = params.get("trade_date")
        if trade_date is not None and (
            not isinstance(trade_date, str)
            or not re.fullmatch(r"(?:\d{8}|\d{4}-\d{2}-\d{2})", trade_date)
        ):
            raise ValueError("index_intraday_compare trade_date must use YYYYMMDD")
        if trade_date is not None:
            params["trade_date"] = _normalize_ymd(trade_date)
    elif intent == "wind_enrichment":
        capability = params.get("capability")
        if capability not in WIND_ENRICHMENT_CAPABILITIES:
            raise ValueError("wind_enrichment requires a supported capability")
        for key in ("codes", "fields"):
            value = params.get(key)
            if value is not None and not isinstance(value, (list, tuple)):
                raise ValueError(f"wind_enrichment {key} must be a list")
            if value is not None:
                params[key] = list(value)
        code = params.get("code")
        codes = params.get("codes")
        if code is not None and not str(code).strip():
            raise ValueError("wind_enrichment code must be non-empty")
        if codes is not None and any(not str(item).strip() for item in codes):
            raise ValueError("wind_enrichment codes cannot contain empty values")
        if (codes is not None and len(codes) > 1) or (
            code is not None and codes is not None
        ):
            raise ValueError("wind_enrichment supports a single target")
        nested = params.get("params")
        if nested is not None and not isinstance(nested, dict):
            raise ValueError("wind_enrichment params must be a mapping")
        nested = nested or {}
        if capability != "announcements" and "top_k" in nested:
            raise ValueError(
                "wind_enrichment top_k is only supported for announcements"
            )
        allowed_nested = (
            {"question", "top_k"}
            if capability == "announcements"
            else {"question", "lang"}
        )
        unknown_nested = set(nested) - allowed_nested
        if unknown_nested:
            raise ValueError(
                "unsupported wind_enrichment params: "
                + ", ".join(sorted(unknown_nested))
            )
        question = nested.get("question")
        if question is not None and (
            not isinstance(question, str) or not question.strip()
        ):
            raise ValueError("wind_enrichment question must be a non-empty string")
        lang = nested.get("lang")
        if lang is not None and (not isinstance(lang, str) or not lang.strip()):
            raise ValueError("wind_enrichment lang must be a non-empty string")
        top_k = nested.get("top_k")
        if top_k is not None and (
            not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0
        ):
            raise ValueError("wind_enrichment top_k must be a positive integer")
        if not question and not any(
            params.get(key) for key in ("code", "codes", "fields")
        ):
            raise ValueError("wind_enrichment requires question, code, codes, or fields")


def _analyze_data(intent: str, params: dict, data: object) -> tuple[bool, bool, int]:
    if not isinstance(data, dict) or data.get("error"):
        return False, False, 0
    if intent == "review_sentiment":
        if params.get("query") is not None:
            rows = data.get("datas")
            count = len(rows) if isinstance(rows, list) else 0
            return isinstance(rows, list), isinstance(rows, list) and not rows, count
        if "_total" in data:
            try:
                count = max(0, int(data.get("_total") or 0))
            except (TypeError, ValueError):
                return False, False, 0
            return count > 0, False, count
        required = {"zt_count", "zb_count", "dt_count", "pools"}
        if required.issubset(data):
            count = sum(int(data.get(key, 0) or 0) for key in ("zt_count", "zb_count", "dt_count"))
            return True, count == 0, count
        return False, False, 0
    if intent == "realtime_market":
        business_keys = {
            key for key in data if key not in {"_meta", "_source", "_stocktoday"}
        }
        count = int(bool(business_keys))
        return bool(count) or "_stocktoday" in data, not count and "_stocktoday" in data, count
    if intent == "sector_index":
        rows = data.get("items")
        count = len(rows) if isinstance(rows, list) else 0
        return isinstance(rows, list), isinstance(rows, list) and not rows, count
    if intent == "stock_snapshot":
        count = sum(
            isinstance(data.get(code), dict) and not data[code].get("error")
            for code in params["codes"]
        )
        is_declared_empty = count == 0 and "_stocktoday" in data
        return count > 0 or is_declared_empty, is_declared_empty, count
    if intent == "stock_kline":
        rows = data.get("bars")
        count = len(rows) if isinstance(rows, list) else 0
        return isinstance(rows, list), isinstance(rows, list) and not rows, count
    if intent == "market_limit_state":
        required = {"zt_count", "zb_count", "dt_count", "break_rate", "max_board", "pools"}
        if not required.issubset(data):
            return False, False, 0
        count = sum(int(data.get(key, 0) or 0) for key in ("zt_count", "zb_count", "dt_count"))
        return True, count == 0, count
    if intent in {
        "market_limit_board",
        "market_hot_rank",
        "industry_flow",
        "fund_flow",
        "northbound_flow",
        "legacy_hot_rank",
    }:
        rows = data.get("items")
        count = len(rows) if isinstance(rows, list) else 0
        return isinstance(rows, list), isinstance(rows, list) and not rows, count
    if intent == "index_kline":
        if isinstance(data.get("items"), list):
            count = len(data["items"])
            return isinstance(data.get("items"), list), not data["items"], count
        rows = data.get("bars")
        count = len(rows) if isinstance(rows, list) else 0
        return isinstance(rows, list), isinstance(rows, list) and not rows, count
    if intent == "index_intraday_compare":
        if isinstance(data.get("items"), list):
            count = len(data["items"])
            return True, not data["items"], count
        count = sum(
            1
            for key in ("上证15min", "深证15min", "创业15min")
            if isinstance(data.get(key), list) and data[key]
        )
        return bool(count), not count, count
    if intent == "wind_enrichment":
        rows = data.get("items")
        count = len(rows) if isinstance(rows, list) else 0
        return isinstance(rows, list), isinstance(rows, list) and not rows, count
    container = {
        "stocktoday_data": "items",
        "stock_event": "items",
        "research": "reports",
        "filings": "filings",
        "news": "items",
    }.get(intent)
    if container:
        rows = data.get(container)
        count = len(rows) if isinstance(rows, list) else 0
        return isinstance(rows, list), isinstance(rows, list) and not rows, count
    count = int(bool(data))
    return bool(count), False, count


def _finite_number(value: object, *, positive: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(float(value)):
        return False
    return float(value) > 0 if positive else True


def _parse_fact_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=TZ_SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(TZ_SHANGHAI)


def _now_shanghai() -> datetime:
    return datetime.now(TZ_SHANGHAI)


def _market_fact_time_failure(
    stamp: datetime, now: datetime, max_age_sec: int, stale_code: str
) -> str | None:
    """Measure quote age only while A-share trading can update the quote."""

    try:
        age = market_fact_age_seconds(stamp, now)
    except TradeCalendarUnavailable:
        return "QUALITY_TRADE_CALENDAR_UNAVAILABLE"
    return stale_code if age is None or age > max_age_sec else None


def _fetch_time_failure(stamp: datetime, now: datetime, max_age_sec: int) -> str | None:
    """Fetch time is a receipt; only same-day trading gaps pause its age."""

    if stamp > now + timedelta(minutes=5):
        return "QUALITY_STALE"
    age = (now - stamp).total_seconds()
    if stamp.date() == now.date():
        try:
            in_trading_day = is_trading_day(now.date())
        except TradeCalendarUnavailable:
            return "QUALITY_TRADE_CALENDAR_UNAVAILABLE"
        if in_trading_day and stamp.time() >= datetime_time(9, 15):
            age = session_seconds(now) - session_seconds(stamp)
    return "QUALITY_STALE" if age > max_age_sec else None


def _quality_failure_code(
    intent: str,
    params: dict,
    data: object,
    outcome: ProviderOutcome,
    max_age_sec: int,
) -> str | None:
    """Reject capability-invalid data before it can terminate a route."""

    if not isinstance(data, dict):
        return "QUALITY_MISSING_FIELDS"

    if intent == "stocktoday_data" or (
        intent in {"stock_snapshot", "stock_kline"}
        and params.get("source") == "stocktoday"
    ):
        # Explicit source/dataset calls retain the provider observation so
        # callers can see stale/unknown/filter-degraded semantics directly.
        return None

    now = _now_shanghai()
    if outcome.fetched_at:
        fetched_at = _parse_fact_datetime(outcome.fetched_at)
        if fetched_at is not None:
            if intent in {"stock_snapshot", "realtime_market"}:
                failure = _fetch_time_failure(fetched_at, now, max_age_sec)
                if failure is not None:
                    return failure
            elif (now - fetched_at).total_seconds() > max_age_sec:
                return "QUALITY_STALE"

    observation = data.get("_stocktoday")
    if isinstance(observation, dict):
        if observation.get("status") == "stale":
            return "QUALITY_STALE"
        if intent == "stock_kline" and observation.get("status") == "unverified_bar_time":
            return "QUALITY_KLINE_BAR_TIME"
        if observation.get("filter_violations"):
            return "QUALITY_DATE_MISMATCH"

    if intent == "realtime_market":
        required = ("上证指数", "深证指数", "创业指数")
        if any(not _finite_number(data.get(key), positive=True) for key in required):
            return "QUALITY_INDEX_INCOMPLETE"
        return None

    if intent == "stock_snapshot":
        # An explicit StockToday source intentionally preserves its vendor
        # observation semantics (including stale/unknown timestamps) for
        # callers that asked for that source only.  Automatic routes and the
        # realtime polling profile must satisfy the stricter canonical quote
        # contract below.
        for requested in params["codes"]:
            row = data.get(requested)
            if not isinstance(row, dict) or row.get("error"):
                return "QUALITY_SNAPSHOT_INCOMPLETE"
            row_code = row.get("code")
            if row_code is not None and str(row_code).split(".")[0] != str(requested).split(".")[0]:
                return "QUALITY_CODE_MISMATCH"
            price = row.get("price", row.get("最新价"))
            if not _finite_number(price, positive=True):
                return "QUALITY_SNAPSHOT_FIELDS"
            for field in ("last_close", "open", "high", "low", "volume", "amount", "quote_time"):
                if field not in row or row.get(field) is None:
                    return "QUALITY_SNAPSHOT_FIELDS"
            for field in ("last_close", "open", "high", "low"):
                if not _finite_number(row.get(field), positive=True):
                    return "QUALITY_SNAPSHOT_FIELDS"
            for field in ("volume", "amount"):
                if not _finite_number(row.get(field)) or float(row[field]) < 0:
                    return "QUALITY_SNAPSHOT_FIELDS"
            quote_time = _parse_fact_datetime(row.get("quote_time"))
            if quote_time is None:
                return "QUALITY_SNAPSHOT_FIELDS"
            failure = _market_fact_time_failure(
                quote_time, now, max_age_sec, "QUALITY_SNAPSHOT_STALE"
            )
            if failure is not None:
                return failure
        return None

    if intent == "stock_kline":
        bars = data.get("bars")
        if not isinstance(bars, list) or not bars:
            return "QUALITY_KLINE_EMPTY"
        if data.get("adjustment") != params.get("adjustment", "none"):
            return "QUALITY_ADJUSTMENT_MISMATCH"
        if data.get("volume_unit") != "share" or data.get("amount_unit") != "CNY":
            return "QUALITY_KLINE_UNITS"
        start_date = params.get("start_date")
        end_date = params.get("end_date")
        for bar in bars:
            if not isinstance(bar, dict):
                return "QUALITY_KLINE_FIELDS"
            required = ("datetime", "open", "high", "low", "close", "volume", "amount")
            if any(key not in bar for key in required):
                return "QUALITY_KLINE_FIELDS"
            if any(not _finite_number(bar.get(key)) for key in ("open", "high", "low", "close")):
                return "QUALITY_KLINE_FIELDS"
            if not _finite_number(bar.get("volume"), positive=True) or not _finite_number(bar.get("amount"), positive=True):
                return "QUALITY_KLINE_FIELDS"
            stamp = _parse_fact_datetime(str(bar.get("datetime")))
            if stamp is None:
                return "QUALITY_KLINE_DATE"
            stamp_date = stamp.strftime("%Y%m%d")
            if start_date and stamp_date < start_date:
                return "QUALITY_DATE_MISMATCH"
            if end_date and stamp_date > end_date:
                return "QUALITY_DATE_MISMATCH"
            if stamp > now + timedelta(minutes=5):
                return "QUALITY_FUTURE_BAR"
            if (
                params.get("period") in {"daily", "weekly", "monthly"}
                and stamp.date() == now.date()
                and now.time().replace(tzinfo=None) < datetime_time(15, 5)
            ):
                return "QUALITY_INCOMPLETE_BAR"
        return None

    if intent == "index_intraday_compare":
        required_names = ("上证15min", "深证15min", "创业15min")
        for name in required_names:
            rows = data.get(name)
            if not isinstance(rows, list) or not rows:
                return "QUALITY_COMPARE_INCOMPLETE"
            for row in rows:
                if not isinstance(row, dict):
                    return "QUALITY_COMPARE_FIELDS"
                required = {"t", "chg", "vol", "volRatio", "amount"}
                if not required.issubset(row):
                    return "QUALITY_COMPARE_FIELDS"
                if not row.get("_cum") and "yesterdayAmt" not in row:
                    return "QUALITY_COMPARE_FIELDS"
        return None

    if intent == "legacy_hot_rank":
        if not isinstance(data.get("reason_stats"), dict) or "zt_count" not in data:
            return "QUALITY_HOT_RANK_SHAPE"
        return None

    if intent == "index_kline":
        requested = params.get("codes") or [params.get("index_code")]
        requested = [code for code in requested if code]
        items = data.get("items")
        if isinstance(items, list):
            if len(items) != len(requested):
                return "QUALITY_INDEX_INCOMPLETE"
            observed = set()
            for item in items:
                if not isinstance(item, dict):
                    return "QUALITY_INDEX_INCOMPLETE"
                code = item.get("index_code")
                if code not in requested:
                    return "QUALITY_INDEX_CODE_MISMATCH"
                if code in observed:
                    return "QUALITY_INDEX_INCOMPLETE"
                observed.add(code)
                failure = _quality_failure_code(
                    "stock_kline", params, item, outcome, max_age_sec
                )
                if failure is not None:
                    return failure
            if observed != set(requested):
                return "QUALITY_INDEX_INCOMPLETE"
            return None
        if len(requested) != 1:
            return "QUALITY_INDEX_INCOMPLETE"
        if data.get("index_code") != requested[0]:
            return "QUALITY_INDEX_CODE_MISMATCH"
        return _quality_failure_code(
            "stock_kline", params, data, outcome, max_age_sec
        )

    return None


def _failure_quality(intent: str, params: dict, status: str, count: int) -> dict:
    quality = assess_quality(
        [],
        expected_row_shape=params.get("expected_row_shape")
        if intent == "review_sentiment"
        else None,
        expected_count=params.get("expected_count")
        if intent == "review_sentiment"
        else None,
        source_error=status == "error",
    )
    quality.update({"status": status, "returned_count": count})
    return quality


def _source_tier(
    intent: str,
    params: dict,
    spec: RouteSpec,
    provider_used: str | None,
    attempts: list[ProviderAttempt],
) -> str:
    if intent == "stocktoday_data" or params.get("source") == "stocktoday":
        return "explicit"
    if provider_used is not None:
        return "primary" if provider_used == spec.providers[0] else "fallback"
    return "primary" if not any(
        attempt.provider != spec.providers[0] for attempt in attempts
    ) else "fallback"


def _query_with(
    intent: str,
    params: dict,
    *,
    provider_loader: Callable[[str], object] | None = None,
    state_loader: Callable[[], ProviderState] | None = None,
    policy_loader: Callable[[], object] | None = None,
) -> dict:
    """Canonical router core with private, test/smoke-only dependency injection."""

    call_params = dict(params)
    provider_loader = provider_loader or _provider_for
    state_loader = state_loader or _provider_state
    policy_loader = policy_loader or load_compiled_policy
    try:
        compiled_policy = policy_loader()
    except Exception:
        # A policy loader failure must leave the V2 route intact.
        compiled_policy = load_compiled_policy()
    spec: RouteSpec = compiled_policy.route(intent, call_params)
    _validate_params(intent, call_params)
    attempts: list[ProviderAttempt] = []
    provider_used = None
    data = None
    final_status = "error"
    final_count = 0
    final_quality = None
    fetched_at = None
    auth = None
    observed_auth = None

    for provider_index, provider_name in enumerate(spec.providers):
        breaker = state_loader().active_breaker(provider_name)
        if breaker:
            attempts.append(ProviderAttempt(provider_name, "breaker_open", breaker["error_code"], 0))
            continue
        try:
            provider_params = dict(call_params)
            # The use-case is a pipeline-owned routing profile, not a
            # provider selector.  Do not leak it into adapter payloads.
            provider_params.pop("use_case", None)
            if intent in {
                "industry_flow",
                "northbound_flow",
                "legacy_hot_rank",
            } and call_params.get("use_case") == "realtime_poll":
                # Current-session is an internal semantic marker for legacy
                # adapters; it is not a provider selector and never appears
                # in the public contract.
                provider_params["current_session"] = True
            if provider_name == "stocktoday" and intent in {
                "stock_snapshot",
                "stock_kline",
            }:
                # StockToday validates its source ownership at the provider
                # boundary.  Keep this internal marker scoped to that one
                # provider; fallback providers must receive the public V2
                # params unchanged.
                provider_params["source"] = "stocktoday"
            outcome = provider_loader(provider_name).call(intent, provider_params)
        except (TimeoutError, socket.timeout):
            outcome = ProviderOutcome(provider_name, "timeout", error_code="TIMEOUT")
        except ImportError:
            outcome = ProviderOutcome(provider_name, "dependency_missing", error_code="DEPENDENCY_MISSING")
        except Exception as error:
            outcome = ProviderOutcome(
                provider_name,
                "provider_error",
                error_code=_safe_error_code(type(error).__name__, "PROVIDER_ERROR"),
            )
        outcome_status = (
            outcome.status
            if outcome.status in _CONTINUE_STATUSES | {"success", "empty"}
            else "provider_error"
        )
        observed_auth = _more_severe_auth(observed_auth, outcome.auth)
        actual = outcome.provider or provider_name
        if actual != provider_name and outcome_status in {"success", "empty"}:
            provenance = outcome.provenance or {}
            verified = (
                provenance.get("verified") is True
                and provenance.get("fallback_from") == provider_name
                and provenance.get("kind") == "source_internal"
            )
            if actual not in spec.providers or not verified:
                attempts.append(
                    ProviderAttempt(provider_name, "provider_error", "INCOMPATIBLE_PROVIDER", max(0, int(outcome.latency_ms)))
                )
                continue
            attempts.append(ProviderAttempt(provider_name, "provider_error", "INTERNAL_FALLBACK", 0))
        if outcome_status in {"success", "empty"}:
            valid, is_empty, count = _analyze_data(intent, call_params, outcome.data)
            if not valid:
                attempts.append(
                    ProviderAttempt(
                        actual,
                        "provider_error",
                        "INVALID_EMPTY" if outcome_status == "empty" else "INVALID_RESPONSE",
                        max(0, int(outcome.latency_ms)),
                    )
                )
                continue
            if outcome_status == "empty" and not is_empty:
                attempts.append(
                    ProviderAttempt(actual, "provider_error", "STATUS_DATA_MISMATCH", max(0, int(outcome.latency_ms)))
                )
                continue
            if not is_empty:
                quality_error = _quality_failure_code(
                    intent,
                    call_params,
                    outcome.data,
                    outcome,
                    spec.max_age_sec,
                )
                if quality_error is not None:
                    attempts.append(
                        ProviderAttempt(
                            actual,
                            "quality_failure",
                            quality_error,
                            max(0, int(outcome.latency_ms)),
                        )
                    )
                    continue
            terminal = "empty" if is_empty else "success"
            if (
                terminal == "empty"
                and spec.empty_policy == EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED
            ):
                empty_attempt = ProviderAttempt(
                    actual,
                    terminal,
                    None,
                    max(0, int(outcome.latency_ms)),
                )
                if (
                    provider_index < len(spec.providers) - 1
                    or any(item.status != "empty" for item in attempts)
                ):
                    attempts.append(empty_attempt)
                    auth = outcome.auth or auth
                    continue
            data, final_quality = normalize_success(
                intent,
                call_params,
                outcome.data,
                provider=actual,
                spec=spec,
                attempts=attempts,
            )
            attempts.append(ProviderAttempt(actual, terminal, None, max(0, int(outcome.latency_ms))))
            provider_used = actual
            final_count = count
            fetched_at = outcome.fetched_at
            auth = outcome.auth
            final_status = (
                "degraded"
                if terminal == "success" and any(item.status != "success" for item in attempts[:-1])
                else terminal
            )
            break
        attempts.append(
            ProviderAttempt(
                provider_name,
                outcome_status,
                _safe_error_code(outcome.error_code, "PROVIDER_ERROR"),
                max(0, int(outcome.latency_ms)),
            )
        )
        auth = outcome.auth or auth

    quality = final_quality or _failure_quality(
        intent, call_params, final_status, final_count
    )
    policy_status = getattr(compiled_policy, "policy_status", "inactive")
    policy_evidence_sha256 = getattr(
        compiled_policy, "policy_evidence_sha256", None
    )
    pipeline_version = getattr(compiled_policy, "pipeline_version", "3.0")
    route_policy_version = getattr(
        compiled_policy, "route_policy_version", "3.0"
    )
    result = build_result(
        intent=intent,
        data=data,
        status=final_status,
        provider_used=provider_used,
        attempts=attempts,
        data_scope=spec.data_scope,
        trade_usage=spec.trade_usage,
        quality=quality,
        max_age_sec=spec.max_age_sec,
        fetched_at=fetched_at,
        auth=auth if provider_used is not None else observed_auth or auth,
        pipeline_version=pipeline_version,
        route_policy_version=route_policy_version,
        source_tier=_source_tier(
            intent, call_params, spec, provider_used, attempts
        ),
        policy_evidence_sha256=policy_evidence_sha256,
        policy_status=policy_status,
    )
    if provider_used == "stocktoday" and isinstance(data, dict):
        observation = data.get("_stocktoday", {})
        result["_meta"]["observation"] = dict(observation)
        if result["_meta"]["status"] == "success" and observation.get("status") in {"stale", "unknown", "unverified_bar_time"}:
            result["_meta"]["status"] = "degraded"
            result["_meta"]["quality"]["status"] = "semantic_degraded"
        if observation.get("filter_violations") and result["_meta"]["status"] in {"success", "degraded"}:
            result["_meta"]["status"] = "degraded"
            result["_meta"]["quality"]["status"] = "semantic_degraded"
            result["_meta"]["quality"]["reason_codes"].append("FILTER_MISMATCH")
        if result["_meta"]["status"] == "success" and (
            observation.get("truncated") or observation.get("missing_codes") or observation.get("duplicate_code_count")
            or result["_meta"]["quality"]["status"] == "partial"
        ):
            result["_meta"]["status"] = "degraded"
            result["_meta"]["quality"]["status"] = "partial"
    if intent == "market_hot_rank" and result["_meta"]["status"] in {"error", "empty"}:
        result["_meta"]["source_gap"] = (
            "no_semantically_equivalent_hot_rank_fallback"
        )
    return result


def query(intent: str, **params) -> dict:
    """Resolve one canonical intent through semantically compatible providers."""

    return _query_with(intent, params)
