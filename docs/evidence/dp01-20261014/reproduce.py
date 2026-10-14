"""Bounded public read queries only; no private provider imports or secrets."""
import json
import pathlib
import sys
import time
from datetime import datetime, timezone
from unittest.mock import patch
import requests

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from ym_stock_data import query

CASES = {
    "D1-suffix": ("sector_index", {"codes": ["881121.TI", "881270.TI"]}),
    "D1-bare": ("sector_index", {"codes": ["881121", "881270"]}),
    "D2": ("market_board_strength", {"trade_date": "20261014", "board_ids": ["881270.TI"]}),
    "D5-intraday-1": ("market_intraday_state", {}),
    "D5-intraday-2": ("market_intraday_state", {}),
    "D5-limit": ("market_limit_state", {"date": "20261014"}),
    "D6": ("board_members", {"codes": ["881270.TI"], "trade_date": "20261014"}),
    "D7": ("stock_kline", {"code": "301041", "count": 60, "adjustment": "qfq"}),
    "D8-1": ("stocktoday_data", {"api_name": "ths_daily", "params": {"ts_code": "881270.TI", "start_date": "20261009", "end_date": "20261014"}}),
    "D8-2": ("stocktoday_data", {"api_name": "ths_daily", "params": {"ts_code": "881270.TI", "start_date": "20261009", "end_date": "20261014"}}),
    "D10": ("realtime_market", {}),
    "D4-snapshot": ("stock_snapshot", {"codes": ["301041"]}),
    "D4-facts": ("market_facts", {"trade_date": "20261014"}),
    "D4-event": ("stock_event", {"code": "301041", "event": "lockup"}),
    "D1-fixed": ("sector_index", {"codes": ["881121.TI", "881270.TI"]}),
    "D2-fixed": ("market_board_strength", {"trade_date": "20261014", "board_ids": ["881270.TI"]}),
    "D5-intraday-trace": ("market_intraday_state", {}),
    "D5-limit-trace": ("market_limit_state", {"date": "20261014"}),
    "D10-fixed": ("realtime_market", {}),
}

label = sys.argv[1]
intent, params = CASES[label]
output = pathlib.Path(__file__).parent / f"online-{label}.json"
if output.exists():
    raise SystemExit("Refusing to overwrite a recorded query")
started = time.monotonic()
record = {"command": f"python docs/evidence/dp01-20261014/reproduce.py {label}",
          "intent": intent, "params": params,
          "started_at": datetime.now(timezone.utc).isoformat()}
trace = []
real_post, real_get = requests.post, requests.get
def observed_post(url, **kwargs):
    tick = time.monotonic()
    name = kwargs.get("json", {}).get("api_name")
    try:
        response = real_post(url, **kwargs)
        trace.append({"api_name": name, "http_status": response.status_code,
                      "elapsed_ms": int((time.monotonic() - tick) * 1000)})
        return response
    except Exception:
        trace.append({"api_name": name, "elapsed_ms": int((time.monotonic() - tick) * 1000), "failed": True})
        raise
def observed_get(url, **kwargs):
    response = real_get(url, **kwargs)
    if label == "D1-fixed" and url.startswith("http://q.10jqka.com.cn/thshy/"):
        response.encoding = "gbk"
        page = len(list(pathlib.Path(__file__).parent.glob("ths-page-*.html"))) + 1
        (pathlib.Path(__file__).parent / f"ths-page-{page}.html").write_text(response.text)
    return response
try:
    with patch.object(requests, "post", observed_post), patch.object(requests, "get", observed_get):
        record["result"] = query(intent, **params)
except Exception as exc:
    # Never print provider exception messages (could contain auth details).
    record["exception_type"] = type(exc).__name__
record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
record["transport_timing"] = trace
output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
meta = record.get("result", {}).get("_meta", {})
print(json.dumps({"label": label, "elapsed_ms": record["elapsed_ms"],
                  "status": meta.get("status"), "attempts": meta.get("attempts"),
                  "exception_type": record.get("exception_type")}, ensure_ascii=False))
