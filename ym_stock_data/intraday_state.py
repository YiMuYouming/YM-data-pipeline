"""Intraday market state from one StockToday full-market snapshot.

Breadth, emotion, limit up/down, broken boards and the consecutive ladder are
derived here and computed by ``indicators`` — the same functions market_facts
uses after the close.  There is no fallback provider: when StockToday is
unavailable the caller keeps its last good value and its time.
"""

from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime
from pathlib import Path

from . import indicators
from .contracts import TZ_SHANGHAI
from .trading_calendar import (
    TradeCalendarUnavailable,
    is_trading_day,
    market_fact_age_seconds,
    previous_trading_day,
)

# 4 exchange wildcards cover every StockToday A-share row (003xxx is a vendor
# gap that no wildcard or code list returns; see REPAIR_PLAN §2).
SNAPSHOT_BATCHES = ("6*.SH", "0*.SZ", "3*.SZ", "*.BJ")
# 2026-10-26（R3/7b）：四个 rt_k 批次彼此独立，串行抓取是盘中耗时 8s+ 的主因
# （真实回放 5 次：min 8.53s / median 8.87s / max 13.13s）。并发只改"什么时候发
# 请求"，不改任何判定：结果仍按 SNAPSHOT_BATCHES 的固定顺序落进诊断与 attempts，
# 第一个失败批次抛出的错误码也与串行时一致。
SNAPSHOT_MAX_WORKERS = len(SNAPSHOT_BATCHES)
B_SHARE_PREFIXES = ("900", "200", "201")
NORMAL_MAX_AGE_SEC = 180
EXPIRED_AGE_SEC = 600
COVERAGE_NORMAL = 98.0
COVERAGE_MINIMUM = 90.0
# rt_k never returns the 003000-003043 block (vendor gap, 42 codes on
# 2026-09-27).  A gap this small is filled per code from Tencent each round;
# a larger one means StockToday itself is failing and is not patched.
STRUCTURAL_FILL_LIMIT = 200
STK_LIMIT_CACHE = Path.home() / ".cache" / "ym-stock-data" / "stk_limit"
BUCKETS = ("涨停", ">7%", "5~7%", "3~5%", "0~3%", "平盘",
           "-0~-3%", "-3~-5%", "-5~-7%", "<-7%", "跌停")


class IntradayStateError(RuntimeError):
    def __init__(self, code: str, status: str = "provider_error", last=None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.last = last


def _timestamp(value) -> datetime | None:
    if not isinstance(value, str) or len(value) < 19:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=TZ_SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(TZ_SHANGHAI)


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def session_data_time(stamp: datetime) -> tuple[datetime, bool]:
    """Map a vendor stamp to the session it belongs to.

    StockToday re-stamps the last session with the calendar date on holidays
    (2026-09-25 rows carried 09-24 prices) and before the open.  A stamp on a
    non-trading day, or before 09:15 on a trading day, belongs to the previous
    completed session's close.
    """

    day = stamp.date()
    if is_trading_day(day) and stamp.time() >= dtime(9, 15):
        return stamp, False
    return datetime.combine(previous_trading_day(day), dtime(15, 0), TZ_SHANGHAI), True


def freshness_tier(age_seconds: int | None) -> str:
    if age_seconds is None:
        return "unknown"
    if age_seconds <= NORMAL_MAX_AGE_SEC:
        return "normal"
    if age_seconds <= EXPIRED_AGE_SEC:
        return "aging"
    return "expired"


def dedupe_latest(rows: list[dict]) -> dict[str, dict]:
    """rt_k repeats some codes (科创板 twice); keep each code's latest row."""

    latest: dict[str, dict] = {}
    for row in rows:
        code = row.get("ts_code")
        if not code:
            continue
        stamp = _timestamp(row.get("updated_at"))
        current = latest.get(code)
        if current is None or (stamp and (_timestamp(current.get("updated_at")) or stamp) <= stamp):
            latest[code] = row
    return latest


def _stk_limit(provider, trade_date: str) -> dict[str, dict]:
    cache = STK_LIMIT_CACHE / f"{trade_date}.json"
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if isinstance(cached, dict) and cached:
            return cached
    except (OSError, ValueError):
        pass
    outcome = provider._request_table("stk_limit", {"trade_date": trade_date})
    items = (outcome.data or {}).get("items") if isinstance(outcome.data, dict) else None
    if outcome.status != "success" or not isinstance(items, list):
        return {}
    limits = {
        str(row.get("ts_code")): {"up": _number(row.get("up_limit")), "down": _number(row.get("down_limit"))}
        for row in items
        if isinstance(row, dict) and row.get("ts_code") and str(row.get("trade_date")) == trade_date
    }
    if limits:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(limits, ensure_ascii=False), encoding="utf-8")
            tmp.replace(cache)
        except OSError:
            pass
    return limits


