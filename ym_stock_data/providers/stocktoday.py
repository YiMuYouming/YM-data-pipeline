"""Bounded read-only adapter matching StockToday's native Tushare SDK protocol.

Tushare 1.4.29 posts to gateway/api_name, not to the bare gateway root.
Only the vendor's documented HTTPS host is used; no website preview fallback.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import requests

from ..contracts import TZ_SHANGHAI
from ..trading_calendar import TradeCalendarUnavailable, market_fact_age_seconds
from .base import ProviderOutcome
from .stocktoday_auth import load_token
from .stocktoday_catalog import API_PARAMS
from .stocktoday_inventory import load_inventory

ENDPOINT = "https://tushare.citydata.club"
DEFAULT_BUDGET_PATH = Path.home() / ".cache" / "ym-stock-data" / "stocktoday-budget.sqlite3"
MAX_ROWS = 10000
_INDEX_CODES = (
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证指数"),
    ("399006.SZ", "创业指数"),
)
_A_SHARE_QUOTE_APIS = frozenset({
    "rt_k", "rt_tick", "rt_idx_k", "rt_idx_tick",
    "rt_etf_k", "rt_etf_tick", "rt_sw_k", "rt_sw_tick",
})


# The frozen vendor examples are part of the accepted public contract even
# where the provider catalogue's parameter list is narrower.  Keeping this
# union local to the adapter fixes example/whitelist drift without changing
# the immutable inventory hash used by Task 1.
EXAMPLE_PARAMS = {
    item["name"]: frozenset(
        key for key in item.get("example", {}) if key != "fields"
    )
    for item in load_inventory()["methods"]
}


class RequestBudget:
    """Shared local ceiling, not a claim about the vendor's purchased quota."""

    def __init__(self, path=DEFAULT_BUDGET_PATH, *, per_minute=None, per_day=None):
        self.path = Path(path)
        self.per_minute = self._limit_from_env(
            "YM_STOCKTODAY_PER_MINUTE", 0
        ) if per_minute is None else per_minute
        self.per_day = self._limit_from_env(
            "YM_STOCKTODAY_PER_DAY", 0
        ) if per_day is None else per_day
        if not isinstance(self.per_minute, int) or self.per_minute < 0:
            raise ValueError("per_minute must be a non-negative integer")
        if not isinstance(self.per_day, int) or self.per_day < 0:
            raise ValueError("per_day must be a non-negative integer")

    @staticmethod
    def _limit_from_env(name, default):
        raw = os.getenv(name)
        if raw is None:
            raw = os.getenv(name.replace("YM_", "YIMU_", 1))
        if raw is None or not raw.strip():
            return default
        try:
            value = int(raw)
        except ValueError:
            return default
        return value if value >= 0 else default

    def acquire(self, *, now=None):
        now = time.time() if now is None else now
        day_start = math.floor((now + 28800) / 86400) * 86400 - 28800
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=1)) as con, con:
            con.execute("CREATE TABLE IF NOT EXISTS requests (at REAL NOT NULL)")
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM requests WHERE at < ?", (min(day_start, now - 60),))
            minute, day = con.execute(
                "SELECT COALESCE(SUM(at > ?),0), COALESCE(SUM(at >= ?),0) FROM requests",
                (now - 60, day_start),
            ).fetchone()
            if (self.per_minute and minute >= self.per_minute) or (
                self.per_day and day >= self.per_day
            ):
                return False
            con.execute("INSERT INTO requests VALUES (?)", (now,))
        return True

    def wait_seconds(self, *, now=None):
        """Return a bounded wait for the minute window, or ``None`` at day cap.

        The audit runner uses this only after an unsuccessful acquire.  It is
        intentionally read-only with respect to the request log so a caller
        can sleep and retry without creating a busy loop or consuming quota.
        """

        now = time.time() if now is None else now
        day_start = math.floor((now + 28800) / 86400) * 86400 - 28800
        try:
            with closing(sqlite3.connect(self.path, timeout=1)) as con:
                con.execute("CREATE TABLE IF NOT EXISTS requests (at REAL NOT NULL)")
                con.execute("DELETE FROM requests WHERE at < ?", (min(day_start, now - 60),))
                minute, day = con.execute(
                    "SELECT COALESCE(SUM(at > ?),0), COALESCE(SUM(at >= ?),0) FROM requests",
                    (now - 60, day_start),
                ).fetchone()
                if self.per_day and day >= self.per_day:
                    return None
                if not self.per_minute or minute < self.per_minute:
                    return 0.0
                oldest = con.execute(
                    "SELECT MIN(at) FROM requests WHERE at > ?", (now - 60,)
                ).fetchone()[0]
        except (OSError, sqlite3.Error):
            return None
        if oldest is None:
            return 1.0
        return max(1.0, float(oldest + 60 - now))


