"""Legacy V2 shape projections over the one canonical query router.

This module performs no transport, retry, breaker, or provider selection.
"""

from __future__ import annotations

from typing import Any

import ym_stock_data.api as public_api


def _project(result: dict, *, data_type: str) -> dict:
    data: Any = result.get("data")
    projected = dict(data) if isinstance(data, dict) else {"data": data}
    meta = dict(result.get("_meta", {}))
    meta.setdefault("data_type", data_type)
    projected["_meta"] = meta
    if meta.get("status") == "error":
        attempts = meta.get("attempts", [])
        error_code = attempts[-1].get("error_code") if attempts else None
        projected.setdefault("error", error_code or "QUERY_FAILED")
        projected.setdefault("error_type", "ProviderError")
    return projected


def fetch_index() -> dict:
    return _project(public_api.query("realtime_market"), data_type="index")


def fetch_breadth() -> dict:
    result = public_api.query("review_sentiment")
    projected = _project(result, data_type="breadth")
    aggregates = projected.get("aggregates")
    breadth = aggregates.get("breadth") if isinstance(aggregates, dict) else None
    if isinstance(breadth, dict):
        raw = dict(breadth)
        raw["_meta"] = dict(projected["_meta"])
        return raw
    return projected


def fetch_quotes(codes: list[str]) -> dict:
    return _project(
        public_api.query("stock_snapshot", codes=codes),
        data_type="quotes",
    )


def fetch_kline(code: str, *, period: str = "daily", count: int | None = None) -> dict:
    params = {"code": code, "period": period}
    if count is not None:
        params["count"] = count
    return _project(public_api.query("stock_kline", **params), data_type="kline")


def fetch_sector_index(
    codes: list[str] | None = None,
    names: list[str] | None = None,
) -> dict:
    return _project(
        public_api.query("sector_index", codes=codes, names=names),
        data_type="sector_index",
    )


def query_iwencai(
    query_str: str,
    *,
    limit: int = 50,
    expected_row_shape: str | None = None,
    expected_count: int | None = None,
) -> dict:
    params: dict[str, object] = {"query": query_str, "limit": limit}
    if expected_row_shape is not None:
        params["expected_row_shape"] = expected_row_shape
    if expected_count is not None:
        params["expected_count"] = expected_count
    return _project(
        public_api.query("review_sentiment", **params),
        data_type="iwencai",
    )


def fetch_limit_state(date: str | None = None) -> dict:
    return _project(
        public_api.query("market_limit_state", date=date),
        data_type="limit_state",
    )


def fetch_stock_event(event: str, code: str, page_size: int = 30) -> dict:
    return _project(
        public_api.query(
            "stock_event",
            event=event,
            code=code,
            page_size=page_size,
        ),
        data_type="stock_event",
    )


def fetch_v1(data_type: str, **kwargs) -> dict:
    """Removed provider escape hatch retained only for import compatibility."""

    raise RuntimeError(
        f"V2 direct source escape hatch removed for {data_type}; use ym_stock_data.query"
    )
