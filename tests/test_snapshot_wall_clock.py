"""Public snapshot wall-clock contract; all providers are offline fakes."""

import copy
import socket
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from ym_stock_data import query
from ym_stock_data import api
from ym_stock_data.contracts import TZ_SHANGHAI, validate_result
from ym_stock_data.provider_policy import CompiledPolicy
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.providers.stocktoday import StockTodayProvider


def at(hms):
    return datetime.fromisoformat(f"2026-11-09T{hms}").replace(tzinfo=TZ_SHANGHAI)


def row(stamp, code="600519"):
    return {"code": code, "price": 10.0, "last_close": 9.0, "open": 9.2,
            "high": 10.2, "low": 9.1, "volume": 100.0, "amount": 1000.0,
            "quote_time": stamp.isoformat() if isinstance(stamp, datetime) else stamp}


class FakeProvider:
    def __init__(self, name, data=None, status="success"):
        self.name, self.data, self.status = name, data, status
        self.calls = []

    def call(self, intent, params):
        self.calls.append((intent, copy.deepcopy(params)))
        return ProviderOutcome(self.name, self.status, data=copy.deepcopy(self.data),
                               error_code="UPSTREAM_ERROR" if self.status != "success" else None)


class SnapshotWallClockTests(unittest.TestCase):
    def setUp(self):
        self.now = at("13:00:00")
        self.primary = FakeProvider("stocktoday")
        self.fallback = FakeProvider("tencent")
        self.providers = {"stocktoday": self.primary, "tencent": self.fallback}
        for name, value in (("_now_shanghai", lambda: self.now),
                            ("_provider_for", lambda name: self.providers[name]),
                            ("_provider_state", lambda: type("State", (), {"active_breaker": lambda *args: None})()),
                            ("load_compiled_policy", lambda: CompiledPolicy(None, "inactive", None))):
            mock = patch.object(api, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def run_query(self, primary, fallback=None, *, strict=True, codes=None, **extra):
        self.primary.data = primary
        self.fallback.data = fallback
        self.fallback.status = "success" if fallback is not None else "provider_error"
        params = {"codes": codes or ["600519"]}
        if strict:
            params.update(quote_age_policy="wall_clock", max_quote_age_seconds=180)
        result = query("stock_snapshot", **params, **extra)
        validate_result(result)
        return result

    def test_lunch_old_quote_falls_back_at_every_opening_minute(self):
        for minute in (0, 1, 2):
            with self.subTest(minute=minute):
                self.now = at(f"13:0{minute}:00")
                old = row(at("11:34:00"))
                fresh = row(self.now - timedelta(seconds=1))
                result = self.run_query({"600519": old}, {"600519": fresh})
                self.assertEqual(fresh["quote_time"], result["data"]["600519"]["quote_time"])
                self.assertEqual("tencent", result["_meta"]["provider_used"])
                self.assertEqual("fresh", result["_meta"]["freshness"]["status"])
                self.assertEqual(1, result["_meta"]["freshness"]["age_sec"])
                self.assertEqual(180, result["_meta"]["freshness"]["max_age_sec"])
                self.assertEqual("wall_clock", result["_meta"]["freshness"]["age_policy"])
                self.assertEqual(fresh["quote_time"], result["_meta"]["data_as_of"])
                self.assertEqual(1, result["data"]["600519"]["lag_sec"])
                self.assertNotEqual(result["_meta"]["data_as_of"], result["_meta"]["fetched_at"])
        self.assertEqual(3, len(self.fallback.calls))

    def test_no_fresh_fallback_never_returns_old_quote(self):
        for fallback in (None, {"600519": row(at("11:34:00"))}):
            with self.subTest(fallback=fallback):
                result = self.run_query({"600519": row(at("11:34:00"))}, fallback)
                self.assertNotIn("600519", result["data"] or {})
                self.assertNotEqual("success", result["_meta"]["status"])
                self.assertNotEqual("fresh", result["_meta"]["freshness"]["status"])
                self.assertTrue(result["_meta"]["quality"]["reason_codes"])

    def test_exact_limit_kept_but_one_microsecond_over_rejected(self):
        self.now = at("13:05:00")
        for age, fill_count in ((180, 0), (180.000001, 1)):
            with self.subTest(age=age):
                self.fallback.calls.clear()
                result = self.run_query({"600519": row(self.now - timedelta(seconds=age))},
                                        {"600519": row(self.now)})
                self.assertEqual(fill_count, len(self.fallback.calls))
                self.assertEqual(180 if not fill_count else 0,
                                 result["_meta"]["freshness"]["age_sec"])

    def test_invalid_time_missing_time_future_and_previous_day_rejected(self):
        for stamp in (None, "bad", self.now + timedelta(microseconds=1),
                      at("13:00:00") - timedelta(days=3)):
            with self.subTest(stamp=stamp):
                result = self.run_query({"600519": row(stamp)}, {"600519": row(stamp)})
                self.assertNotIn("600519", result["data"] or {})
                self.assertNotEqual("fresh", result["_meta"]["freshness"]["status"])

    def test_strict_rejects_date_only_incomplete_or_nonstandard_time_on_all_routes(self):
        self.now = at("00:01:00")
        values = ("2026-11-09", "20261109", "2026-11-09T", "2026-11-09T00",
                  "2026-11-09T00:00", "2026-11-09X00:00:30", "2026-11-09\n00:00:30",
                  "2026/11/09 00:00:30", "2026-W46-1T00:00:30", "20261109T000030")
        for stamp in values:
            with self.subTest(stamp=stamp, route="whole_fallback"):
                result = self.run_query({"600519": row(stamp)}, {"600519": row(stamp)})
                self.assertNotIn("600519", result["data"] or {})
                self.assertIsNone(result["_meta"]["data_as_of"])
                self.assertNotEqual("fresh", result["_meta"]["freshness"]["status"])
            with self.subTest(stamp=stamp, route="per_code_fill"):
                result = self.run_query(
                    {"600519": row(self.now), "000001": row(stamp, "000001")},
                    {"000001": row(stamp, "000001")}, codes=["600519", "000001"])
                self.assertNotIn("000001", result["data"])
                self.assertEqual(["000001"], result["_meta"]["quality"]["missing"])
                self.assertEqual(self.now.isoformat(), result["_meta"]["data_as_of"])

    def test_strict_full_datetime_forms_share_quote_time_and_metadata_semantics(self):
        self.now = at("00:01:00")
        for stamp in ("2026-11-09T00:00:30", "2026-11-09 00:00:30",
                      "2026-11-09T00:00:30+08:00", "2026-11-08T16:00:30Z"):
            with self.subTest(stamp=stamp):
                result = self.run_query({"600519": row(stamp)})
                self.assertEqual(stamp, result["data"]["600519"]["quote_time"])
                self.assertEqual(at("00:00:30").isoformat(), result["_meta"]["data_as_of"])
                self.assertEqual(30, result["data"]["600519"]["lag_sec"])
                self.assertEqual(30, result["_meta"]["freshness"]["age_sec"])
                self.assertEqual("fresh", result["_meta"]["freshness"]["status"])

    def test_default_date_only_quote_parsing_remains_unchanged(self):
        self.now = at("00:01:00")
        result = self.run_query({"600519": row("2026-11-09")}, strict=False, source="stocktoday")
        self.assertEqual("2026-11-09", result["data"]["600519"]["quote_time"])
        self.assertEqual(at("15:00:00").isoformat(), result["_meta"]["data_as_of"])

    def test_partial_fill_drops_unusable_row_and_counts_gap(self):
        result = self.run_query(
            {"600519": row(self.now), "000001": row(at("11:34:00"), "000001")},
            {"000001": row(at("11:34:00"), "000001")}, codes=["600519", "000001"])
        self.assertEqual(["000001"], self.fallback.calls[0][1]["codes"])
        self.assertNotIn("000001", result["data"])
        quality = result["_meta"]["quality"]
        self.assertEqual(["000001"], quality["missing"])
        self.assertEqual(1, quality["missing_count"])
        self.assertEqual(0.5, quality["coverage"])
        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertIn("QUALITY_SNAPSHOT_STALE", quality["reason_codes"])

    def test_mixed_batch_fills_only_old_row_with_fresh_row(self):
        self.now = at("13:02:00")
        result = self.run_query(
            {"600519": row(self.now), "000001": row(at("11:34:00"), "000001")},
            {"000001": row(self.now - timedelta(seconds=3), "000001")}, codes=["600519", "000001"])
        self.assertEqual(["000001"], self.fallback.calls[0][1]["codes"])
        self.assertEqual(["000001"], result["_meta"]["filled_by_fallback"])
        self.assertEqual("tencent", result["data"]["000001"]["source"])
        self.assertEqual(3, result["_meta"]["freshness"]["age_sec"])
        self.assertEqual(1.0, result["_meta"]["quality"]["coverage"])

    def test_fallback_time_uses_receipt_clock_and_drops_rows_expiring_during_call(self):
        self.now = at("13:05:00")
        initial = self.now
        original = self.fallback.call

        def delayed(intent, params):
            self.now = initial + timedelta(seconds=2)
            return original(intent, params)

        self.fallback.call = delayed
        result = self.run_query(
            {"600519": row(initial - timedelta(seconds=179)),
             "000001": row(at("11:34:00"), "000001")},
            {"000001": row(initial + timedelta(seconds=1), "000001")}, codes=["600519", "000001"])
        self.assertIn("000001", result["data"])
        self.assertNotIn("600519", result["data"])
        self.assertEqual(1, result["data"]["000001"]["lag_sec"])
        self.assertEqual(["600519"], result["_meta"]["quality"]["missing"])
        self.assertEqual("degraded", result["_meta"]["status"])

    def test_final_recheck_removes_expired_fallback_from_fill_receipt(self):
        initial = at("13:05:00")
        accepted = initial + timedelta(seconds=1)
        final = accepted + timedelta(microseconds=1)
        self.primary.data = {"600519": row(initial), "000001": row(at("11:34:00"), "000001")}
        self.fallback.data = {"000001": row(initial - timedelta(seconds=179), "000001")}
        with patch.object(api, "_now_shanghai", side_effect=[initial, initial, accepted, final, final]):
            result = query("stock_snapshot", codes=["600519", "000001"],
                           quote_age_policy="wall_clock", max_quote_age_seconds=180)
        self.assertNotIn("000001", result["data"])
        self.assertEqual([], result["_meta"]["filled_by_fallback"])
        fill = result["_meta"]["fill_attempt"]
        self.assertEqual([], fill["filled"])
        self.assertEqual("success", fill["status"])
        self.assertEqual(["000001"], fill["requested"])
        self.assertEqual("QUALITY_SNAPSHOT_STALE", fill["rejected"]["000001"])
        self.assertNotIn("FILLED_BY_FALLBACK", result["_meta"]["quality"]["reason_codes"])

    def test_final_recheck_keeps_only_surviving_fallback_codes(self):
        initial = at("13:05:00")
        accepted = initial + timedelta(seconds=1)
        final = accepted + timedelta(microseconds=1)
        self.primary.data = {"600519": row(initial), "000001": row(at("11:34:00"), "000001"),
                             "300001": row(at("11:34:00"), "300001")}
        self.fallback.data = {"000001": row(initial - timedelta(seconds=179), "000001"),
                              "300001": row(initial, "300001")}
        with patch.object(api, "_now_shanghai", side_effect=[initial, initial, accepted, final, final]):
            result = query("stock_snapshot", codes=["600519", "000001", "300001"],
                           quote_age_policy="wall_clock", max_quote_age_seconds=180)
        self.assertEqual(["300001"], result["_meta"]["filled_by_fallback"])
        self.assertEqual(["300001"], result["_meta"]["fill_attempt"]["filled"])
        self.assertEqual(["000001"], result["_meta"]["quality"]["missing"])
        self.assertIn("FILLED_BY_FALLBACK", result["_meta"]["quality"]["reason_codes"])
        self.assertEqual("QUALITY_SNAPSHOT_STALE", result["_meta"]["fill_attempt"]["rejected"]["000001"])

    def test_real_adapter_preserves_raw_source_time_for_strict_validation(self):
        self.now = at("13:00:30")
        clock = type("Clock", (), {"time": lambda _: self.now.timestamp(), "monotonic": lambda _: 0.0})()
        provider = StockTodayProvider(token_loader=lambda: self.fail("no authentication allowed"),
                                      budget=object(), clock=clock)
        calls = []
        source = {}
        additional = []

        def request(name, params):
            calls.append((name, params.copy()))
            return ProviderOutcome("stocktoday", "success", data={"items": [source.copy(), *additional],
                "_stocktoday": {"status": "fresh", "missing_codes": []}})

        provider._request_table = request
        self.providers["stocktoday"] = provider
        base = {"ts_code": "600519.SH", "close": 10.0, "pre_close": 9.0,
                "open": 9.2, "high": 10.2, "low": 9.1, "vol": 100.0, "amount": 1000.0}
        for field in ("updated_at", "trade_time"):
            for stamp in ("2026-11-09T13:00+08:00", "2026-11-09X13:00:00"):
                with self.subTest(field=field, stamp=stamp):
                    source.clear()
                    source.update(base, **{field: stamp})
                    default = query("stock_snapshot", codes=["600519"], source="stocktoday")
                    self.assertEqual("2026-11-09T13:00:00+08:00", default["data"]["600519"]["quote_time"])
                    strict = query("stock_snapshot", codes=["600519"], source="stocktoday",
                                   quote_age_policy="wall_clock", max_quote_age_seconds=180)
                    self.assertNotIn("600519", strict["data"] or {})
                    self.assertEqual("error", strict["_meta"]["status"])
                    self.assertIsNone(strict["_meta"]["data_as_of"])
        source.clear()
        source.update(base, updated_at="2026-11-09 13:00:00")
        strict = query("stock_snapshot", codes=["600519"], source="stocktoday",
                       quote_age_policy="wall_clock", max_quote_age_seconds=180)
        self.assertEqual("2026-11-09 13:00:00", strict["data"]["600519"]["quote_time"])
        self.assertEqual(30, strict["_meta"]["freshness"]["age_sec"])
        self.assertEqual("fresh", strict["_meta"]["freshness"]["status"])
        additional.append({**base, "ts_code": "000001.SZ", "updated_at": "2026-11-09X13:00:00"})
        self.fallback.data = {"000001": row(self.now, "000001")}
        strict = query("stock_snapshot", codes=["600519", "000001"],
                       quote_age_policy="wall_clock", max_quote_age_seconds=180)
        self.assertEqual("2026-11-09 13:00:00", strict["data"]["600519"]["quote_time"])
        self.assertEqual("tencent", strict["data"]["000001"]["source"])
        self.assertEqual(["000001"], strict["_meta"]["filled_by_fallback"])
        self.assertEqual("2026-11-09T13:00:00+08:00", strict["_meta"]["data_as_of"])
        additional.clear()
        source["updated_at"] = "2026-11-09 11:34:00"
        self.now = at("11:35:00")
        strict = query("stock_snapshot", codes=["600519"], source="stocktoday",
                       quote_age_policy="wall_clock", max_quote_age_seconds=180)
        self.assertEqual("2026-11-09 11:34:00", strict["data"]["600519"]["quote_time"])
        self.assertEqual("2026-11-09T11:34:00+08:00", strict["_meta"]["data_as_of"])
        self.assertEqual(60, strict["_meta"]["freshness"]["age_sec"])
        for name, params in calls:
            self.assertEqual("rt_k", name)
            self.assertEqual({"ts_code"}, set(params))
            self.assertIn(params["ts_code"], ("600519.SH", "600519.SH,000001.SZ"))

    def test_explicit_stocktoday_cannot_bypass_strict_quote_age(self):
        result = self.run_query({"600519": row(at("11:34:00"))}, source="stocktoday")
        self.assertNotIn("600519", result["data"] or {})
        self.assertEqual("error", result["_meta"]["status"])
        self.assertEqual([], self.fallback.calls)
        result = self.run_query(
            {"600519": row(self.now), "000001": row(at("11:34:00"), "000001")},
            source="stocktoday", codes=["600519", "000001"])
        self.assertNotIn("000001", result["data"] or {})
        self.assertEqual("error", result["_meta"]["status"])

    def test_missing_quote_time_is_not_synthesized(self):
        missing = row(self.now)
        missing.pop("quote_time")
        result = self.run_query({"600519": missing}, {"600519": missing})
        self.assertNotIn("600519", result["data"] or {})
        self.assertIsNone(result["_meta"]["data_as_of"])

    def test_microseconds_are_preserved_in_strict_data_time(self):
        self.now = at("13:05:00").replace(microsecond=123456)
        stamp = self.now - timedelta(seconds=180)
        result = self.run_query({"600519": row(stamp)})
        self.assertEqual(stamp.isoformat(), result["_meta"]["data_as_of"])

    def test_fresh_primary_does_not_add_requests_or_leak_params(self):
        result = self.run_query({"600519": row(self.now)})
        self.assertEqual([], self.fallback.calls)
        self.assertEqual("success", result["_meta"]["status"])
        for _, params in self.primary.calls:
            self.assertNotIn("quote_age_policy", params)
            self.assertNotIn("max_quote_age_seconds", params)

    def test_lunch_timestamp_is_not_clamped_in_strict_metadata(self):
        self.now = at("11:35:00")
        result = self.run_query({"600519": row(at("11:34:00"))})
        self.assertEqual(at("11:34:00").isoformat(), result["_meta"]["data_as_of"])
        self.assertEqual(60, result["data"]["600519"]["lag_sec"])
        self.assertEqual(60, result["_meta"]["freshness"]["age_sec"])

    def test_zero_limit_requires_exact_source_time(self):
        self.primary.data = {"600519": row(self.now - timedelta(microseconds=1))}
        self.fallback.data = {"600519": row(self.now)}
        result = query("stock_snapshot", codes=["600519"], quote_age_policy="wall_clock", max_quote_age_seconds=0)
        self.assertEqual(1, len(self.fallback.calls))
        self.assertEqual(0, result["_meta"]["freshness"]["age_sec"])

    def test_default_preserves_lunch_session_age_and_route_limit(self):
        result = self.run_query({"600519": row(at("11:34:00"))}, strict=False)
        self.assertEqual([], self.fallback.calls)
        self.assertEqual(at("11:34:00").isoformat(), result["data"]["600519"]["quote_time"])
        self.assertEqual(0, result["data"]["600519"]["lag_sec"])
        self.assertEqual(600, result["_meta"]["freshness"]["max_age_sec"])
        self.assertNotIn("age_policy", result["_meta"]["freshness"])

    def test_invalid_parameters_rejected_before_provider_call(self):
        cases = [{"quote_age_policy": "wall_clock"}, {"max_quote_age_seconds": 180},
                 {"quote_age_policy": "session", "max_quote_age_seconds": 180},
                 {"quote_age_policy": True, "max_quote_age_seconds": 180}]
        cases += [{"quote_age_policy": "wall_clock", "max_quote_age_seconds": value}
                  for value in (True, -1, float("nan"), float("inf"), 180.0, "180", None)]
        for params in cases:
            with self.subTest(params=params), self.assertRaises(ValueError):
                query("stock_snapshot", codes=["600519"], **params)
        for intent in ("realtime_market", "stock_kline"):
            with self.subTest(intent=intent), self.assertRaises(ValueError):
                query(intent, quote_age_policy="wall_clock", max_quote_age_seconds=180)
        self.assertEqual([], self.primary.calls)
        self.assertEqual([], self.fallback.calls)


if __name__ == "__main__":
    unittest.main()
