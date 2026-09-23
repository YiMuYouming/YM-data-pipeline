"""Regenerate the credential-free StockToday V3 probe manifest.

This is a local, offline generator.  It consumes only the frozen inventory and
the reviewed fixed-probe table below; it never contacts the vendor gateway.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ym_stock_data.providers.stocktoday_inventory import load_inventory


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "ym_stock_data" / "v3" / "stocktoday-probes.v3.json"
SCHEMA_VERSION = "3.1"


FIXED = {
    "balancesheet_vip": {"ts_code": "600519.SH", "period": "20241231"},
    "cashflow_vip": {"ts_code": "600519.SH", "period": "20241231"},
    "dc_concept": {"trade_date": "20250930"},
    "dc_concept_cons": {"trade_date": "20250930"},
    "etf_mins": {"ts_code": "510330.SH", "freq": "1min", "start_date": "2025-09-30 09:30:00", "end_date": "2025-09-30 10:00:00"},
    "express_vip": {"ts_code": "600519.SH", "start_date": "20240101", "end_date": "20241231"},
    "fina_indicator_vip": {"ts_code": "600519.SH", "period": "20241231"},
    "fina_mainbz_vip": {"ts_code": "600519.SH", "start_date": "20240101", "end_date": "20241231"},
    "forecast_vip": {"ts_code": "600519.SH", "period": "20241231"},
    "fund_company": {},
    "fund_factor_pro": {"ts_code": "510330.SH", "start_date": "20250101", "end_date": "20250930"},
    "fund_sales_ratio": {"year": "2024"},
    "fund_sales_vol": {"year": "2024", "quarter": "4"},
    "fut_weekly_monthly": {"ts_code": "CU2507.SHF", "freq": "W", "start_date": "20250101", "end_date": "20250930", "exchange": "SHFE"},
    "hk_basic": {"ts_code": "00700.HK", "list_status": "L"},
    "hm_list": {},
    "idx_factor_pro": {"ts_code": "000300.SH", "start_date": "20250101", "end_date": "20250930"},
    "income_vip": {"ts_code": "600519.SH", "period": "20241231"},
    "kpl_concept": {"trade_date": "20250930"},
    "pro_bar": {"ts_code": "600519.SH", "start_date": "20250901", "end_date": "20250930", "asset": "E", "adj": "qfq", "freq": "D"},
    "realtime_list": {},
    "realtime_quote": {"ts_code": "600519.SH"},
    "realtime_tick": {"ts_code": "600519.SH"},
    "rt_etf_min": {"ts_code": "510330.SH", "freq": "1MIN"},
    "rt_etf_tick": {"ts_code": "510330.SH", "topic": "1"},
    "rt_hk_tick": {"ts_code": "00700.HK"},
    "rt_idx_tick": {"ts_code": "000300.SH"},
    "rt_sw_k": {"ts_code": "801010.SI"},
    "rt_sw_tick": {"ts_code": "801010.SI"},
    "rt_tick": {"ts_code": "600519.SH"},
    "st": {"ts_code": "600519.SH", "pub_date": "20250930", "imp_date": "20250930"},
    "stk_factor_pro": {"ts_code": "600519.SH", "trade_date": "20250930", "start_date": "20250101", "end_date": "20250930"},
    "ths_index": {"ts_code": "885001.TI", "type": "N"},
    "ths_news": {"start_date": "20250901", "end_date": "20250930", "limit": 100},
    "top_inst": {"ts_code": "600519.SH", "trade_date": "20250930"},
    "us_basic": {"ts_code": "AAPL"},
}


FIXED_FIELDS = {
    "balancesheet_vip": "ts_code,ann_date,end_date,period",
    "cashflow_vip": "ts_code,ann_date,end_date,period",
    # The frozen local reference documents the request parameters for these
    # endpoints, but not a stable response schema.  An empty fields request is
    # intentional: do not invent columns that would turn a reachable probe
    # into a schema failure.
    "dc_concept": "",
    "dc_concept_cons": "",
    "etf_mins": "ts_code,trade_time,open,high,low,close,vol,amount",
    "express_vip": "ts_code,ann_date,end_date,revenue,operate_profit,n_income",
    "fina_indicator_vip": "ts_code,ann_date,end_date,eps,roe",
    "fina_mainbz_vip": "ts_code,end_date,bz_item,bz_sales,bz_profit",
    "forecast_vip": "ts_code,ann_date,end_date,period,type",
    "fund_company": "",
    "fund_factor_pro": "ts_code,trade_date,close",
    "fund_sales_ratio": "year,market,broker,ratio",
    "fund_sales_vol": "year,quarter,market,volume",
    "fut_weekly_monthly": "ts_code,trade_date,open,high,low,close,vol,amount",
    "hk_basic": "ts_code,name,list_date",
    "hm_list": "",
    "idx_factor_pro": "ts_code,trade_date,pe,pb",
    "income_vip": "ts_code,ann_date,end_date,period,revenue,n_income",
    "kpl_concept": "",
    "pro_bar": "ts_code,trade_date,open,high,low,close,vol,amount",
    "realtime_list": "",
    "realtime_quote": "",
    "realtime_tick": "",
    "rt_etf_min": "ts_code,trade_time,close,vol",
    "rt_etf_tick": "",
    "rt_hk_tick": "",
    "rt_idx_tick": "",
    "rt_sw_k": "",
    "rt_sw_tick": "",
    "rt_tick": "",
    "st": "",
    "stk_factor_pro": "ts_code,trade_date,close",
    "ths_index": "ts_code,name,type",
    "ths_news": "",
    "top_inst": "ts_code,trade_date,buy,sell",
    "us_basic": "ts_code,name,exchange,list_date",
}


CONSERVATIVE_UNKNOWN_FIXED_FIELDS = frozenset(
    {
        "dc_concept",
        "dc_concept_cons",
        "fund_company",
        "hm_list",
        "kpl_concept",
        "realtime_list",
        "realtime_quote",
        "realtime_tick",
        "rt_etf_tick",
        "rt_hk_tick",
        "rt_idx_tick",
        "rt_sw_k",
        "rt_sw_tick",
        "rt_tick",
        "st",
        "ths_news",
    }
)


DAILY_NATURAL_KEY_METHODS = frozenset(
    {
        "daily",
        "daily_basic",
        "bak_daily",
        "pro_bar",
        "fund_factor_pro",
        "idx_factor_pro",
        "stk_factor_pro",
        "fut_weekly_monthly",
    }
)


INTRADAY_NATURAL_KEY_METHODS = frozenset(
    {
        "etf_mins",
        "ft_mins",
        "hk_mins",
        "idx_mins",
        "opt_mins",
        "stk_mins",
        "rt_etf_min",
        "rt_fut_min",
        "rt_idx_min",
        "rt_min",
        "realtime_tick",
        "rt_etf_tick",
        "rt_hk_tick",
        "rt_idx_tick",
        "rt_sw_tick",
        "rt_tick",
    }
)


FALLBACK_FIELDS = {
    "realtime_list": "ts_code,name,close,pre_close",
    "fund_company": "name,province,city",
    "us_basic": "ts_code,name,exchange,list_date",
}


EXACT_POINT_METHODS = frozenset(
    {
        "hk_basic",
        "realtime_quote",
        "realtime_tick",
        "rt_etf_k",
        "rt_etf_tick",
        "rt_hk_k",
        "rt_hk_tick",
        "rt_idx_k",
        "rt_idx_tick",
        "rt_k",
        "rt_sw_k",
        "rt_sw_tick",
        "rt_tick",
        "us_basic",
    }
)


def sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def split_fields(value: object) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def expected_fields(name: str, request_fields: str, nested: dict) -> list[str]:
    fields = split_fields(request_fields)
    if fields:
        return fields
    if name in CONSERVATIVE_UNKNOWN_FIXED_FIELDS:
        return []
    fields = split_fields(FIXED_FIELDS.get(name, FALLBACK_FIELDS.get(name, "")))
    if fields:
        return fields
    if nested.get("ts_code"):
        return ["ts_code"]
    for field in ("trade_date", "date", "month", "name", "year"):
        if field in nested:
            return [field]
    return ["ts_code"]


def filter_checks(name: str | dict, nested: dict | None = None) -> list[dict]:
    if nested is None:
        nested = name if isinstance(name, dict) else {}
        name = ""
    if name in CONSERVATIVE_UNKNOWN_FIXED_FIELDS:
        return []
    checks = []
    if nested.get("ts_code"):
        ts_code = str(nested["ts_code"])
        kind = "pattern" if any(marker in ts_code for marker in ("*", "?")) else "set_equal"
        checks.append({"field": "ts_code", "kind": kind})
    for field in ("trade_date", "date", "ann_date", "pub_date", "imp_date"):
        if nested.get(field):
            checks.append({"field": field, "kind": "date_equal"})
    if nested.get("start_date") or nested.get("end_date"):
        checks.append({"field": "date_range", "kind": "date_range", "response_fields": ["trade_date", "date", "end_date", "ann_date", "pub_date"]})
    if nested.get("m") or nested.get("start_m") or nested.get("end_m"):
        checks.append({"field": "month_range", "kind": "month_range", "response_fields": ["month"]})
    for field in ("limit_type", "type", "list_status", "classify", "market", "status", "tag", "hot_type", "year", "quarter", "period"):
        if field in nested and nested[field] not in (None, ""):
            checks.append({"field": field, "kind": "scalar_equal"})
    existing = {check["field"] for check in checks}
    for field in sorted(
        {
            "exchange", "idx_type", "topic", "name", "con_code", "theme_code",
            "fut_type", "call_put", "is_new", "is_open", "content_type",
            "market_type", "exchange_id", "bank", "country", "currency", "event",
            "src", "org", "ptype", "report_date", "level", "publisher", "category",
            "symbol", "index_code", "l1_code", "l2_code", "l3_code", "report_type",
            "comp_type",
        }
        & set(nested)
        - existing
    ):
        if nested[field] not in (None, ""):
            checks.append({"field": field, "kind": "scalar_equal"})
    return checks


def is_exact_point(name: str, nested: dict) -> bool:
    range_fields = ("start_date", "end_date", "start_m", "end_m", "limit", "offset")
    if any(nested.get(field) not in (None, "") for field in range_fields):
        return False
    ts_code = str(nested.get("ts_code", ""))
    if not ts_code or any(marker in ts_code for marker in ("*", "?")) or "," in ts_code:
        return False
    return name in EXACT_POINT_METHODS


def pagination_declaration(name: str, nested: dict) -> dict:
    total_required = not is_exact_point(name, nested)
    report_if_truncated = (
        "limit" in nested
        or "offset" in nested
        or name.startswith(("rt_", "realtime_"))
        or name.endswith("_mins")
        or name == "pro_bar"
    )
    return {
        "total_field": "total",
        "limit_field": "limit",
        "offset_field": "offset",
        "total_required": total_required,
        "truncation_policy": (
            "report_if_total_exceeds_rows"
            if total_required and report_if_truncated
            else "fail_if_total_exceeds_rows"
        ),
    }


def duplicate_keys(nested: dict, fields: list[str], *, name: str = "") -> list[str]:
    available = set(fields)
    if (
        name in INTRADAY_NATURAL_KEY_METHODS
        and {"ts_code", "trade_time"}.issubset(available)
    ):
        return ["ts_code", "trade_time"]
    if (
        name in DAILY_NATURAL_KEY_METHODS
        and {"ts_code", "trade_date"}.issubset(available)
    ):
        return ["ts_code", "trade_date"]
    if name.endswith(("_daily", "_weekly", "_monthly")) and {"ts_code", "trade_date"}.issubset(available):
        return ["ts_code", "trade_date"]
    return []


def build() -> dict:
    inventory = load_inventory()
    methods = {}
    for item in inventory["methods"]:
        name = item["name"]
        if item["has_example"]:
            example = dict(item["example"])
            fields = example.pop("fields", "")
            nested = example
            source = "vendor_example"
        else:
            nested = dict(FIXED[name])
            fields = FIXED_FIELDS[name]
            source = "fixed_parameters"
        expected = expected_fields(name, fields, nested)
        declared_filters = filter_checks(name, nested)
        pagination = pagination_declaration(name, nested)
        methods[name] = [
            {
                "probe_id": source,
                "source": source,
                "params": {"api_name": name, "params": nested, "fields": fields},
                "fixed_date": "20250930",
                "duplicate_key_fields": duplicate_keys(nested, split_fields(fields), name=name),
                "pagination": pagination,
                "expected_fields": expected,
                "filter_checks": declared_filters,
                "primary_declared": bool(expected and declared_filters and pagination["total_required"]),
            }
        ]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "inventory_sha256": inventory["payload_sha256"],
        "manifest_sha256": "",
        "methods": dict(sorted(methods.items())),
        "account_diagnostic": {"name": "token_info", "kind": "account_diagnostic", "primary_declared": False},
    }
    payload["manifest_sha256"] = sha256({key: value for key, value in payload.items() if key != "manifest_sha256"})
    return payload


if __name__ == "__main__":
    payload = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT}: {len(payload['methods'])} methods")
