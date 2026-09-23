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


def is_trading_day(day: date) -> bool:
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
