"""V2 adapters over stable source modules.

The v2 layer owns intent/policy/meta handling. It reuses the proven source
modules, but does not route through v1 fetch().
"""

from datetime import datetime
from typing import Any

from ym_stock_data.sources import iwencai, pytdx, ths_industry


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _with_meta(raw: Any, *, data_type: str, source: str) -> dict:
    result = raw if isinstance(raw, dict) else {"data": raw}
    meta = dict(result.get("_meta", {}))
    meta.setdefault("data_type", data_type)
    meta.setdefault("source", source)
    meta.setdefault("fetched_at", _now_iso())
    if result.get("error"):
        meta["error"] = True
    result["_meta"] = meta
    return result


def fetch_index() -> dict:
    return _with_meta(pytdx.fetch_index(), data_type="index", source="pytdx")


def fetch_quotes(codes: list[str]) -> dict:
    return _with_meta(pytdx.fetch_quotes(codes), data_type="quotes", source="pytdx")


def fetch_kline(code: str, *, period: str = "daily", count: int | None = None) -> dict:
    result = _with_meta(pytdx.fetch_kline(code, period=period), data_type="kline", source="pytdx")
    result.setdefault("period", period)
    if count is not None:
        bars = result.get("bars", [])
        if isinstance(bars, list):
            result["bars"] = bars[-count:]
            result["requested_count"] = count
            result["returned_bars"] = len(result["bars"])
    return result


def fetch_sector_index(codes: list[str] | None = None, names: list[str] | None = None) -> dict:
    return _with_meta(
        ths_industry.fetch_sector_index(codes=codes, names=names),
        data_type="sector_index",
        source="ths_industry",
    )


def query_iwencai(query_str: str, *, limit: int = 50) -> dict:
    return _with_meta(iwencai.query(query_str, limit=limit), data_type="iwencai", source="iwencai")


def fetch_v1(data_type: str, **kwargs) -> dict:
    """Compatibility escape hatch; resolve() should not call this."""
    from ym_stock_data import fetch

    return fetch(data_type, **kwargs)
