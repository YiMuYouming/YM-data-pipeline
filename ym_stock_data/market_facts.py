"""Dated A-share limit-event facts and deterministic daily derivatives.

The database is separate from dashboard/account data.  Each successful ingest
appends a complete source run; failed or partial input never changes stored
facts.  Fetch receipts are not treated as exchange event timestamps.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import date, datetime, time
from pathlib import Path

from .contracts import TZ_SHANGHAI
from .sources.limit_state import derive_limit_promotion
from .trading_calendar import is_trading_day, previous_trading_day


DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "market-facts.sqlite3"
_KINDS = {"zt": "up", "zb": "broken", "dt": "down"}
_TUSHARE_KINDS = {"U": "zt", "D": "dt", "Z": "zb"}


def _day(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        raise ValueError("trade date must use YYYYMMDD")
    result = datetime.strptime(value, "%Y%m%d").date()
    if not is_trading_day(result):
        raise ValueError("date is not an exchange trading day")
    return result


def _stamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("source timestamp is missing")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("source timestamp lacks timezone")
    return result.astimezone(TZ_SHANGHAI)


def _hash(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_stocktoday_limits(trade_date: str, results: dict[str, dict]) -> dict:
    """Accept all three dated Tushare board pools or no historical run at all."""
    _day(trade_date)
    pools = {"zt": [], "dt": [], "zb": [], "yzt": []}
    fetched = []
    step_result = results.get("STEP")
    step_meta = (step_result or {}).get("_meta") or {}
    step_data = (step_result or {}).get("data") or {}
    if (step_meta.get("status") != "success" or step_meta.get("provider_used") != "stocktoday"
            or not isinstance(step_data.get("items"), list) or step_data.get("truncated")):
        raise ValueError("historical consecutive-board ladder unavailable")
    step_boards = {}
    for item in step_data["items"]:
        if not isinstance(item, dict) or item.get("trade_date") != trade_date:
            raise ValueError("historical ladder date mismatch")
        code = str(item.get("ts_code") or "").split(".")[0]
        try:
            board = int(item.get("nums"))
        except (TypeError, ValueError):
            raise ValueError("historical ladder board count invalid") from None
        if not re.fullmatch(r"\d{6}", code) or board < 2 or code in step_boards:
            raise ValueError("historical ladder malformed")
        step_boards[code] = (board, str(item.get("name") or ""))
    fetched.append(_stamp(step_meta.get("fetched_at")))
    board_source = "limit_step_crosschecked"
    for limit_type, kind in _TUSHARE_KINDS.items():
        result = results.get(limit_type)
        if not isinstance(result, dict):
            raise ValueError(f"historical {limit_type} pool missing")
        meta, data = result.get("_meta") or {}, result.get("data") or {}
        if meta.get("status") not in {"success", "empty"} or meta.get("provider_used") != "stocktoday":
            raise ValueError(f"historical {limit_type} pool unavailable")
        if data.get("truncated") or not isinstance(data.get("items"), list):
            raise ValueError(f"historical {limit_type} pool incomplete")
        if data.get("total_present") and data.get("total") != len(data["items"]):
            raise ValueError(f"historical {limit_type} pool count mismatch")
        fetched.append(_stamp(meta.get("fetched_at")))
        shifted = (data["items"] and
                   sum(not isinstance(row, dict) or not isinstance(row.get("close"), (int, float))
                       or row.get("close") <= 0 for row in data["items"]) > len(data["items"]) // 4)
        if limit_type == "U" and shifted:
            board_source = "limit_step_reference_unverified"
        for item in data["items"]:
            if (not isinstance(item, dict) or item.get("trade_date") != trade_date
                    or item.get("limit_type") not in {None, limit_type}):
                raise ValueError(f"historical {limit_type} row date or type mismatch")
            code = str(item.get("ts_code") or "").split(".")[0]
            if not re.fullmatch(r"\d{6}", code):
                raise ValueError(f"historical {limit_type} code invalid")
            row_shifted = shifted or not isinstance(item.get("close"), (int, float)) or item.get("close") <= 0
            if limit_type == "U" and row_shifted and not shifted:
                board_source = "limit_step_partial_reference_unverified"
            step_board = step_boards.get(code, (1, ""))[0]
            raw_board = item.get("limit_times")
            if limit_type == "U" and not row_shifted and type(raw_board) is int and raw_board >= 1:
                board = raw_board
                if raw_board != step_board:
                    board_source = "limit_list_d_ladder_disagreement"
            else:
                board = step_board if limit_type == "U" else 0
                if limit_type == "U" and board_source == "limit_step_crosschecked":
                    board_source = "limit_step_partial_reference_unverified"
            pools[kind].append({"code": code, "name": str(item.get("name") or ""),
                                "limit_days": board, "price": item.get("close") if not row_shifted else None,
                                "pct": item.get("pct_chg") if not row_shifted else None,
                                "break_times": item.get("open_times") if not row_shifted else None,
                                "industry": str(item.get("industry") or "")})
    up_codes = {item["code"] for item in pools["zt"]}
    if any(code not in up_codes and "ST" not in name.upper() for code, (_, name) in step_boards.items()):
        raise ValueError("historical non-ST ladder stock absent from up-pool")
    if not pools["zt"]:
        raise ValueError("historical up-pool empty; completeness unverified")
    return {"data": {"date": trade_date, "pools": pools, "_board_source": board_source,
                     "zt_count": len(pools["zt"]), "dt_count": len(pools["dt"]),
                     "zb_count": len(pools["zb"])},
            "_meta": {"status": "success", "provider_used": "stocktoday",
                      "fetched_at": max(fetched).isoformat(),
                      "quality": {"status": "normal", "reason_codes": []}}}


class MarketFactStore:
    def __init__(self, path: str | Path = DEFAULT_DB, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        if read_only:
            if not self.path.is_file():
                raise FileNotFoundError(f"market facts database missing: {self.path}")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS limit_runs (
                    id INTEGER PRIMARY KEY,
                    trade_date TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    source_time_status TEXT NOT NULL,
                    board_source TEXT NOT NULL DEFAULT 'provider_reported',
                    up_count INTEGER NOT NULL,
                    down_count INTEGER NOT NULL,
                    broken_count INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS limit_runs_day ON limit_runs(trade_date, id);
                CREATE TABLE IF NOT EXISTS limit_events (
                    run_id INTEGER NOT NULL REFERENCES limit_runs(id),
                    kind TEXT NOT NULL CHECK(kind IN ('up','down','broken')),
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    board_count INTEGER NOT NULL,
                    price REAL,
                    pct_change REAL,
                    break_times INTEGER,
                    industry TEXT,
                    PRIMARY KEY(run_id, kind, code)
                );
                CREATE TABLE IF NOT EXISTS quote_runs (
                    id INTEGER PRIMARY KEY,
                    trade_date TEXT NOT NULL,
                    prior_limit_run_id INTEGER NOT NULL REFERENCES limit_runs(id),
                    provider TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    row_count INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS quote_runs_day ON quote_runs(trade_date, id);
                CREATE TABLE IF NOT EXISTS quote_rows (
                    run_id INTEGER NOT NULL REFERENCES quote_runs(id),
                    code TEXT NOT NULL,
                    price REAL NOT NULL,
                    pct_change REAL NOT NULL,
                    quote_time TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    PRIMARY KEY(run_id, code)
                );
                CREATE TABLE IF NOT EXISTS daily_runs (
                    id INTEGER PRIMARY KEY,
                    trade_date TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    up_count INTEGER NOT NULL,
                    flat_count INTEGER NOT NULL,
                    down_count INTEGER NOT NULL,
                    universe TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS daily_runs_day ON daily_runs(trade_date, id);
                CREATE TABLE IF NOT EXISTS daily_rows (
                    run_id INTEGER NOT NULL REFERENCES daily_runs(id),
                    code TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    pre_close REAL NOT NULL,
                    pct_change REAL NOT NULL,
                    volume_lots REAL NOT NULL,
                    amount_thousand_cny REAL NOT NULL,
                    PRIMARY KEY(run_id, code)
                );
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(quote_rows)")}
            if "provider" not in columns:
                conn.execute("ALTER TABLE quote_rows ADD COLUMN provider TEXT NOT NULL DEFAULT 'unknown_legacy'")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(limit_runs)")}
            if "board_source" not in columns:
                conn.execute("ALTER TABLE limit_runs ADD COLUMN board_source TEXT NOT NULL DEFAULT 'provider_reported'")

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        else:
            conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def latest_limit_run(self, trade_date: str) -> dict | None:
        _day(trade_date)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM limit_runs WHERE trade_date=? ORDER BY (phase='post_close_observed') DESC, id DESC LIMIT 1",
                (trade_date,),
            ).fetchone()
        return dict(row) if row else None

    def _events(self, run_id: int, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM limit_events WHERE run_id=?"
        values: tuple = (run_id,)
        if kind is not None:
            sql += " AND kind=?"
            values += (kind,)
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql + " ORDER BY kind, code", values)]

    def prior_cohort_codes(self, trade_date: str) -> tuple[int, list[str]]:
        previous = previous_trading_day(_day(trade_date)).strftime("%Y%m%d")
        run = self.latest_limit_run(previous)
        if run is None:
            raise ValueError("previous limit snapshot missing")
        rows = self._events(run["id"])
        rows = [row for row in rows if row["kind"] in {"up", "broken"}]
        codes = [row["code"] for row in rows if "ST" not in row["name"].upper()]
        if not codes:
            raise ValueError("previous non-ST limit cohort missing")
        return run["id"], codes

    def ingest_limits(self, trade_date: str, result: dict) -> dict:
        day = _day(trade_date)
        if not isinstance(result, dict):
            raise ValueError("limit source response missing")
        meta, data = result.get("_meta") or {}, result.get("data") or {}
        if meta.get("status") not in {"success", "degraded"} or not meta.get("provider_used"):
            raise ValueError("limit source did not succeed")
        if not isinstance(data, dict) or data.get("date") != trade_date:
            raise ValueError("limit source date mismatch")
        fetched = _stamp(meta.get("fetched_at"))
        pools = data.get("pools")
        if not isinstance(pools, dict):
            raise ValueError("limit pools missing")
        rows: list[tuple] = []
        seen: set[str] = set()
        counts: dict[str, int] = {}
        for raw_kind, kind in _KINDS.items():
            bucket = pools.get(raw_kind)
            count_key = {"zt": "zt_count", "zb": "zb_count", "dt": "dt_count"}[raw_kind]
            if (
                not isinstance(bucket, list)
                or type(data.get(count_key)) is not int
                or data[count_key] != len(bucket)
            ):
                raise ValueError("limit pool count mismatch")
            counts[kind] = len(bucket)
            for item in bucket:
                if not isinstance(item, dict):
                    raise ValueError("invalid limit stock row")
                code = str(item.get("code") or "")
                board = item.get("limit_days")
                if (not re.fullmatch(r"\d{6}", code) or code in seen or type(board) is not int
                        or (kind == "up" and board < 1) or (kind != "up" and board < 0)):
                    raise ValueError("duplicate or malformed limit stock row")
                seen.add(code)
                rows.append((kind, code, str(item.get("name") or ""), board,
                             item.get("price"), item.get("pct"), item.get("break_times"),
                             str(item.get("industry") or "")))
        if not counts["up"]:
            raise ValueError("limit-up pool empty; completeness unverified")
        phase = (
            "provisional"
            if fetched.date() == day and fetched.time() < time(15, 30)
            else "post_close_observed"
        )
        digest = _hash({"date": trade_date, "provider": meta["provider_used"], "rows": rows})
        board_source = str(data.get("_board_source") or "provider_reported")
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO limit_runs(trade_date,provider,fetched_at,phase,payload_sha256,source_time_status,board_source,up_count,down_count,broken_count) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (trade_date, str(meta["provider_used"]), fetched.isoformat(), phase,
                 digest, "unavailable", board_source, counts["up"], counts["down"], counts["broken"]),
            )
            run_id = cursor.lastrowid
            conn.executemany(
                "INSERT INTO limit_events(run_id,kind,code,name,board_count,price,pct_change,break_times,industry) VALUES(?,?,?,?,?,?,?,?,?)",
                [(run_id, *row) for row in rows],
            )
        return {"run_id": run_id, "trade_date": trade_date, "provider": meta["provider_used"],
                "phase": phase, "counts": counts, "payload_sha256": digest,
                "source_time_status": "unavailable", "board_source": board_source}

    def ingest_quotes(self, trade_date: str, result: dict) -> dict:
        _day(trade_date)
        prior_run_id, codes = self.prior_cohort_codes(trade_date)
        if not isinstance(result, dict):
            raise ValueError("quote source response missing")
        meta, data = result.get("_meta") or {}, result.get("data") or {}
        if meta.get("status") not in {"success", "degraded"} or not meta.get("provider_used"):
            raise ValueError("quote source did not succeed")
        if not isinstance(data, dict):
            raise ValueError("quote rows missing")
        fetched = _stamp(meta.get("fetched_at"))
        rows: list[tuple] = []
        for code in codes:
            item = data.get(code)
            if not isinstance(item, dict):
                raise ValueError("quote cohort incomplete")
            price, pct = item.get("price"), item.get("change_pct")
            if (isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0
                    or isinstance(pct, bool) or not isinstance(pct, (int, float))):
                raise ValueError("quote cohort has missing price or change")
            quote_time = _stamp(item.get("quote_time"))
            if quote_time.strftime("%Y%m%d") != trade_date:
                raise ValueError("quote cohort contains another trading day")
            provider = str(item.get("source") or meta["provider_used"])
            rows.append((code, float(price), float(pct), quote_time.isoformat(), provider))
        digest = _hash({"date": trade_date, "provider": meta["provider_used"], "rows": rows})
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO quote_runs(trade_date,prior_limit_run_id,provider,fetched_at,payload_sha256,row_count) VALUES(?,?,?,?,?,?)",
                (trade_date, prior_run_id, str(meta["provider_used"]), fetched.isoformat(), digest, len(rows)),
            )
            run_id = cursor.lastrowid
            conn.executemany(
                "INSERT INTO quote_rows(run_id,code,price,pct_change,quote_time,provider) VALUES(?,?,?,?,?,?)",
                [(run_id, *row) for row in rows],
            )
        return {"run_id": run_id, "trade_date": trade_date, "provider": meta["provider_used"],
                "row_count": len(rows), "prior_limit_run_id": prior_run_id,
                "payload_sha256": digest}

    def ingest_daily(self, trade_date: str, result: dict) -> dict:
        """Store one complete provider-reported non-suspended A-share daily bar set."""
        _day(trade_date)
        if not isinstance(result, dict):
            raise ValueError("daily response missing")
        meta, data = result.get("_meta") or {}, result.get("data") or {}
        if meta.get("status") != "success" or meta.get("provider_used") != "stocktoday":
            raise ValueError("daily source unavailable")
        items = data.get("items")
        if not isinstance(items, list) or data.get("truncated") or not 4000 <= len(items) < 6000:
            raise ValueError("daily all-market row coverage unverified")
        if data.get("total_present") and data.get("total") != len(items):
            raise ValueError("daily reported count mismatch")
        fetched = _stamp(meta.get("fetched_at"))
        rows, seen = [], set()
        for item in items:
            if not isinstance(item, dict) or item.get("trade_date") != trade_date:
                raise ValueError("daily date mismatch")
            code = str(item.get("ts_code") or "").split(".")[0]
            if not re.fullmatch(r"\d{6}", code) or code in seen:
                raise ValueError("daily code missing or repeated")
            seen.add(code)
            names = ("open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount")
            values = []
            for name in names:
                value = item.get(name)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"daily {name} missing or invalid")
                values.append(float(value))
            opening, high, low, close, pre_close, pct, volume, amount = values
            if min(opening, high, low, close, pre_close) <= 0 or high < max(opening, close, low) or low > min(opening, close) or min(volume, amount) < 0:
                raise ValueError("daily OHLC or units invalid")
            rows.append((code, *values))
        up = sum(row[6] > 0 for row in rows)
        flat = sum(row[6] == 0 for row in rows)
        down = len(rows) - up - flat
        digest = _hash({"date": trade_date, "provider": "stocktoday", "rows": rows})
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO daily_runs(trade_date,provider,fetched_at,payload_sha256,row_count,up_count,flat_count,down_count,universe) VALUES(?,?,?,?,?,?,?,?,?)",
                (trade_date, "stocktoday", fetched.isoformat(), digest, len(rows), up, flat, down,
                 "traded_daily_rows_excludes_suspended"),
            )
            run_id = cursor.lastrowid
            conn.executemany(
                "INSERT INTO daily_rows(run_id,code,open,high,low,close,pre_close,pct_change,volume_lots,amount_thousand_cny) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(run_id, *row) for row in rows],
            )
        return {"run_id": run_id, "trade_date": trade_date, "provider": "stocktoday",
                "row_count": len(rows), "up_count": up, "flat_count": flat,
                "down_count": down, "payload_sha256": digest,
                "universe": "traded_daily_rows_excludes_suspended"}

    def _state(self, run: dict) -> dict:
        rows = self._events(run["id"], "up")
        return {
            "date": run["trade_date"], "zt_count": run["up_count"],
            "pools": {"zt": [{"code": row["code"], "name": row["name"],
                               "limit_days": row["board_count"]} for row in rows]},
        }

    def report(self, trade_date: str) -> dict:
        previous_date = previous_trading_day(_day(trade_date)).strftime("%Y%m%d")
        current = self.latest_limit_run(trade_date)
        previous = self.latest_limit_run(previous_date)
        with self._connect() as conn:
            daily = conn.execute(
                "SELECT * FROM daily_runs WHERE trade_date=? ORDER BY id DESC LIMIT 1", (trade_date,)
            ).fetchone()
            previous_daily = conn.execute(
                "SELECT * FROM daily_runs WHERE trade_date=? ORDER BY id DESC LIMIT 1", (previous_date,)
            ).fetchone()

        def limit_daily_conflicts(limit_run: dict | None, daily_run: sqlite3.Row | None) -> dict:
            if limit_run is None or daily_run is None:
                return {"checked": False, "missing_codes": [], "nonpositive_codes": []}
            with self._connect() as conn:
                changes = {row["code"]: row["pct_change"] for row in conn.execute(
                    "SELECT code,pct_change FROM daily_rows WHERE run_id=?", (daily_run["id"],)
                )}
            codes = [row["code"] for row in self._events(limit_run["id"], "up")]
            return {"checked": True,
                    "missing_codes": sorted(code for code in codes if code not in changes),
                    "nonpositive_codes": sorted(code for code in codes if code in changes and changes[code] <= 0)}

        current_check = limit_daily_conflicts(current, daily)
        previous_check = limit_daily_conflicts(previous, previous_daily)
        current_conflict = bool(current_check["missing_codes"] or current_check["nonpositive_codes"])
        previous_conflict = bool(previous_check["missing_codes"] or previous_check["nonpositive_codes"])
        gaps: list[str] = []
        promotion = None
        if current_conflict:
            gaps.append("current_limit_daily_universe_conflict")
        if previous_conflict:
            gaps.append("previous_limit_daily_universe_conflict")
        if current is None:
            gaps.append("today_limit_snapshot_missing")
        if previous is None:
            gaps.append("previous_limit_snapshot_missing")
        overall_promotion = None
        if current and previous:
            if current_conflict or previous_conflict:
                gaps.append("promotion_limit_pool_conflict")
            elif current["provider"] != previous["provider"]:
                gaps.append("promotion_mixed_limit_sources")
            else:
                prev_codes = {row["code"] for row in self._events(previous["id"], "up")
                              if "ST" not in row["name"].upper()}
                curr_codes = {row["code"] for row in self._events(current["id"], "up")
                              if "ST" not in row["name"].upper()}
                if prev_codes and curr_codes:
                    overall_promotion = {"numerator": len(prev_codes & curr_codes),
                                         "denominator": len(prev_codes),
                                         "pct": round(len(prev_codes & curr_codes) / len(prev_codes) * 100, 6),
                                         "basis": "same_source_next_trade_day_up_pool_intersection"}
                if any(any(flag in run["board_source"] for flag in ("unverified", "disagreement", "repaired"))
                       for run in (current, previous)):
                    gaps.append("promotion_tier_board_counts_unverified")
                else:
                    try:
                        promotion = derive_limit_promotion(
                            self._state(previous), self._state(current),
                            previous_date=previous_date, current_date=trade_date,
                        )
                        promotion["source"] = current["provider"]
                    except ValueError:
                        gaps.append("promotion_input_incomplete")
        returns = {"yesterday_limit_up_return_pct": None,
                   "yesterday_consecutive_return_pct": None,
                   "yesterday_broken_return_pct": None}
        return_counts = {"yesterday_limit_up": 0,
                         "yesterday_consecutive": 0,
                         "yesterday_broken": 0}
        quote_evidence = None
        return_evidence = {}
        if previous:
            prior_events = [row for row in self._events(previous["id"])
                            if row["kind"] in {"up", "broken"} and "ST" not in row["name"].upper()]
            prior_codes = {row["code"] for row in prior_events}
            cohorts = {
                "yesterday_limit_up": [row["code"] for row in prior_events if row["kind"] == "up"],
                "yesterday_consecutive": [row["code"] for row in prior_events if row["kind"] == "up" and row["board_count"] >= 2],
                "yesterday_broken": [row["code"] for row in prior_events if row["kind"] == "broken"],
            }
            return_counts = {key: len(codes) for key, codes in cohorts.items()}
            consecutive_cohort_unverified = any(
                flag in previous["board_source"] for flag in ("unverified", "disagreement", "repaired")
            )
            if consecutive_cohort_unverified:
                cohorts["yesterday_consecutive"] = []
                return_counts["yesterday_consecutive"] = None
                gaps.append("yesterday_consecutive_cohort_board_counts_unverified")
            with self._connect() as conn:
                quote = conn.execute(
                    "SELECT * FROM quote_runs WHERE trade_date=? AND prior_limit_run_id=? ORDER BY id DESC LIMIT 1",
                    (trade_date, previous["id"]),
                ).fetchone()
                quote_rows = (
                    [dict(row) for row in conn.execute("SELECT * FROM quote_rows WHERE run_id=?", (quote["id"],))]
                    if quote else []
                )
            quote_complete = bool(prior_codes) and quote and len(quote_rows) == len(prior_codes) and {row["code"] for row in quote_rows} == prior_codes
            if quote_complete:
                quote_evidence = {"provider": quote["provider"], "run_id": quote["id"],
                                  "row_count": len(quote_rows),
                                  "providers": sorted({row["provider"] for row in quote_rows}),
                                  "fetched_at": quote["fetched_at"],
                                  "oldest_quote_time": min(row["quote_time"] for row in quote_rows),
                                  "latest_quote_time": max(row["quote_time"] for row in quote_rows)}
            daily_rows = []
            if daily:
                with self._connect() as conn:
                    daily_rows = [dict(row) for row in conn.execute(
                        "SELECT code,pct_change FROM daily_rows WHERE run_id=?", (daily["id"],)
                    ) if row["code"] in prior_codes]
            daily_changes = {row["code"]: row["pct_change"] for row in daily_rows}
            quote_changes = {row["code"]: row["pct_change"] for row in quote_rows} if quote_complete else {}
            for key, codes in cohorts.items():
                if previous_conflict and key in {"yesterday_limit_up", "yesterday_consecutive"}:
                    gaps.append(f"{key}_source_pool_conflict")
                    continue
                if key == "yesterday_consecutive" and consecutive_cohort_unverified:
                    continue
                if not codes:
                    gaps.append(f"{key}_cohort_empty")
                    continue
                if all(code in daily_changes for code in codes):
                    changes = daily_changes
                    return_evidence[key] = {"provider": daily["provider"], "run_id": daily["id"],
                                            "row_count": len(codes), "trade_date": trade_date,
                                            "fetched_at": daily["fetched_at"], "basis": "unadjusted_daily_pct_chg"}
                elif all(code in quote_changes for code in codes):
                    changes = quote_changes
                    return_evidence[key] = {**quote_evidence, "row_count": len(codes),
                                            "basis": "same_day_snapshot_change_pct"}
                else:
                    gaps.append(f"{key}_return_cohort_incomplete")
                    continue
                returns[f"{key}_return_pct"] = round(sum(changes[code] for code in codes) / len(codes), 6)
        emotion = None
        if daily:
            emotion = {"score": round(daily["up_count"] / daily["row_count"] * 100, 6),
                       "up": daily["up_count"], "flat": daily["flat_count"],
                       "down": daily["down_count"], "denominator": daily["row_count"],
                       "universe": daily["universe"], "provider": daily["provider"],
                       "fetched_at": daily["fetched_at"], "run_id": daily["id"]}
            gaps.append("emotion_all_listed_denominator_unverified")
        else:
            gaps.append("all_market_daily_breadth_missing")
        gaps.append("consecutive_break_risk_definition_and_adjusted_history_missing")
        return {
            "trade_date": trade_date,
            "previous_trade_date": previous_date,
            "counts": ({"up": current["up_count"], "down": current["down_count"],
                        "broken": current["broken_count"]} if current else None),
            "promotion": promotion,
            "promotion_overall_by_code": overall_promotion,
            **returns,
            "return_cohort_counts": return_counts,
            "yimu_emotion": emotion,
            "ths_emotion_equivalent": None,
            "consecutive_break_risk": None,
            "source_gaps": gaps,
            "limit_evidence": {
                "current": ({key: current[key] for key in ("id", "provider", "fetched_at", "phase", "payload_sha256", "source_time_status", "board_source")} if current else None),
                "previous": ({key: previous[key] for key in ("id", "provider", "fetched_at", "phase", "payload_sha256", "source_time_status", "board_source")} if previous else None),
            },
            "limit_daily_quality": {"current": current_check, "previous": previous_check},
            "quote_evidence": quote_evidence,
            "return_evidence": return_evidence,
        }