def validate_dataset(params: dict) -> None:
    name = params.get("api_name")
    if not isinstance(name, str) or name not in API_PARAMS:
        raise ValueError("unsupported StockToday read-only api_name")
    nested = params.get("params", {})
    if not isinstance(nested, dict):
        raise ValueError("StockToday params must be a mapping")
    allowed = API_PARAMS[name] | EXAMPLE_PARAMS.get(name, frozenset()) | {"limit", "offset"}
    if set(nested) - allowed:
        raise ValueError("unsupported StockToday parameter names")
    for key, value in nested.items():
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError("StockToday parameters must be scalar strings or numbers")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("StockToday parameters must be finite")
        if isinstance(value, str) and (len(value) > 4096 or any(ord(c) < 32 for c in value)):
            raise ValueError("StockToday parameter is too long or contains control characters")
    for key in ("max_rows",):
        value = params.get(key, MAX_ROWS)
        if type(value) is not int or not 1 <= value <= MAX_ROWS:
            raise ValueError("StockToday max_rows must be 1..10000")
    for key in ("limit", "offset"):
        if key in nested and (type(nested[key]) is not int or not 0 <= nested[key] <= (MAX_ROWS if key == "limit" else 1000000)):
            raise ValueError("StockToday pagination is out of range")
    fields = params.get("fields", [])
    if isinstance(fields, str):
        fields = fields.split(",") if fields else []
    if not isinstance(fields, (list, tuple)) or not all(isinstance(f, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", f.strip()) for f in fields):
        raise ValueError("StockToday fields must be column names")
    if "freq" in nested and name in {"rt_min", "rt_idx_min", "rt_etf_min", "rt_fut_min"}:
        if nested["freq"] not in {"1MIN", "5MIN", "15MIN", "30MIN", "60MIN"}:
            raise ValueError("StockToday realtime freq must use uppercase MIN")
    if "freq" in nested and name in {"stk_mins", "idx_mins", "etf_mins"}:
        if nested["freq"] not in {"1min", "5min", "15min", "30min", "60min"}:
            raise ValueError("StockToday historical freq must use lowercase min")


def _stock_code(code: str) -> str:
    value = str(code).upper()
    raw = value.split(".")[0]
    suffix = "BJ" if raw.startswith(("920", "8", "4")) else "SH" if raw.startswith("6") else "SZ"
    if not re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", value) or ("." in value and value != f"{raw}.{suffix}"):
        raise ValueError("StockToday requires a consistent A-share stock code")
    return f"{raw}.{suffix}"


def validate_source(intent: str, params: dict) -> None:
    if params.get("source") != "stocktoday":
        raise ValueError("unsupported explicit source")
    if intent == "stock_snapshot":
        codes = params.get("codes", [])
        if isinstance(codes, str):
            codes = [codes]
        if not codes or len(codes) > 100:
            raise ValueError("StockToday snapshot requires 1..100 stocks")
        for code in codes:
            _stock_code(code)
    else:
        _stock_code(params.get("code", ""))
        count = params.get("count", 60)
        if type(count) is not int or not 1 <= count <= 1000:
            raise ValueError("StockToday kline count must be 1..1000")


def _timestamp(value):
    if not isinstance(value, str) or len(value) < 19:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=TZ_SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(TZ_SHANGHAI)
    except ValueError:
        return None


def _number(value, default=None):
    try:
        if value in (None, "", "-"):
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _minute_window_value(value, *, end=False):
    """Translate the public date-only contract to StockToday's minute shape."""

    text = str(value or "")
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return f"{text} {'15:10:00' if end else '09:00:00'}"
    return value


def _compact_trade_date(value):
    text = str(value or "")[:10]
    if re.fullmatch(r"\d{8}", text):
        return text
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text.replace("-", "")
    return None


def _format_amount(value):
    amount = _number(value, 0.0)
    if not amount:
        return "—"
    yi = amount / 1e8
    return f"{yi:.2f}亿" if yi < 10000 else f"{yi / 10000:.2f}万亿"


def _observation(rows, api_name, params, *, now):
    result = {"endpoint": f"{ENDPOINT}/{api_name}", "api_name": api_name, "status": "historical_or_reference", "returned_count": len(rows)}
    violations = []
    for param, field in (("trade_date", "trade_date"), ("period", "end_date"), ("m", "month")):
        if params.get(param) and any(str(r[field]) != str(params[param]) for r in rows if field in r):
            violations.append(param)
    for param, field, below in (("start_m", "month", True), ("end_m", "month", False)):
        if params.get(param) and any((str(r[field]) < str(params[param]) if below else str(r[field]) > str(params[param])) for r in rows if field in r):
            violations.append(param)
    result["filter_violations"] = violations
    dates = [str(r["trade_date"]) for r in rows if r.get("trade_date")]
    if dates:
        result["latest_trade_date"] = max(dates)
    if api_name.startswith("rt_") or api_name.startswith("realtime_"):
        if api_name in {"rt_k", "rt_idx_k", "rt_etf_k", "rt_hk_k"}:
            code_counts = Counter(r.get("ts_code") for r in rows if r.get("ts_code"))
            result["unique_code_count"] = len(code_counts)
            result["duplicate_code_count"] = sum(n - 1 for n in code_counts.values())
        result["timestamp_kind"] = "provider_updated_at_or_trade_time"
        requested = str(params.get("ts_code", ""))
        if requested and "*" not in requested:
            result["missing_codes"] = sorted(set(requested.split(",")) - {r.get("ts_code") for r in rows})
        times = [_timestamp(r.get("updated_at")) or _timestamp(r.get("trade_time")) for r in rows]
        valid = [t for t in times if t]
        result.update({"status": "unknown", "timestamp_coverage": len(valid) / len(rows) if rows else 0})
        if valid:
            oldest, latest = min(valid), max(valid)
            age = int((now - oldest).total_seconds())
            allowance = int(str(params.get("freq", "0MIN")).replace("MIN", "")) * 60 + 120 if "freq" in params else 120
            a_share_realtime = api_name not in {"rt_hk_k", "rt_hk_tick", "rt_fut_min"}
            if api_name in _A_SHARE_QUOTE_APIS:
                allowance = 60
            if a_share_realtime:
                try:
                    session_age = market_fact_age_seconds(oldest, now)
                except TradeCalendarUnavailable:
                    session_age = None
                age = int(session_age) if session_age is not None else age
            result.update({"oldest_at": oldest.isoformat(), "latest_at": latest.isoformat(), "age_sec": age, "max_age_sec": allowance})
            if len(valid) == len(rows):
                if a_share_realtime:
                    result["status"] = "fresh" if session_age is not None and session_age <= allowance else "stale"
                elif age >= -60:
                    result["status"] = "fresh" if age <= allowance else "stale"
    return result


def _rows(body):
    data = body.get("data")
    if isinstance(data, dict):
        fields, values = data.get("fields"), data.get("items")
        if not isinstance(fields, list) or not isinstance(values, list) or len(set(fields)) != len(fields):
            raise ValueError("invalid table")
        if all(isinstance(v, dict) and set(fields).issubset(v) for v in values):
            rows = [{field: v[field] for field in fields} for v in values]
        elif all(isinstance(v, list) and len(v) == len(fields) for v in values):
            rows = [dict(zip(fields, v)) for v in values]
        else:
            raise ValueError("invalid row width")
    elif isinstance(data, list) and all(isinstance(v, dict) for v in data):
        rows = data
        fields = body.get("columns", sorted({k for row in rows for k in row}))
    else:
        raise ValueError("invalid table")
    if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
        raise ValueError("invalid fields")
    return fields, rows


class StockTodayProvider:
    name = "stocktoday"

    def __init__(
        self,
        *,
        token_loader=load_token,
        post=None,
        budget_path=DEFAULT_BUDGET_PATH,
        budget=None,
        clock=None,
    ):
        self.token_loader = token_loader
        self.post = post or requests.post
        self.budget = budget or RequestBudget(budget_path)
        self.clock = clock or time

    def probe(self):
        try:
            present = bool(self.token_loader())
        except Exception:
            return {"provider": self.name, "status": "unavailable", "auth": {"required": True, "status": "error"}}
        return {"provider": self.name, "status": "configured_unverified" if present else "auth_missing", "auth": {"required": True, "status": "present" if present else "missing"}}

    def _provenance(self, *, http_status=0, upstream_code=0, total_present=False):
        return {
            "http_status": http_status if type(http_status) is int else 0,
            "upstream_code": upstream_code if type(upstream_code) is int else 0,
            "total_present": bool(total_present),
        }

    def _fail(
        self,
        started,
        code,
        status="provider_error",
        auth="unverified",
        *,
        http_status=0,
        upstream_code=0,
        total_present=False,
    ):
        return ProviderOutcome(
            self.name,
            status,
            error_code=code,
            latency_ms=int((self.clock.monotonic() - started) * 1000),
            auth={"required": True, "status": auth},
            provenance=self._provenance(
                http_status=http_status,
                upstream_code=upstream_code,
                total_present=total_present,
            ),
        )

    @staticmethod
    def _numeric_code(value):
        return value if type(value) is int else 0

    def _request_table(self, name, nested, *, fields="", max_rows=MAX_ROWS, diagnostic=False):
        started = self.clock.monotonic()
        try:
            token = self.token_loader()
        except Exception:
            return self._fail(started, "AUTH_STORAGE_UNAVAILABLE", "auth_error", "error")
        if not token:
            return self._fail(started, "AUTH_MISSING", "auth_error", "missing")
        try:
            if not self.budget.acquire(now=self.clock.time()):
                return self._fail(started, "LOCAL_BUDGET_EXHAUSTED")
        except (OSError, sqlite3.Error):
            return self._fail(started, "LOCAL_BUDGET_UNAVAILABLE")
        try:
            wire_params = {**nested, "ts_type_name": f"{ENDPOINT}/"}
            response = self.post(
                f"{ENDPOINT}/{name}",
                json={"api_name": name, "token": token, "params": wire_params, "fields": fields},
                timeout=(4, 15),
                allow_redirects=False,
            )
            http_status = response.status_code if type(response.status_code) is int else 0
            if 300 <= http_status < 400:
                return self._fail(started, "REDIRECT_BLOCKED", http_status=http_status)

            # HTTP authentication/rate statuses are authoritative even when a
            # gateway returns HTML or plain text instead of JSON.  Parse JSON
            # opportunistically first only to preserve a numeric upstream code.
            body = None
            body_json_error = False
            try:
                body = response.json()
            except (ValueError, TypeError):
                body_json_error = True
            upstream_code = self._numeric_code(body.get("code")) if isinstance(body, dict) else 0
            if http_status == 429:
                return self._fail(
                    started,
                    "RATE_LIMITED",
                    http_status=http_status,
                    upstream_code=upstream_code,
                )
            if http_status in {401, 403}:
                return self._fail(
                    started,
                    "AUTH_DENIED",
                    "auth_error",
                    "error",
                    http_status=http_status,
                    upstream_code=upstream_code,
                )
            if http_status != 200:
                return self._fail(
                    started,
                    f"HTTP_{http_status}" if http_status else "INVALID_RESPONSE",
                    http_status=http_status,
                    upstream_code=upstream_code,
                )
            if body_json_error or not isinstance(body, dict):
                return self._fail(started, "INVALID_RESPONSE", http_status=http_status)
            if type(body.get("code")) is not int:
                return self._fail(started, "INVALID_RESPONSE", http_status=http_status)
            if upstream_code != 0:
                message = str(body.get("msg", ""))
                lowered = message.lower()
                if (
                    any(word in lowered for word in ("token", "权限", "积分", "认证"))
                    or upstream_code in {401, 403, 40203, -2001, -2002}
                ):
                    return self._fail(
                        started,
                        "AUTH_DENIED",
                        "auth_error",
                        "error",
                        http_status=http_status,
                        upstream_code=upstream_code,
                    )
                if any(word in message for word in ("限流", "超限", "频率")) or upstream_code in {429, -429}:
                    return self._fail(
                        started,
                        "RATE_LIMITED",
                        http_status=http_status,
                        upstream_code=upstream_code,
                    )
                return self._fail(
                    started,
                    "API_NOT_AVAILABLE" if "不存在" in message else "UPSTREAM_ERROR",
                    http_status=http_status,
                    upstream_code=upstream_code,
                )
            if token in json.dumps(body, ensure_ascii=False):
                return self._fail(
                    started,
                    "RESPONSE_CONTAINS_SECRET",
                    http_status=http_status,
                    upstream_code=upstream_code,
                )
            total_present = "total" in body
            if diagnostic and not isinstance(body.get("data"), (dict, list)):
                return self._fail(
                    started,
                    "INVALID_RESPONSE",
                    http_status=http_status,
                    upstream_code=upstream_code,
                )
            try:
                columns, rows = _rows(body)
            except ValueError:
                if not diagnostic or not isinstance(body.get("data"), dict):
                    raise
                # token_info is a diagnostic only.  Preserve structure, not
                # returned values; the audit receipt sees one safe marker.
                columns, rows = ["account_status"], [{"account_status": "available"}]
            if diagnostic:
                # token_info is never a data route.  Do not carry token
                # metadata values into the provider result or audit receipt.
                columns, rows = ["account_status"], [{"account_status": "available"}]
            total = body.get("total") if total_present else None
            if total_present and (type(total) is not int or total < len(rows)):
                return self._fail(
                    started,
                    "INVALID_RESPONSE",
                    http_status=http_status,
                    upstream_code=upstream_code,
                    total_present=total_present,
                )
            truncated = bool(total is not None and total > len(rows)) or len(rows) > max_rows
            rows = rows[:max_rows]
            observation = _observation(
                rows, name, nested,
                now=datetime.fromtimestamp(self.clock.time(), TZ_SHANGHAI),
            )
            observation["truncated"] = truncated
            data = {
                "api_name": name,
                "items": rows,
                "fields": columns,
                "total": total,
                "total_present": total_present,
                "truncated": truncated,
                "_stocktoday": observation,
            }
        except (requests.Timeout, TimeoutError):
            return self._fail(started, "TIMEOUT", "timeout")
        except requests.RequestException:
            return self._fail(started, "NETWORK_ERROR", "network_error")
        except (ValueError, TypeError, KeyError, OverflowError):
            return self._fail(started, "INVALID_RESPONSE")
        return ProviderOutcome(
            self.name,
            "success" if rows else "empty",
            data=data,
            fetched_at=datetime.fromtimestamp(self.clock.time(), TZ_SHANGHAI).isoformat(timespec="seconds"),
            latency_ms=int((self.clock.monotonic() - started) * 1000),
            auth={"required": True, "status": "ok"},
            provenance=self._provenance(
                http_status=http_status,
                upstream_code=upstream_code,
                total_present=total_present,
            ),
        )

    def call_account_diagnostic(self):
        """Run the private token_info diagnostic through the same safe gateway."""

        return self._request_table("token_info", {}, diagnostic=True)

    # Descriptive alias for callers that do not use the provider protocol.
    diagnostic = call_account_diagnostic

    @staticmethod
    def _realtime_market_data(outcome):
        raw = outcome.data if isinstance(outcome.data, dict) else {}
        rows = raw.get("items")
        if not isinstance(rows, list):
            return None
        by_code = {
            str(row.get("ts_code")): row
            for row in rows
            if isinstance(row, dict) and row.get("ts_code")
        }
        if any(code not in by_code for code, _ in _INDEX_CODES):
            return None

        result = {}
        amount_total = 0.0
        for code, name in _INDEX_CODES:
            row = by_code[code]
            price = _number(row.get("close"), _number(row.get("price")))
            previous = _number(row.get("pre_close"), _number(row.get("last_close")))
            if price is None:
                return None
            pct = _number(
                row.get("pct_chg"),
                _number(row.get("pct_change"), _number(row.get("change_pct"))),
            )
            if pct is None and previous:
                pct = (price / previous - 1) * 100
            result[name] = int(price) if price.is_integer() else price
            result[f"{name}涨幅"] = f"{(pct or 0):+.2f}%"
            result[f"{name}成交额"] = _format_amount(row.get("amount"))
            high = _number(row.get("high"))
            low = _number(row.get("low"))
            if high is not None and low is not None and previous:
                result[f"{name}振幅"] = f"{(high - low) / previous * 100:.2f}%"
            if code in {"000001.SH", "399001.SZ"}:
                amount_total += _number(row.get("amount"), 0.0)
        if amount_total:
            result["成交额"] = _format_amount(amount_total)
        result["_stocktoday"] = dict(raw.get("_stocktoday") or {})
        return result

    def _call_realtime_market(self):
        requested = ",".join(code for code, _ in _INDEX_CODES)
        last = None
        for api_name in ("rt_idx_k", "rt_idx_tick"):
            outcome = self._request_table(api_name, {"ts_code": requested})
            last = outcome
            if outcome.status not in {"success", "empty"}:
                continue
            data = self._realtime_market_data(outcome)
            if data is not None:
                return ProviderOutcome(
                    self.name,
                    "success",
                    data=data,
                    fetched_at=outcome.fetched_at,
                    latency_ms=outcome.latency_ms,
                    auth=outcome.auth,
                    provenance=outcome.provenance,
                )
        if last is None:
            return self._fail(self.clock.monotonic(), "INVALID_RESPONSE")
        if last.status == "empty":
            return ProviderOutcome(
                self.name,
                "empty",
                data={"_stocktoday": dict((last.data or {}).get("_stocktoday") or {})}
                if isinstance(last.data, dict)
                else {"_stocktoday": {}},
                fetched_at=last.fetched_at,
                latency_ms=last.latency_ms,
                auth=last.auth,
                provenance=last.provenance,
            )
        return last

    @staticmethod
    def _limit_row(row):
        def number(*keys):
            for key in keys:
                value = _number(row.get(key))
                if value is not None:
                    return value
            return None

        return {
            "code": str(row.get("ts_code") or row.get("code") or "").split(".")[0],
            "name": str(row.get("name") or row.get("ts_name") or ""),
            "price": number("close", "price"),
            "pct": number("pct_chg", "pct_change", "change_pct"),
            "limit_days": int(number("limit_times", "open_times") or 1),
            "industry": str(row.get("industry") or row.get("tag") or ""),
        }

    def _fetch_limit_rows(self, *, date, limit_type):
        ths_type = "涨停池" if limit_type == "U" else "跌停池"
        candidates = (
            ("limit_list_d", {"limit_type": limit_type}),
            ("limit_list_ths", {"limit_type": ths_type}),
            ("limit_step", {}),
        )
        last = None
        for api_name, extra in candidates:
            nested = dict(extra)
            if date:
                nested["trade_date"] = date
            outcome = self._request_table(api_name, nested)
            last = outcome
            rows = outcome.data.get("items", []) if isinstance(outcome.data, dict) else []
            if outcome.status == "success" and isinstance(rows, list) and rows:
                return rows, outcome, api_name
            if outcome.status not in {"success", "empty"}:
                continue
        return [], last, None

    def _call_limit_state(self, params):
        date = params.get("date") or datetime.fromtimestamp(
            self.clock.time(), TZ_SHANGHAI
        ).strftime("%Y%m%d")
        limit_types = [params["limit_type"]] if params.get("limit_type") else ["U", "D"]
        pools = {"zt": [], "zb": [], "dt": [], "yzt": []}
        observations = []
        last = None
        for limit_type in limit_types:
            rows, outcome, api_name = self._fetch_limit_rows(
                date=date, limit_type=limit_type
            )
            last = outcome or last
            if outcome is not None and isinstance(outcome.data, dict):
                observations.append(dict(outcome.data.get("_stocktoday") or {}))
            if outcome is not None and outcome.status not in {"success", "empty"}:
                if not any(pools.values()):
                    return outcome
                continue
            normalized = [self._limit_row(row) for row in rows]
            pools["zt" if limit_type == "U" else "dt"] = normalized
            if api_name and observations:
                observations[-1]["selected_api"] = api_name

        count = len(pools["zt"]) + len(pools["dt"])
        data = {
            "date": date,
            "zt_count": len(pools["zt"]),
            "zb_count": 0,
            "dt_count": len(pools["dt"]),
            "yzt_count": 0,
            "break_rate": 0.0,
            "max_board": max(
                (int(row.get("limit_days") or 1) for row in pools["zt"]),
                default=0,
            ),
            "pools": pools,
            "_stocktoday": {
                "api_name": "limit_list_d",
                "status": "empty" if not count else "historical_or_reference",
                "returned_count": count,
                "fallback_observations": observations,
            },
        }
        if count:
            return ProviderOutcome(
                self.name,
                "success",
                data=data,
                fetched_at=(last.fetched_at if last else None),
                latency_ms=(last.latency_ms if last else 0),
                auth=(last.auth if last else None),
                provenance=(last.provenance if last else None),
            )
        if last is not None and last.status not in {"empty", "success"}:
            return last
        return ProviderOutcome(
            self.name,
            "empty",
            data=data,
            fetched_at=(last.fetched_at if last else None),
            latency_ms=(last.latency_ms if last else 0),
            auth=(last.auth if last else None),
            provenance=(last.provenance if last else None),
        )

    def _call_limit_board(self, params):
        """Return one board kind; incompatible kinds are left to route fallback."""

        started = self.clock.monotonic()
        kind = params.get("kind")
        if kind == "yesterday":
            return self._fail(
                started,
                "SEMANTIC_UNSUPPORTED",
                "incompatible",
            )
        limit_type = {"up": "U", "down": "D", "broken": "Z"}.get(kind)
        if limit_type is None:
            return self._fail(started, "INVALID_PARAMS", "provider_error")
        nested = {"limit_type": limit_type}
        if params.get("date"):
            nested["trade_date"] = params["date"]
        outcome = self._request_table(
            "limit_list_d",
            nested,
            fields=(
                "ts_code,trade_date,industry,name,close,pct_chg,"
                "open_times,up_stat,limit_times"
            ),
        )
        if outcome.error_code:
            return outcome
        raw = outcome.data if isinstance(outcome.data, dict) else {}
        rows = [row for row in raw.get("items", []) if isinstance(row, dict)]
        requested_date = params.get("date")
        if requested_date:
            selected_date = requested_date
            rows = [
                row
                for row in rows
                if str(row.get("trade_date") or "") == requested_date
            ]
        else:
            available_dates = sorted(
                {
                    str(row.get("trade_date"))
                    for row in rows
                    if re.fullmatch(r"\d{8}", str(row.get("trade_date") or ""))
                }
            )
            if not available_dates:
                if rows:
                    return self._fail(
                        started,
                        "SEMANTIC_DATE_MISSING",
                        "provider_error",
                    )
                selected_date = None
            else:
                selected_date = available_dates[-1]
                rows = [
                    row
                    for row in rows
                    if str(row.get("trade_date") or "") == selected_date
                ]
        observation = dict(raw.get("_stocktoday") or {})
        if selected_date:
            observation["selected_trade_date"] = selected_date
        data = {
            "kind": kind,
            "date": selected_date,
            "items": [self._limit_row(row) for row in rows if isinstance(row, dict)],
            "_stocktoday": observation,
        }
        return ProviderOutcome(
            self.name,
            outcome.status,
            data=data,
            fetched_at=outcome.fetched_at,
            latency_ms=outcome.latency_ms,
            auth=outcome.auth,
            provenance=outcome.provenance,
        )

    def _call_hot_rank(self, params):
        """Fetch the full hot table, then select/project rows locally.

        The upstream accepts these hot methods without a ``fields`` subset;
        requesting the audited field subset is an intermittent upstream error.
        ``is_new=Y`` is likewise not part of this canonical path.
        """

        source = params.get("source")
        if source == "ths":
            api_name = "ths_hot"
            nested = {"market": "热股"}
        elif source == "dc":
            api_name = "dc_hot"
            nested = {"hot_type": "人气榜", "market": "A股市场"}
        else:
            return self._fail(
                self.clock.monotonic(),
                "INVALID_PARAMS",
                "provider_error",
            )
        if params.get("trade_date"):
            nested["trade_date"] = params["trade_date"]
        outcome = self._request_table(
            api_name,
            nested,
            max_rows=MAX_ROWS,
        )
        if outcome.error_code:
            return outcome
        raw = outcome.data if isinstance(outcome.data, dict) else {}
        rows = [row for row in raw.get("items", []) if isinstance(row, dict)]
        requested_date = params.get("trade_date")
        current_session = params.get("current_session") is True
        if current_session:
            current_date = datetime.fromtimestamp(
                self.clock.time(), TZ_SHANGHAI
            ).strftime("%Y%m%d")
            current_rows = [
                row
                for row in rows
                if _compact_trade_date(
                    row.get("trade_date") or row.get("date") or row.get("trade_time")
                )
                == current_date
            ]
            rows = current_rows
            selected_date = current_date if rows else None
        elif requested_date:
            rows = [
                row for row in rows if str(row.get("trade_date") or "") == requested_date
            ]
            selected_date = requested_date
        else:
            available_dates = sorted(
                {
                    str(row.get("trade_date"))
                    for row in rows
                    if re.fullmatch(r"\d{8}", str(row.get("trade_date") or ""))
                }
            )
            selected_date = available_dates[-1] if available_dates else None
            if selected_date:
                rows = [
                    row
                    for row in rows
                    if str(row.get("trade_date") or "") == selected_date
                ]
        if params.get("limit") is not None:
            rows = rows[: params["limit"]]
        observation = dict(raw.get("_stocktoday") or {})
        if current_session and not rows:
            observation.update(
                {
                    "current_session": True,
                    "current_session_date": datetime.fromtimestamp(
                        self.clock.time(), TZ_SHANGHAI
                    ).strftime("%Y%m%d"),
                    "current_session_rejected": True,
                }
            )
        if selected_date:
            observation["selected_trade_date"] = selected_date
        return ProviderOutcome(
            self.name,
            "success" if rows else "empty",
            data={
                "source": source,
                "trade_date": selected_date,
                "items": rows,
                "_stocktoday": observation,
            },
            fetched_at=outcome.fetched_at,
            latency_ms=outcome.latency_ms,
            auth=outcome.auth,
            provenance=outcome.provenance,
        )

    def _call_sector_index(self, params):
        codes = params.get("codes") or []
        names = params.get("names") or []
        nested = {}
        if codes:
            nested["ts_code"] = ",".join(str(code) for code in codes)
        if names:
            nested["name"] = ",".join(str(name) for name in names)
        outcome = self._request_table("ths_index", nested)
        if outcome.error_code:
            return outcome
        raw = outcome.data if isinstance(outcome.data, dict) else {}
        return ProviderOutcome(
            self.name,
            outcome.status,
            data={
                "items": raw.get("items", []),
                "missing": [],
                "_stocktoday": raw.get("_stocktoday", {}),
            },
            fetched_at=outcome.fetched_at,
            latency_ms=outcome.latency_ms,
            auth=outcome.auth,
            provenance=outcome.provenance,
        )

    def _call_flow(self, *, api_name, params):
        nested = {"trade_date": params.get("trade_date")}
        nested = {key: value for key, value in nested.items() if value}
        outcome = self._request_table(api_name, nested)
        if api_name == "moneyflow_mkt_dc" and outcome.error_code == "INVALID_RESPONSE":
            outcome = self._request_table(api_name, nested)
        if outcome.error_code:
            return outcome
        raw = outcome.data if isinstance(outcome.data, dict) else {}
        rows = [row for row in raw.get("items", []) if isinstance(row, dict)]
        current_session = params.get("current_session") is True
        selected_date = params.get("trade_date")
        if current_session:
            current_date = datetime.fromtimestamp(
                self.clock.time(), TZ_SHANGHAI
            ).strftime("%Y%m%d")
            rows = [
                row
                for row in rows
                if _compact_trade_date(
                    row.get("trade_date") or row.get("date") or row.get("trade_time")
                )
                == current_date
            ]
            selected_date = current_date if rows else None
        if params.get("limit") is not None:
            rows = rows[: params["limit"]]
        observation = dict(raw.get("_stocktoday") or {})
        if current_session and not rows:
            observation.update(
                {
                    "current_session": True,
                    "current_session_date": datetime.fromtimestamp(
                        self.clock.time(), TZ_SHANGHAI
                    ).strftime("%Y%m%d"),
                    "current_session_rejected": True,
                }
            )
        return ProviderOutcome(
            self.name,
            "success" if rows else "empty",
            data={
                "trade_date": selected_date,
                "items": rows,
                "api_name": api_name,
                "_stocktoday": observation,
            },
            fetched_at=outcome.fetched_at,
            latency_ms=outcome.latency_ms,
            auth=outcome.auth,
            provenance=outcome.provenance,
        )

    @staticmethod
    def _index_code(value):
        text = str(value or "").upper()
        if not re.fullmatch(r"\d{6}\.(?:SH|SZ)", text):
            raise ValueError("StockToday requires an exchange-qualified index code")
        return text

    def _call_index_kline_one(self, params, index_code):
        period = params.get("period", "daily")
        name = {
            "daily": "index_daily",
            "weekly": "index_weekly",
            "monthly": "index_monthly",
        }.get(period, "idx_mins")
        now = datetime.fromtimestamp(self.clock.time(), TZ_SHANGHAI)
        requested_date = params.get("trade_date")
        start_date = params.get("start_date") or requested_date or (
            now - timedelta(days=params.get("count", 60) * 3 + 30)
        ).strftime("%Y%m%d")
        end_date = params.get("end_date") or requested_date or now.strftime("%Y%m%d")
        nested = {
            "ts_code": self._index_code(index_code),
            "start_date": start_date,
            "end_date": end_date,
        }
        if name == "idx_mins":
            nested["freq"] = period.replace("m", "min")
            nested["start_date"] = _minute_window_value(start_date)
            nested["end_date"] = _minute_window_value(end_date, end=True)
        outcome = self._request_table(name, nested)
        if outcome.error_code:
            return outcome
        raw = outcome.data if isinstance(outcome.data, dict) else {}
        rows = raw.get("items", [])
        bars = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("ts_code") not in {nested["ts_code"], None}:
                continue
            stamp = str(row.get("trade_date") or row.get("trade_time") or "")
            if len(stamp) == 8 and stamp.isdigit():
                stamp = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
            if not stamp:
                continue
            values = {key: _number(row.get(key)) for key in ("open", "high", "low", "close")}
            if any(value is None for value in values.values()):
                continue
            bars.append(
                {
                    "datetime": stamp,
                    **values,
                    "volume": _number(row.get("vol"), 0.0) * (1 if name == "idx_mins" else 100),
                    "amount": _number(row.get("amount"), 0.0) * (1 if name == "idx_mins" else 1000),
                }
            )
        bars = sorted(bars, key=lambda bar: bar["datetime"])
        if params.get("count") is not None:
            bars = bars[-params["count"] :]
        return ProviderOutcome(
            self.name,
            "success" if bars else "empty",
            data={
                "index_code": index_code,
                "period": period,
                "bars": bars,
                "adjustment": "none",
                "volume_unit": "share",
                "amount_unit": "CNY",
                "_stocktoday": raw.get("_stocktoday", {}),
            },
            fetched_at=outcome.fetched_at,
            latency_ms=outcome.latency_ms,
            auth=outcome.auth,
            provenance=outcome.provenance,
        )

    def _call_index_kline(self, params):
        codes = params.get("codes") or [params.get("index_code")]
        outcomes = []
        items = []
        for code in codes:
            outcome = self._call_index_kline_one(params, code)
            outcomes.append(outcome)
            if outcome.status == "success" and isinstance(outcome.data, dict):
                items.append(dict(outcome.data))
        if len(codes) == 1:
            return outcomes[0]
        if not items:
            return outcomes[-1]
        by_code = {
            item["index_code"]: {"rows": item.get("bars", [])}
            for item in items
        }
        for item in items:
            by_code[item["index_code"].split(".")[0]] = by_code[item["index_code"]]
        return ProviderOutcome(
            self.name,
            "success",
            data={
                "items": items,
                "by_code": by_code,
                **{key.split(".")[0]: value for key, value in by_code.items() if "." in key},
                "_stocktoday": {
                    "api_name": "index_daily",
                    "returned_count": len(items),
                },
            },
            fetched_at=next((item.fetched_at for item in outcomes if item.fetched_at), None),
            latency_ms=sum(item.latency_ms for item in outcomes),
            auth=next((item.auth for item in outcomes if item.auth), None),
            provenance=next((item.provenance for item in outcomes if item.provenance), None),
        )

    def _call_index_intraday_compare(self, params):
        result = self._call_index_kline(
            {
                "codes": [code for code, _ in _INDEX_CODES],
                "period": params.get("period", "15m"),
                "trade_date": params.get("trade_date"),
            }
        )
        return result

    def call(self, intent, params):
        started = self.clock.monotonic()
        if intent == "stocktoday_account_diagnostic":
            return self.call_account_diagnostic()

        def fail(code, status="provider_error", auth="unverified"):
            return self._fail(started, code, status, auth)

        if intent == "realtime_market":
            return self._call_realtime_market()
        if intent == "market_limit_state":
            return self._call_limit_state(params)
        if intent == "market_limit_board":
            return self._call_limit_board(params)
        if intent == "market_hot_rank":
            return self._call_hot_rank(params)
        if intent == "sector_index":
            return self._call_sector_index(params)
        if intent == "industry_flow":
            return self._call_flow(api_name="moneyflow_ind_ths", params=params)
        if intent == "fund_flow":
            return self._call_flow(api_name="moneyflow_mkt_dc", params=params)
        if intent == "northbound_flow":
            return self._call_flow(api_name="moneyflow_hsgt", params=params)
        if intent == "legacy_hot_rank":
            return self._call_hot_rank({"source": "ths", **params})
        if intent == "index_kline":
            return self._call_index_kline(params)
        if intent == "index_intraday_compare":
            return self._call_index_intraday_compare(params)
        if intent not in {"stocktoday_data", "stock_snapshot", "stock_kline"}:
            return fail("INCOMPATIBLE_INTENT", "incompatible")
        now = datetime.fromtimestamp(self.clock.time(), TZ_SHANGHAI)
        if intent == "stocktoday_data":
            validate_dataset(params)
            name, nested = params["api_name"], dict(params.get("params", {}))
            fields = params.get("fields", "")
            return self._request_table(
                name,
                nested,
                fields=",".join(fields) if isinstance(fields, (list, tuple)) else fields,
                max_rows=params.get("max_rows", MAX_ROWS),
            )
        if intent == "stock_snapshot":
            validate_source(intent, params)
            name, nested = "rt_k", {"ts_code": ",".join(_stock_code(c) for c in params["codes"])}
        else:
            validate_source(intent, params)
            period, count = params.get("period", "daily"), params.get("count", 60)
            adjustment = params.get("adjustment", "none")
            if adjustment == "qfq":
                name = "pro_bar"
            else:
                name = period if period in {"daily", "weekly", "monthly"} else "stk_mins"
            days = min(36500, count * {"daily": 3, "weekly": 10, "monthly": 40}.get(period, 2) + 30)
            nested = {
                "ts_code": _stock_code(params["code"]),
                "start_date": params.get("start_date") or (now - timedelta(days=days)).strftime("%Y%m%d"),
                "end_date": params.get("end_date") or now.strftime("%Y%m%d"),
            }
            if name == "pro_bar":
                nested["freq"] = {"daily": "D", "weekly": "W", "monthly": "M"}[period]
                nested["adj"] = "qfq"
            elif name == "stk_mins":
                start_date = params.get("start_date")
                end_date = params.get("end_date")
                nested.update(
                    {
                        "freq": period.replace("m", "min"),
                        "start_date": _minute_window_value(
                            start_date
                        ) or (now - timedelta(days=min(365, count + 10))).strftime("%Y-%m-%d 09:00:00"),
                        "end_date": _minute_window_value(
                            end_date, end=True
                        ) or now.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
        outcome = self._request_table(name, nested)
        if name == "pro_bar" and outcome.error_code == "INVALID_RESPONSE":
            outcome = self._request_table(name, nested)
        if outcome.error_code or intent == "stocktoday_data":
            return outcome
        data = outcome.data if isinstance(outcome.data, dict) else {}
        rows = data.get("items", [])
        observation = data.get("_stocktoday", {})
        if intent == "stock_snapshot":
            ordered = sorted(
                rows,
                key=lambda r: _timestamp(r.get("updated_at"))
                or _timestamp(r.get("trade_time"))
                or datetime.min.replace(tzinfo=TZ_SHANGHAI),
            )
            by_code = {r.get("ts_code"): r for r in ordered}
            snapshot = {"_stocktoday": observation}
            for code in params["codes"]:
                row = by_code.get(_stock_code(code))
                if not row or type(row.get("close")) not in (float, int) or not math.isfinite(row["close"]) or row["close"] <= 0:
                    continue
                price, previous = row["close"], row.get("pre_close")
                timestamp = _timestamp(row.get("updated_at")) or _timestamp(row.get("trade_time"))
                snapshot[code] = {
                    "name": row.get("name"),
                    "price": price,
                    "last_close": previous,
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "change_pct": (price / previous - 1) * 100 if previous else None,
                    "amount": row.get("amount"),
                    "amount_wan": row["amount"] / 10000 if row.get("amount") is not None else None,
                    "volume": row.get("vol"),
                    "quote_time": timestamp.isoformat() if timestamp else None,
                    "source": self.name,
                }
            if outcome.status == "success" and rows and not any(
                code in snapshot for code in params["codes"]
            ):
                return self._fail(started, "INVALID_SNAPSHOT")
            return ProviderOutcome(
                self.name,
                outcome.status,
                data=snapshot,
                fetched_at=outcome.fetched_at,
                latency_ms=outcome.latency_ms,
                auth=outcome.auth,
                provenance=outcome.provenance,
            )
        bars = []
        for row in rows:
            if row.get("ts_code") != nested["ts_code"]:
                return self._fail(started, "SYMBOL_MISMATCH")
            stamp = str(row.get("trade_date") or row.get("trade_time") or "")
            if len(stamp) == 8 and stamp.isdigit():
                stamp = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
            if not stamp or any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k]) for k in ("open", "high", "low", "close")):
                return self._fail(started, "INVALID_KLINE")
            bars.append(
                {
                    "datetime": stamp,
                    **{k: row[k] for k in ("open", "high", "low", "close")},
                    "volume": row["vol"] * (1 if name == "stk_mins" else 100) if row.get("vol") is not None else None,
                    "amount": row["amount"] * (1 if name == "stk_mins" else 1000) if row.get("amount") is not None else None,
                }
            )
        bars = sorted(bars, key=lambda b: b["datetime"])
        if name == "stk_mins":
            requested_count = params.get("count")
            if requested_count is not None:
                bars = bars[-requested_count:]
        else:
            bars = bars[-params.get("count", 60) :]
        if name == "stk_mins":
            observation.update({"status": "unverified_bar_time", "bar_time_convention": "unverified"})
            for bar in bars:
                bar["bar_closed"] = None
        return ProviderOutcome(
            self.name,
            outcome.status,
            data={
                "code": params["code"],
                "period": params.get("period", "daily"),
                "bars": bars,
                "adjustment": adjustment,
                "volume_unit": "share",
                "amount_unit": "CNY",
                "_stocktoday": observation,
            },
            fetched_at=outcome.fetched_at,
            latency_ms=outcome.latency_ms,
            auth=outcome.auth,
            provenance=outcome.provenance,
        )
