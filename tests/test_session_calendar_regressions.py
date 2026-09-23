"""Regressions for session freshness, complete index batches, and holidays."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ym_stock_data import api
from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome


def _at(value):
    return datetime.fromisoformat(value).replace(tzinfo=TZ_SHANGHAI)


def _bar():
    return {
        "datetime": "2026-09-22 15:00:00",
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "volume": 1000,
        "amount": 10500,
    }


def _index(code):
    return {
        "index_code": code,
        "bars": [_bar()],
        "adjustment": "none",
        "volume_unit": "share",
        "amount_unit": "CNY",
    }


def _snapshot(stamp):
    return {
        "600519": {
            "code": "600519",
            "price": 10.0,
            "last_close": 9.9,
            "open": 9.9,
            "high": 10.1,
            "low": 9.8,
            "volume": 1000,
            "amount": 10000,
            "quote_time": stamp,
        }
    }


class _Provider:
    def __init__(self, name, data):
        self.name = name
        self.data = data
        self.calls = 0

    def call(self, intent, params):
        self.calls += 1
        return ProviderOutcome(self.name, "success", data=self.data)


class SessionCalendarRegressions(unittest.TestCase):
    def test_lunch_close_and_weekend_keep_last_session_quote_time(self):
        examples = (
            ("2026-09-23T12:00:00", "2026-09-23T11:30:00"),
            ("2026-09-23T17:00:00", "2026-09-23T15:00:00"),
            ("2026-09-26T12:00:00", "2026-09-24T15:00:00"),
        )
        for now, quote in examples:
            with self.subTest(now=now), patch.object(
                api, "_now_shanghai", return_value=_at(now), create=True
            ):
                failure = api._quality_failure_code(
                    "stock_snapshot",
                    {"codes": ["600519"]},
                    _snapshot(quote),
                    ProviderOutcome("pytdx", "success"),
                    60,
                )
                self.assertIsNone(failure)

    def test_active_and_lunch_old_quotes_are_stale(self):
        for now, quote in (
            ("2026-09-23T10:00:00", "2026-09-23T09:55:00"),
            ("2026-09-23T12:00:00", "2026-09-23T11:20:00"),
            ("2026-09-23T13:02:00", "2026-09-23T11:30:00"),
            ("2026-09-23T12:00:00", "2026-09-22T15:00:00"),
        ):
            with self.subTest(now=now), patch.object(
                api, "_now_shanghai", return_value=_at(now), create=True
            ):
                self.assertEqual(
                    "QUALITY_SNAPSHOT_STALE",
                    api._quality_failure_code(
                        "stock_snapshot",
                        {"codes": ["600519"]},
                        _snapshot(quote),
                        ProviderOutcome("pytdx", "success"),
                        60,
                    ),
                )

    def test_lunch_provider_timestamp_uses_same_session_clock(self):
        with patch.object(api, "_now_shanghai", return_value=_at("2026-09-23T12:00:00")):
            self.assertIsNone(
                api._quality_failure_code(
                    "stock_snapshot",
                    {"codes": ["600519"]},
                    _snapshot("2026-09-23T11:30:00"),
                    ProviderOutcome(
                        "pytdx", "success", fetched_at="2026-09-23T11:30:00"
                    ),
                    60,
                )
            )

    def test_nontrading_day_fresh_fetch_keeps_previous_close_quote(self):
        with patch.object(api, "_now_shanghai", return_value=_at("2026-09-26T12:00:00")):
            self.assertIsNone(
                api._quality_failure_code(
                    "stock_snapshot",
                    {"codes": ["600519"]},
                    _snapshot("2026-09-24T15:00:00"),
                    ProviderOutcome(
                        "pytdx", "success", fetched_at="2026-09-26T11:59:59"
                    ),
                    60,
                )
            )

            self.assertEqual(
                "QUALITY_STALE",
                api._quality_failure_code(
                    "stock_snapshot",
                    {"codes": ["600519"]},
                    _snapshot("2026-09-24T15:00:00"),
                    ProviderOutcome(
                        "pytdx", "success", fetched_at="2026-09-26T11:58:00"
                    ),
                    60,
                ),
            )

    def test_quote_expires_after_trading_resumes(self):
        for now, expected in (
            ("2026-09-23T13:00:30", None),
            ("2026-09-23T13:01:01", "QUALITY_SNAPSHOT_STALE"),
        ):
            with self.subTest(now=now), patch.object(
                api, "_now_shanghai", return_value=_at(now)
            ):
                self.assertEqual(
                    expected,
                    api._quality_failure_code(
                        "stock_snapshot",
                        {"codes": ["600519"]},
                        _snapshot("2026-09-23T11:30:00"),
                        ProviderOutcome("pytdx", "success"),
                        60,
                    ),
                )

    def test_no_date_uses_verified_exchange_holidays(self):
        examples = (
            ("2026-09-25T16:00:00", "20260924"),
            ("2026-09-28T14:00:00", "20260924"),
            ("2026-09-28T15:01:00", "20260928"),
            ("2026-10-08T10:00:00", "20260930"),
        )
        for now, expected in examples:
            with self.subTest(now=now):
                self.assertEqual(expected, api._latest_completed_trade_date(_at(now)))

    def test_no_date_outside_calendar_coverage_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "CALENDAR_UNAVAILABLE"):
            api._latest_completed_trade_date(_at("2027-01-04T16:00:00"))

    def test_partial_index_batch_falls_through_to_complete_provider(self):
        requested = ["000001.SH", "399001.SZ", "399006.SZ"]
        primary = _Provider("stocktoday", {"items": [_index(requested[0])]})
        fallback = _Provider("eastmoney_index", {"items": [_index(c) for c in requested]})
        with tempfile.TemporaryDirectory() as tmp:
            state = ProviderState(Path(tmp) / "state.sqlite3")
            result = api._query_with(
                "index_kline",
                {"codes": requested, "period": "daily"},
                provider_loader={"stocktoday": primary, "eastmoney_index": fallback}.__getitem__,
                state_loader=lambda: state,
            )
        self.assertEqual("eastmoney_index", result["_meta"]["provider_used"])
        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("QUALITY_INDEX_INCOMPLETE", result["_meta"]["attempts"][0]["error_code"])
        self.assertEqual(1, fallback.calls)

    def test_index_batch_never_returns_partial_when_every_source_is_partial(self):
        requested = ["000001.SH", "399001.SZ", "399006.SZ"]
        providers = {
            name: _Provider(name, {"items": [_index(requested[0])]})
            for name in ("stocktoday", "eastmoney_index", "sina_index", "pytdx_index")
        }
        with tempfile.TemporaryDirectory() as tmp:
            state = ProviderState(Path(tmp) / "state.sqlite3")
            result = api._query_with(
                "index_kline",
                {"codes": requested, "period": "daily"},
                provider_loader=providers.__getitem__,
                state_loader=lambda: state,
            )
        self.assertEqual("error", result["_meta"]["status"])
        self.assertIsNone(result["_meta"]["provider_used"])
        self.assertTrue(all(
            attempt["error_code"] == "QUALITY_INDEX_INCOMPLETE"
            for attempt in result["_meta"]["attempts"]
        ))

    def test_index_batch_rejects_duplicate_and_wrong_code(self):
        params = {"codes": ["000001.SH", "399001.SZ"], "adjustment": "none", "period": "daily"}
        outcome = ProviderOutcome("stocktoday", "success")
        for data in (
            {"items": [_index("000001.SH"), _index("000001.SH")]},
            {"items": [_index("000001.SH"), _index("399006.SZ")]},
        ):
            with self.subTest(codes=[item["index_code"] for item in data["items"]]):
                self.assertIsNotNone(api._quality_failure_code("index_kline", params, data, outcome, 86400))

    def test_single_index_rejects_wrong_code(self):
        self.assertEqual(
            "QUALITY_INDEX_CODE_MISMATCH",
            api._quality_failure_code(
                "index_kline",
                {"index_code": "000001.SH", "adjustment": "none", "period": "daily"},
                _index("399001.SZ"),
                ProviderOutcome("stocktoday", "success"),
                86400,
            ),
        )
