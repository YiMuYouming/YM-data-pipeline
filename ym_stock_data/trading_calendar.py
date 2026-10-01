"""Packaged, bounded A-share session calendar for canonical date decisions."""

from __future__ import annotations

import json
from datetime import date, datetime, time as datetime_time, timedelta
from functools import lru_cache
from importlib import resources


class TradeCalendarUnavailable(ValueError):
    """The packaged exchange calendar cannot establish a trading date."""


@lru_cache(maxsize=1)
def _calendar() -> tuple[frozenset[int], frozenset[date]]:
    try:
        path = resources.files("ym_stock_data.v3").joinpath("trading-holidays.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        years = payload["years"]
        raw_holidays = payload["holidays"]
        if (
            not isinstance(years, list)
            or not years
            or any(type(year) is not int for year in years)
            or len(set(years)) != len(years)
            or not isinstance(raw_holidays, list)
            or not raw_holidays
        ):
            raise ValueError("invalid calendar structure")
        holidays = []
        for value in raw_holidays:
            if not isinstance(value, str) or len(value) != 10:
                raise ValueError("invalid holiday date")
            day = date.fromisoformat(value)
            if day.isoformat() != value or day.year not in years:
                raise ValueError("holiday outside coverage")
            holidays.append(day)
        if len(set(holidays)) != len(holidays):
            raise ValueError("duplicate holiday")
        return frozenset(years), frozenset(holidays)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise TradeCalendarUnavailable("CALENDAR_UNAVAILABLE: exchange calendar invalid") from exc


def is_trading_day(day: date | str) -> bool:
    if isinstance(day, str):
        cleaned = day.strip()
        if len(cleaned) == 8 and cleaned.isdigit():
            day = date(int(cleaned[:4]), int(cleaned[4:6]), int(cleaned[6:]))
        elif len(cleaned) == 10 and cleaned[4] == "-" and cleaned[7] == "-":
            day = date.fromisoformat(cleaned)
        else:
            raise ValueError(f"invalid date format: {day!r}")
    elif isinstance(day, datetime):
        day = day.date()
    elif not isinstance(day, date):
        raise TypeError(f"expected date or str, got {type(day).__name__}")
    years, holidays = _calendar()
    if day.year not in years:
        raise TradeCalendarUnavailable(
            f"CALENDAR_UNAVAILABLE: exchange calendar does not cover {day.year}"
        )
    return day.weekday() < 5 and day not in holidays


def previous_trading_day(day: date) -> date:
    candidate = day - timedelta(days=1)
    for _ in range(366):
        if is_trading_day(candidate):
            return candidate
        candidate -= timedelta(days=1)
    raise TradeCalendarUnavailable("CALENDAR_UNAVAILABLE: no prior trading day")


def next_trading_day(day: date) -> date:
    candidate = day + timedelta(days=1)
    for _ in range(366):
        if is_trading_day(candidate):
            return candidate
        candidate += timedelta(days=1)
    raise TradeCalendarUnavailable("CALENDAR_UNAVAILABLE: no next trading day")


def latest_completed_trade_date(now: datetime) -> str:
    candidate = now.date()
    if not is_trading_day(candidate) or now.hour < 15:
        candidate = previous_trading_day(candidate)
    return candidate.strftime("%Y%m%d")


def session_seconds(stamp: datetime) -> float:
    """Elapsed A-share auction and continuous-trading seconds in one day."""

    second = stamp.hour * 3600 + stamp.minute * 60 + stamp.second + stamp.microsecond / 1e6
    sessions = ((9 * 3600 + 15 * 60, 9 * 3600 + 25 * 60),
                (9 * 3600 + 30 * 60, 11 * 3600 + 30 * 60),
                (13 * 3600, 15 * 3600))
    return sum(max(0.0, min(second, end) - start) for start, end in sessions)


def market_fact_age_seconds(stamp: datetime, now: datetime) -> float | None:
    """Age of an A-share quote; None means wrong trading day or future fact."""

    if stamp > now + timedelta(minutes=5):
        return None
    today_open = is_trading_day(now.date())
    current_session = today_open and now.time() >= datetime_time(9, 15)
    expected_day = now.date() if current_session else previous_trading_day(now.date())
    if stamp.date() != expected_day:
        return None
    if current_session:
        age = session_seconds(now) - session_seconds(stamp)
        if stamp.time() < datetime_time(9, 15):
            opening = stamp.replace(hour=9, minute=15, second=0, microsecond=0)
            age += (opening - stamp).total_seconds()
        return age
    close = stamp.replace(hour=15, minute=0, second=0, microsecond=0)
    return session_seconds(close) - session_seconds(stamp)


def week_info(day: date | str) -> dict[str, object]:
    """Everything a weekly report or a "next trade date" decision needs, in one call.

    看板的周分组、周报页、下一交易日判断以前各有一份本地实现，三份可能互相对不上。
    这里只给一份；调用方不再自己数星期。
    """
    if isinstance(day, str):
        if not (len(day) == 10 and day[4] == "-" and day[7] == "-"):
            raise ValueError(f"invalid date format: {day!r}")
        target = date.fromisoformat(day)
    elif isinstance(day, datetime):
        target = day.date()
    elif isinstance(day, date):
        target = day
    else:
        raise TypeError(f"expected date or str, got {type(day).__name__}")

    iso_year, iso_week, _weekday = target.isocalendar()
    monday = target - timedelta(days=target.weekday())
    trading_days: list[str] = []
    for offset in range(7):
        candidate = monday + timedelta(days=offset)
        if is_trading_day(candidate) and candidate <= target:
            trading_days.append(candidate.isoformat())
    previous_monday = monday - timedelta(days=7)
    prev_week_last = None
    for offset in range(6, -1, -1):
        candidate = previous_monday + timedelta(days=offset)
        if is_trading_day(candidate):
            prev_week_last = candidate
            break
    if prev_week_last is None:
        prev_week_last = previous_trading_day(monday)
    return {
        "week_label": f"{iso_year:04d}-W{iso_week:02d}",
        "iso_year": iso_year,
        "iso_week": iso_week,
        "trading_days": trading_days,
        "prev_trading_day": previous_trading_day(target).isoformat(),
        "next_trading_day": next_trading_day(target).isoformat(),
        "prev_week_last_trading_day": prev_week_last.isoformat(),
    }
