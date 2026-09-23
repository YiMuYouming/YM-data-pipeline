"""Exact, read-only natural-language mappings for common A-share queries.

The registry is deliberately a small schema table.  It resolves an exact
phrase to a canonical ``query()`` call; it does not interpret arbitrary
natural language or call a provider itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_DATE = re.compile(r"^\d{8}$")
_PERIODS = frozenset({"1m", "5m", "15m", "60m"})


@dataclass(frozen=True)
class IntentSpec:
    name: str
    phrases: tuple[str, ...]
    intent: str
    api_name: str | None = None
    fixed: tuple[tuple[str, Any], ...] = ()
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    required_any: tuple[str, ...] = ()


_SPECS = (
    IntentSpec(
        "limit_up",
        ("查涨停板", "涨停板"),
        "market_limit_board",
        fixed=(("kind", "up"),),
        optional=("trade_date", "date"),
    ),
    IntentSpec(
        "limit_down",
        ("查跌停板", "跌停板"),
        "market_limit_board",
        fixed=(("kind", "down"),),
        optional=("trade_date", "date"),
    ),
    IntentSpec(
        "market_facts",
        ("查晋级率", "晋级率", "查市场事实"),
        "market_facts",
        optional=("trade_date",),
    ),
    IntentSpec(
        "ths_hot",
        ("查同花顺热榜", "同花顺热榜"),
        "market_hot_rank",
        fixed=(("source", "ths"),),
        optional=("trade_date", "limit"),
    ),
    IntentSpec(
        "dc_hot",
        ("查东财热榜", "东财热榜"),
        "market_hot_rank",
        fixed=(("source", "dc"),),
        optional=("trade_date", "limit"),
    ),
    IntentSpec(
        "realtime_market",
        ("查实时大盘", "实时大盘"),
        "realtime_market",
    ),
    IntentSpec(
        "stock_snapshot",
        ("查实时个股", "实时个股"),
        "stock_snapshot",
        required=("codes",),
    ),
    IntentSpec(
        "daily_kline",
        ("查日K", "日K"),
        "stock_kline",
        fixed=(("period", "daily"),),
        required=("code",),
        optional=("count", "start_date", "end_date", "adjustment"),
    ),
    IntentSpec(
        "qfq_daily_kline",
        ("查前复权日K", "前复权日K"),
        "stock_kline",
        fixed=(("period", "daily"), ("adjustment", "qfq")),
        required=("code",),
        optional=("count", "start_date", "end_date"),
    ),
    IntentSpec(
        "weekly_kline",
        ("查周K", "周K"),
        "stock_kline",
        fixed=(("period", "weekly"),),
        required=("code",),
        optional=("count", "start_date", "end_date", "adjustment"),
    ),
    IntentSpec(
        "monthly_kline",
        ("查月K", "月K"),
        "stock_kline",
        fixed=(("period", "monthly"),),
        required=("code",),
        optional=("count", "start_date", "end_date", "adjustment"),
    ),
    IntentSpec(
        "minute_kline",
        ("查分钟K", "分钟K"),
        "stock_kline",
        required=("code", "period"),
        optional=("count", "start_date", "end_date"),
    ),
    IntentSpec(
        "sector_index",
        ("查板块", "板块"),
        "sector_index",
        required_any=("names", "codes"),
        optional=("names", "codes"),
    ),
    IntentSpec(
        "industry_index",
        ("查行业", "行业"),
        "sector_index",
        required_any=("names", "codes"),
        optional=("names", "codes"),
    ),
    IntentSpec(
        "industry_flow",
        ("查行业资金流", "行业资金流"),
        "industry_flow",
        optional=("trade_date", "limit"),
    ),
    IntentSpec(
        "fund_flow",
        ("查市场资金流", "市场资金流"),
        "fund_flow",
        optional=("trade_date", "limit"),
    ),
    IntentSpec(
        "northbound_flow",
        ("查北向资金", "北向资金"),
        "northbound_flow",
        optional=("trade_date", "limit"),
    ),
    IntentSpec(
        "legacy_hot_rank",
        ("查旧热榜", "旧热榜"),
        "legacy_hot_rank",
        optional=("trade_date", "limit"),
    ),
    IntentSpec(
        "index_kline",
        ("查指数历史", "指数历史K", "查指数K"),
        "index_kline",
        required_any=("index_code", "codes"),
        optional=("index_code", "codes", "period", "count", "start_date", "end_date"),
    ),
)

INTENT_REGISTRY = {
    phrase: spec
    for spec in _SPECS
    for phrase in spec.phrases
}


def _phrase(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("registered intent phrase must be a non-empty string")
    return " ".join(value.split())


def _validate_value(key: str, value: Any) -> Any:
    if key in {"code", "name", "ts_code", "index_code"}:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"registered intent {key} must be a non-empty string")
        return value.strip()
    if key in {"codes", "names"}:
        if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
            raise ValueError(f"registered intent {key} must be a non-empty list")
        values = [str(item).strip() for item in value]
        if any(not item for item in values):
            raise ValueError(f"registered intent {key} cannot contain empty values")
        return values
    if key in {"count", "limit"}:
        if type(value) is not int or value <= 0:
            raise ValueError(f"registered intent {key} must be a positive integer")
        return value
    if key == "period":
        if value not in {"daily", "weekly", "monthly", *_PERIODS}:
            raise ValueError("registered intent period is not supported")
        return value
    if key in {"trade_date", "date", "start_date", "end_date"}:
        if not isinstance(value, str) or _DATE.fullmatch(value) is None:
            raise ValueError(f"registered intent {key} must use YYYYMMDD")
        return value
    if key == "fields":
        if not isinstance(value, (str, list, tuple)):
            raise ValueError("registered intent fields must be a string or list")
    if key == "adjustment":
        if value not in {"none", "qfq"}:
            raise ValueError("registered intent adjustment must be none or qfq")
    return value


def resolve_intent(phrase: str, **params: Any) -> dict[str, Any]:
    """Resolve one exact registered phrase to a canonical query descriptor."""

    normalized = _phrase(phrase)
    spec = INTENT_REGISTRY.get(normalized)
    if spec is None:
        raise ValueError(f"unsupported registered intent phrase: {phrase}")

    fixed = dict(spec.fixed)
    allowed = set(spec.required) | set(spec.optional) | set(spec.required_any)
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(
            "unsupported registered intent params: " + ", ".join(sorted(unknown))
        )
    for key, value in list(params.items()):
        if key in fixed and value != fixed[key]:
            raise ValueError(f"registered intent {key} is fixed to {fixed[key]!r}")
        params[key] = _validate_value(key, value)
    for key in spec.required:
        if key not in params:
            raise ValueError(f"registered intent requires {key}")
    if spec.required_any and not any(key in params for key in spec.required_any):
        raise ValueError(
            "registered intent requires one of: " + ", ".join(spec.required_any)
        )
    if spec.name == "minute_kline" and params.get("period") not in _PERIODS:
        raise ValueError("registered minute K requires period 1m, 5m, 15m, or 60m")

    values = {**fixed, **params}
    if spec.intent == "market_limit_board" and "trade_date" in values:
        if "date" in values:
            raise ValueError("registered intent accepts trade_date or date, not both")
        values["date"] = values.pop("trade_date")
    if spec.api_name is not None:
        return {
            "intent": "stocktoday_data",
            "params": {"api_name": spec.api_name, "params": values},
        }
    return {"intent": spec.intent, "params": values}


def list_registered_intents() -> tuple[str, ...]:
    """Return the exact phrases accepted by the registry."""

    return tuple(sorted(INTENT_REGISTRY))


__all__ = ["INTENT_REGISTRY", "IntentSpec", "list_registered_intents", "resolve_intent"]
