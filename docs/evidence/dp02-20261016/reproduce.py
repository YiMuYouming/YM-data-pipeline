"""Bounded, sequential public queries. Trace safe transport shape, never auth."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch
import urllib.request
import requests

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from ym_stock_data import query

CASES = {
    "limit": ("market_limit_state", {"date": "20261016"}),
    "promotion": ("market_limit_state", {"date": "20261016", "include_promotion": True}),
    "intraday": ("market_intraday_state", {}),
    "sector": ("sector_index", {"names": ["文化传媒", "出版传媒"]}),
    "sector-empty": ("sector_index", {"names": ["出版"]}),
    "compare": ("index_intraday_compare", {"trade_date": "20261016"}),
    "basic": ("stocktoday_data", {"api_name": "daily_basic", "max_rows": 1, "params": {"trade_date": "20261015", "ts_code": "600825.SH"}}),
    "suspension-diagnostic": ("stocktoday_data", {"api_name": "suspend_d", "params": {"trade_date": "20261015", "ts_code": "600825.SH"}}),
    "kline": ("stock_kline", {"code": "601811", "count": 60, "period": "daily"}),
    "kline-600825": ("stock_kline", {"code": "600825", "count": 60, "period": "daily"}),
    "flow": ("industry_flow", {"limit": 10, "trade_date": "20261015"}),
    "flow-live": ("industry_flow", {"limit": 10, "use_case": "realtime_poll"}),
    "filings": ("filings", {"code": "600825", "days": 7}),
}

def main():
    label = sys.argv[1]
    case = label.removesuffix("-fixed")
    intent, params = CASES[case]
    output = Path(__file__).with_name(f"online-{label}.json")
    if output.exists():
        raise SystemExit("Refusing duplicate query: evidence already exists")
    record = {"argv": sys.argv, "intent": intent, "params": params,
              "started_at": datetime.now(timezone.utc).isoformat(), "code_phase": "fixed" if label.endswith("-fixed") else "baseline"}
    trace = []
    real_request = requests.sessions.Session.request
    real_urlopen = urllib.request.urlopen

    def observed_request(session, method, url, **kwargs):
        tick = time.monotonic()
        body = kwargs.get("json") or {}
        row = {"method": method, "api_name": body.get("api_name") or "public_page"}
        row["params"] = {k: v for k, v in (body.get("params") or {}).items() if k in {"ts_code", "trade_date", "limit_type", "start_date", "end_date"}}
        try:
            response = real_request(session, method, url, **kwargs)
            row["http_status"] = response.status_code
            if body.get("api_name"):
                try:
                    data = response.json()
                    row["upstream_code"] = data.get("code") if isinstance(data, dict) else None
                    table = data.get("data") if isinstance(data, dict) else None
                    if isinstance(table, dict):
                        row["fields"] = table.get("fields")
                        row["rows"] = len(table.get("items") or [])
                except (ValueError, TypeError):
                    row["json_invalid"] = True
            return response
        except Exception as exc:
            row["exception_type"] = type(exc).__name__
            raise
        finally:
            row["elapsed_ms"] = int((time.monotonic() - tick) * 1000)
            trace.append(row)

    def observed_urlopen(*args, **kwargs):
        tick = time.monotonic()
        row = {"method": "GET", "api_name": "public_quote_or_minutes"}
        try:
            return real_urlopen(*args, **kwargs)
        except Exception as exc:
            row["exception_type"] = type(exc).__name__
            raise
        finally:
            row["elapsed_ms"] = int((time.monotonic() - tick) * 1000)
            trace.append(row)

    started = time.monotonic()
    try:
        with patch.object(requests.sessions.Session, "request", observed_request), patch.object(urllib.request, "urlopen", observed_urlopen):
            record["result"] = query(intent, **params)
    except Exception as exc:
        record["exception_type"] = type(exc).__name__
    record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    record["transport_trace"] = trace
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    meta = record.get("result", {}).get("_meta", {})
    print(json.dumps({"label": label, "elapsed_ms": record["elapsed_ms"], "status": meta.get("status"),
                      "attempts": meta.get("attempts"), "exception_type": record.get("exception_type")}, ensure_ascii=False))

if __name__ == "__main__":
    main()