def _limit_metadata(provider, trade_date: str, *, diagnostics: dict | None = None):
    """Dated metadata only; snapshot membership and counts stay authoritative."""
    try:
        outcome = provider._request_table("limit_list_d", {"trade_date": trade_date, "limit_type": "U"})
    except Exception:
        if diagnostics is not None:
            diagnostics.update(status="provider_error", error_code="METADATA_REQUEST_FAILED",
                               returned_count=0, matched_count=0, latency_ms=None)
        return {}
    if diagnostics is not None:
        diagnostics.update(status=outcome.status,
                           error_code=outcome.error_code or ("EMPTY_UPSTREAM" if outcome.status == "empty" else None),
                           returned_count=0, matched_count=0, latency_ms=outcome.latency_ms)
    if outcome.status != "success" or not isinstance(outcome.data, dict):
        return {}
    rows = {}
    items = outcome.data.get("items") or []
    if diagnostics is not None:
        diagnostics["returned_count"] = len(items)
    for row in items:
        if str(row.get("trade_date")) != trade_date:
            continue
        code = str(row.get("ts_code") or "").split(".")[0]
        if len(code) != 6 or not code.isdigit():
            continue
        first = str(row.get("first_time") or "")
        seal_time = ""
        if len(first) == 6 and first.isdigit():
            try:
                seal_time = datetime.strptime(first, "%H%M%S").strftime("%H:%M:%S")
            except ValueError:
                pass
        rows[code] = {"industry": str(row.get("industry") or ""), "seal_time": seal_time}
    if diagnostics is not None:
        diagnostics["matched_count"] = len(rows)
        if items and not rows:
            diagnostics["error_code"] = "DATE_OR_CODE_MISMATCH"
    return rows


def _previous_ladder(previous_date: str, db_path=None):
    """Sealed previous-session non-ST up pool with board counts, and broken codes."""

    from .market_facts import DEFAULT_DB, MarketFactStore

    try:
        facts = MarketFactStore(db_path or DEFAULT_DB, read_only=True)
        run = facts.latest_limit_run(previous_date)
    except Exception:
        return None, None, None
    if run is None:
        return None, None, None
    events = facts._events(run["id"])
    boards = {row["code"]: int(row["board_count"] or 1) for row in events
              if row["kind"] == "up" and not indicators.is_st(row["name"])}
    broken = [row["code"] for row in events
              if row["kind"] == "broken" and not indicators.is_st(row["name"])]
    return boards, broken, run.get("board_source")


def _sealed_break_risk(as_of: str, db_path=None):
    """§3.3 value from the sealed facts (break days need 3 completed sessions)."""

    from .market_facts import DEFAULT_DB, MarketFactStore

    try:
        return MarketFactStore(db_path or DEFAULT_DB, read_only=True).consecutive_break_risk(as_of)
    except Exception:
        return {"value": None, "source_gaps": ["break_risk_unavailable"]}


def _bucket(pct: float, state: str | None) -> str:
    if state == "up":
        return "涨停"
    if state == "down":
        return "跌停"
    if pct == 0:
        return "平盘"
    for bound, label in ((7, ">7%"), (5, "5~7%"), (3, "3~5%"), (0, "0~3%")):
        if pct > bound:
            return label
    for bound, label in ((-3, "-0~-3%"), (-5, "-3~-5%"), (-7, "-5~-7%")):
        if pct >= bound:
            return label
    return "<-7%"


def _tencent_rows(codes: list[str], quote_loader) -> dict[str, dict]:
    """Tencent quotes shaped like rt_k rows for the fixed StockToday gap."""

    rows = {}
    for start in range(0, len(codes), 60):
        chunk = codes[start:start + 60]
        quotes = quote_loader([code.split(".")[0] for code in chunk]) or {}
        for ts_code in chunk:
            quote = quotes.get(ts_code.split(".")[0])
            if not isinstance(quote, dict) or not quote.get("price"):
                continue
            rows[ts_code] = {
                "ts_code": ts_code, "name": quote.get("name"), "close": quote.get("price"),
                "pre_close": quote.get("last_close"), "high": quote.get("high"),
                "vol": quote.get("volume"), "amount": quote.get("amount"),
                "updated_at": quote.get("quote_time"),
                "ask_price1": quote.get("ask_price1"), "ask_volume1": quote.get("ask_volume1"),
                "bid_price1": quote.get("bid_price1"), "bid_volume1": quote.get("bid_volume1"),
                "source": "tencent",
            }
    return rows


