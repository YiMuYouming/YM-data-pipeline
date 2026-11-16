"""R042: strict automatic whole-route fallback keeps only usable rows."""
import copy
import socket
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from ym_stock_data import api, query
from ym_stock_data.contracts import TZ_SHANGHAI, validate_result
from ym_stock_data.provider_policy import CompiledPolicy
from ym_stock_data.providers.base import ProviderOutcome

NOW = datetime(2026, 11, 16, 10, 6, tzinfo=TZ_SHANGHAI)
CODES = ["600000", "605255"]


def row(code, stamp=NOW):
    return {"code": code, "price": 10., "last_close": 9., "open": 9.2,
            "high": 10.2, "low": 9.1, "volume": 100., "amount": 1000.,
            "quote_time": stamp.isoformat(), "source": "tencent"}


class Provider:
    def __init__(self, name, status, data):
        self.name, self.status, self.data = name, status, data
        self.calls = []

    def call(self, intent, params):
        self.calls.append((intent, copy.deepcopy(params)))
        return ProviderOutcome(self.name, self.status, data=copy.deepcopy(self.data),
                               fetched_at=NOW.isoformat(), error_code="OTHER" if self.status != "success" else None)


class SnapshotFallbackPartialTests(unittest.TestCase):
    def setUp(self):
        self.primary = Provider("stocktoday", "provider_error", None)
        self.fallback = Provider("tencent", "success", {})
        self.now = NOW
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

    def run_query(self, data, *, strict=True, **kwargs):
        self.fallback.data = data
        params = {"codes": CODES, **kwargs}
        if strict:
            params.update(quote_age_policy="wall_clock", max_quote_age_seconds=180)
        result = query("stock_snapshot", **params)
        validate_result(result)
        return result

    def test_partial_fallback_preserves_good_quote_and_all_provenance(self):
        bad = row(CODES[1])
        del bad["amount"]
        good = row(CODES[0], NOW - timedelta(seconds=1))
        result = self.run_query({CODES[0]: good, CODES[1]: bad})
        self.assertEqual({CODES[0]}, set(result["data"] or {}))
        meta, quality = result["_meta"], result["_meta"]["quality"]
        self.assertEqual("degraded", meta["status"])
        self.assertEqual("partial", quality["status"])
        self.assertEqual((2, 1, 1, .5), tuple(quality[k] for k in
                         ("requested_count", "returned_count", "missing_count", "coverage")))
        self.assertEqual([CODES[1]], quality["missing"])
        self.assertIn("QUALITY_SNAPSHOT_FIELDS", quality["reason_codes"])
        self.assertEqual("QUALITY_SNAPSHOT_FIELDS", meta["snapshot_rejected"][CODES[1]])
        self.assertEqual("tencent", meta["provider_used"])
        self.assertEqual([("stocktoday", "provider_error"), ("tencent", "success")],
                         [(a["provider"], a["status"]) for a in meta["attempts"]])
        self.assertEqual(good["quote_time"], result["data"][CODES[0]]["quote_time"])
        self.assertEqual(good["quote_time"], meta["data_as_of"])
        self.assertEqual(NOW.isoformat(), meta["fetched_at"])
        self.assertNotIn("filled_by_fallback", meta)
        self.assertEqual((1, 1), (len(self.primary.calls), len(self.fallback.calls)))

    def test_bad_fields_codes_dates_future_staleness_drop_only_bad_row(self):
        invalid = [{k: v for k, v in row(CODES[1]).items() if k != field}
                   for field in ("price", "last_close", "open", "high", "low", "volume", "amount", "quote_time")]
        invalid += [{**row(CODES[1]), key: value} for key, value in
                    (("price", 0), ("price", float("nan")), ("open", -1),
                     ("volume", -1), ("amount", float("inf")), ("code", CODES[0]))]
        invalid += [row(CODES[1], NOW + delta) for delta in
                    (timedelta(days=-1), timedelta(microseconds=1), timedelta(seconds=-180, microseconds=-1))]
        invalid += [None]
        for bad in invalid:
            with self.subTest(bad=bad):
                result = self.run_query({CODES[0]: row(CODES[0]), CODES[1]: bad})
                self.assertEqual({CODES[0]}, set(result["data"] or {}))
                self.assertEqual([CODES[1]], result["_meta"]["quality"]["missing"])

    def test_equal_age_limit_is_accepted(self):
        result = self.run_query({code: row(code, NOW - timedelta(seconds=180)) for code in CODES})
        self.assertEqual(set(CODES), set(result["data"]))
        self.assertEqual(180, result["_meta"]["freshness"]["age_sec"])

    def test_rows_expiring_at_final_check_cannot_form_fresh_empty_success(self):
        self.fallback.data = {code: row(code, NOW - timedelta(seconds=180)) for code in CODES}
        with patch.object(api, "_now_shanghai", side_effect=[NOW, NOW + timedelta(microseconds=1),
                                                            NOW + timedelta(microseconds=1)]):
            result = query("stock_snapshot", codes=CODES, quote_age_policy="wall_clock", max_quote_age_seconds=180)
        self.assertFalse(result["data"])
        self.assertEqual("error", result["_meta"]["status"])
        self.assertNotEqual("fresh", result["_meta"]["freshness"]["status"])
        self.assertEqual(0, result["_meta"]["quality"]["returned_count"])

    def test_all_bad_still_fails(self):
        result = self.run_query({code: row(code, NOW - timedelta(days=1)) for code in CODES})
        self.assertFalse(result["data"])
        self.assertEqual("error", result["_meta"]["status"])
        self.assertNotEqual("normal", result["_meta"]["quality"]["status"])
        self.assertNotEqual("fresh", result["_meta"]["freshness"]["status"])
        quality = result["_meta"]["quality"]
        self.assertEqual((2, 0, 2, 0.), tuple(quality[k] for k in
                         ("requested_count", "returned_count", "missing_count", "coverage")))
        self.assertEqual(set(CODES), set(result["_meta"]["snapshot_rejected"]))

    def test_only_one_fallback_row_expires_during_final_check(self):
        self.fallback.data = {CODES[0]: row(CODES[0]),
                              CODES[1]: row(CODES[1], NOW - timedelta(seconds=180))}
        final = NOW + timedelta(microseconds=1)
        with patch.object(api, "_now_shanghai", side_effect=[NOW, final, final]):
            result = query("stock_snapshot", codes=CODES, quote_age_policy="wall_clock", max_quote_age_seconds=180)
        self.assertEqual({CODES[0]}, set(result["data"]))
        self.assertEqual("partial", result["_meta"]["quality"]["status"])
        self.assertEqual("QUALITY_SNAPSHOT_STALE", result["_meta"]["snapshot_rejected"][CODES[1]])

    def test_other_intent_kline_uses_existing_fallback_without_snapshot_markers(self):
        self.fallback.data = {"code": CODES[0], "adjustment": "none", "volume_unit": "share",
                              "amount_unit": "CNY", "bars": [{"datetime": "2026-11-13", "open": 9,
                                  "high": 11, "low": 8, "close": 10, "volume": 100, "amount": 1000}]}
        result = query("stock_kline", code=CODES[0], count=1, period="daily", adjustment="none")
        self.assertEqual("tencent", result["_meta"]["provider_used"])
        self.assertEqual("stock_kline", self.fallback.calls[-1][0])
        self.assertNotIn("snapshot_rejected", result["_meta"])

    def test_all_good_keeps_all_rows_without_fill_receipt(self):
        result = self.run_query({code: row(code) for code in CODES})
        self.assertEqual(set(CODES), set(result["data"]))
        self.assertEqual(1, result["_meta"]["quality"]["coverage"])
        self.assertNotIn("filled_by_fallback", result["_meta"])

    def test_non_strict_and_explicit_single_source_keep_batch_rejection(self):
        data = {CODES[0]: row(CODES[0]), CODES[1]: {"price": 10}}
        for params in ({"strict": False}, {"source": "stocktoday"}):
            with self.subTest(params=params):
                self.primary.status = "success" if params.get("source") else "provider_error"
                self.primary.data = data
                result = self.run_query(data, **params)
                self.assertFalse(result["data"])
                self.assertEqual("error", result["_meta"]["status"])


if __name__ == "__main__":
    unittest.main()