def _fetch_snapshot_batches(provider, patterns=SNAPSHOT_BATCHES):
    """Fetch every wildcard batch concurrently, keeping the fixed batch order.

    pool.map 按提交顺序交付结果，因此批次诊断、首个失败批次的错误码都与串行
    实现逐字一致；并发只影响墙钟耗时。
    """

    def load(pattern):
        return provider._request_table("rt_k", {"ts_code": pattern})

    workers = min(SNAPSHOT_MAX_WORKERS, len(patterns))
    if workers <= 1:
        return [(pattern, load(pattern)) for pattern in patterns]
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rt-k") as pool:
        return list(zip(patterns, pool.map(load, patterns)))


def build(provider, *, now: datetime | None = None, quote_loader=None) -> dict:
    """Return the intraday state or raise IntradayStateError (no fallback)."""

    now = now or datetime.now(TZ_SHANGHAI)
    rows: list[dict] = []
    batch_latest: list[datetime] = []
    batches = []
    fetched_at = None
    for pattern, outcome in _fetch_snapshot_batches(provider):
        items = (outcome.data or {}).get("items") if isinstance(outcome.data, dict) else None
        batches.append({"ts_code": pattern, "status": outcome.status,
                        "error_code": outcome.error_code, "rows": len(items or []),
                        "latency_ms": outcome.latency_ms})
        if outcome.status != "success" or not items:
            raise IntradayStateError(outcome.error_code or "SNAPSHOT_BATCH_EMPTY",
                                     "provider_error" if outcome.error_code else "empty")
        fetched_at = fetched_at or outcome.fetched_at
        rows.extend(items)
        stamps = [s for s in (_timestamp(r.get("updated_at")) for r in items) if s]
        if stamps:
            batch_latest.append(max(stamps))
    latest = dedupe_latest(rows)
    if not batch_latest:
        raise IntradayStateError("SNAPSHOT_TIME_UNKNOWN")
    try:
        data_as_of, corrected = session_data_time(min(batch_latest))
        age = market_fact_age_seconds(data_as_of, now)
    except TradeCalendarUnavailable:
        raise IntradayStateError("QUALITY_TRADE_CALENDAR_UNAVAILABLE")
    age = int(age) if age is not None else None
    tier = freshness_tier(age)
    if age is None and is_trading_day(now.date()) and now.time() >= dtime(9, 15):
        # A previous-session snapshot during today's session is not "unknown":
        # it is out of date for every intraday use.
        tier = "expired"
    if tier == "expired":
        raise IntradayStateError("QUALITY_STALE", "quality_failure")
    trade_date = data_as_of.strftime("%Y%m%d")

    # stk_limit（涨跌停价）与 limit_list_d（当日明细元数据）只依赖 trade_date，
    # 彼此独立：并发取，省掉串行的一整跳（真实回放 limit_list_d 单跳 0.4~2.1s）。
    metadata_observation: dict = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="limit-meta") as pool:
        limits_future = pool.submit(_stk_limit, provider, trade_date)
        metadata_future = pool.submit(_limit_metadata, provider, trade_date,
                                      diagnostics=metadata_observation)
        limits = limits_future.result()
        limit_metadata = metadata_future.result()
    gaps: list[str] = []
    universe = {code for code in limits if not code.startswith(B_SHARE_PREFIXES)}
    if not universe:
        gaps.append("stk_limit_unavailable")
    filled: list[str] = []
    fill_ms = None
    missing = sorted(universe - set(latest))
    if missing and len(missing) <= STRUCTURAL_FILL_LIMIT:
        if quote_loader is None:
            from .sources.tencent import fetch_quotes as quote_loader
        started = datetime.now()
        try:
            extra = _tencent_rows(missing, quote_loader)
        except Exception:
            extra = {}
            gaps.append("structural_gap_fill_failed")
        fill_ms = int((datetime.now() - started).total_seconds() * 1000)
        latest.update(extra)
        filled = sorted(code.split(".")[0] for code in extra)
    elif missing:
        gaps.append("snapshot_gap_too_large_to_fill")
    covered = len(universe & set(latest)) if universe else len(latest)
    coverage = round(covered / len(universe) * 100, 2) if universe else None
    if coverage is not None and coverage < COVERAGE_MINIMUM:
        raise IntradayStateError("COVERAGE_INSUFFICIENT", "quality_failure")
    if coverage is not None and coverage < COVERAGE_NORMAL:
        gaps.append(f"coverage_below_{COVERAGE_NORMAL:g}")

    changes: dict[str, float] = {}
    states: dict[str, str] = {}
    buckets = dict.fromkeys(BUCKETS, 0)
    detail: dict[str, dict] = {}
    for ts_code, row in latest.items():
        close, previous = _number(row.get("close")), _number(row.get("pre_close"))
        if not close or not previous or not (_number(row.get("vol")) or 0) > 0:
            continue
        code = ts_code.split(".")[0]
        pct = round((close / previous - 1) * 100, 4)
        changes[code] = pct
        limit = limits.get(ts_code)
        state = None
        if limit and not indicators.is_st(row.get("name")):
            state = indicators.limit_state(
                close, _number(row.get("high")), limit["up"], limit["down"],
                ask=(_number(row.get("ask_price1")), _number(row.get("ask_volume1"))),
                bid=(_number(row.get("bid_price1")), _number(row.get("bid_volume1"))),
            )
        if state:
            states[code] = state
            detail[code] = {"code": code, "name": str(row.get("name") or "").strip(),
                            "price": close, "pct": pct, "amount": _number(row.get("amount")),
                            "source": row.get("source") or "stocktoday"}
        buckets[_bucket(pct, state)] += 1

    previous_date = previous_trading_day(data_as_of.date()).strftime("%Y%m%d")
    previous_boards, previous_broken, board_source = _previous_ladder(previous_date)
    if previous_boards is None:
        # No sealed previous-session ladder: board heights and the consecutive
        # count fail closed to null (2026-09-29 audit, 谷米补修一.1).  The gap
        # is typed so the lianban side closes instead of reading 1 板 / 0 连板.
        gaps.append("side_hard:lianban:previous_ladder_missing")
    elif board_source and any(flag in board_source for flag in ("unverified", "disagreement", "repaired")):
        gaps.append("previous_ladder_board_counts_unverified")
    limit_sets = {kind: [c for c, s in states.items() if s == kind] for kind in ("up", "down", "broken")}
    current_boards = {code: (previous_boards or {}).get(code, 0) + 1 for code in limit_sets["up"]}
    break_risk = _sealed_break_risk(previous_date)
    if break_risk.get("value") is None:
        gaps.append("consecutive_break_risk_missing")
    summary = indicators.summarize(
        changes=changes,
        limit_sets=limit_sets if limits else {},
        current_boards=current_boards if limits else {},
        previous_boards=previous_boards if limits else None,
        previous_broken=previous_broken,
        board_risk_value=break_risk.get("value"),
        ladder_available=previous_boards is not None,
    )
    for code, board in current_boards.items():
        detail[code]["board"] = board
    metadata = limit_metadata
    if limit_sets["up"] and not metadata:
        gaps.append("limit_up_metadata_empty" if metadata_observation.get("status") == "empty"
                    else "limit_up_metadata_unavailable")
    for code in limit_sets["up"]:
        row = metadata.get(code) or {}
        if row.get("seal_time") and row["seal_time"] > data_as_of.strftime("%H:%M:%S"):
            row = {}
        detail[code].update(industry=row.get("industry") or "", seal_time=row.get("seal_time") or "")
    if any(not detail[c].get("industry") or not detail[c].get("seal_time") for c in limit_sets["up"]):
        gaps.append("limit_up_metadata_incomplete")

    def listing(kind):
        return sorted((detail[c] for c in limit_sets[kind]),
                      key=lambda item: (-item.get("board", 0), item["code"]))

    ladder: dict[str, list[str]] = {}
    for code, board in sorted(current_boards.items()):
        ladder.setdefault(str(board), []).append(code)
    buckets["_total"] = len(changes)
    return {
        "trade_date": trade_date,
        "previous_trade_date": previous_date,
        "data_as_of": data_as_of.isoformat(timespec="seconds"),
        "vendor_stamp_corrected": corrected,
        "age_seconds": age,
        "freshness_tier": tier,
        "coverage": {"covered": covered, "universe": len(universe) or None, "pct": coverage},
        "filled_by_fallback": filled,
        "fill_ms": fill_ms,
        "indicators": summary,
        "limit_up": listing("up"),
        "limit_down": listing("down"),
        "broken": listing("broken"),
        "ladder": ladder,
        "breadth_buckets": buckets,
        "previous_ladder_board_source": board_source,
        "source_gaps": gaps,
        "_stocktoday": {"api_name": "rt_k", "batches": batches, "unique_code_count": len(latest),
                        "limit_metadata": metadata_observation,
                        "deduplicated_rows": len(rows) - len(latest),
                        "stk_limit_rows": len(limits), "fetched_at": fetched_at},
    }
