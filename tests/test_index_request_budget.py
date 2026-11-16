"""Optional index request budget: offline public boundary and worker lifetime."""

import json
import socket
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from ym_stock_data import api, query
from ym_stock_data.contracts import TZ_SHANGHAI, validate_result
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.routing import route_for


class IndexRequestBudgetTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 16, 10, 0, tzinfo=TZ_SHANGHAI)
        self.calls = []
        self.releases = []
        self.workers = []
        self.actions = {}
        self.providers = ("tencent", "eastmoney_index")
        self.state = SimpleNamespace(active_breaker=lambda name: None)
        for name, value in (
            ("_now_shanghai", lambda: self.now),
            ("_provider_for", self.loader),
            ("_provider_state", lambda: self.state),
            ("load_compiled_policy", lambda: SimpleNamespace(route=lambda intent, params:
                replace(route_for(intent, params), providers=self.providers))),
        ):
            mock = patch.object(api, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.addCleanup(self.release_workers)

    def release_workers(self):
        for event in self.releases:
            event.set()
        for worker in self.workers:
            worker.join(1)
            self.assertFalse(worker.is_alive(), "test worker leaked")

    def loader(self, name):
        if threading.current_thread() is not threading.main_thread():
            self.workers.append(threading.current_thread())
        action = self.actions.get((name, "load"))
        if action:
            action()
        return SimpleNamespace(call=lambda intent, params: self.call(name, intent, params))

    def call(self, name, intent, params):
        self.calls.append((name, intent, dict(params), threading.current_thread()))
        if threading.current_thread() is not threading.main_thread():
            self.workers.append(threading.current_thread())
        action = self.actions.get(name)
        if action:
            return action(intent, params)
        if intent == "index_kline":
            data = {"index_code": params["index_code"], "adjustment": "none",
                    "volume_unit": "share", "amount_unit": "CNY",
                    "bars": [{"datetime": "2026-11-16 10:00:00", "open": 3190,
                              "high": 3210, "low": 3180, "close": 3200,
                              "volume": 100, "amount": 320000}]}
        else:
            data = {"上证指数": 3200, "深证指数": 10000, "创业指数": 2100,
                    "quote_time": self.now.isoformat()}
        return ProviderOutcome(name, "success", data)

    def block(self, name, *, construction=False):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def action(*args):
            self.workers.append(threading.current_thread())
            entered.set()
            if not release.wait(1):
                raise AssertionError("test release missing")
            return ProviderOutcome(name, "success", {"上证指数": 1111})

        self.actions[(name, "load") if construction else name] = action
        return entered, release

    def request(self, intent="realtime_market", budget=.04, **params):
        if intent == "index_kline":
            params = {"index_code": "000001.SH", "period": "1m", "count": 1, **params}
        result = query(intent, request_budget_seconds=budget, **params)
        validate_result(result)
        return result

    def test_valid_parameter_is_consumed_for_both_index_intents(self):
        for intent in ("realtime_market", "index_kline"):
            self.assertIn("request_budget_seconds", api._ALLOWED_PARAMS[intent])
            for budget in (1, .5, 1e100):
                with self.subTest(intent=intent, budget=budget):
                    result = self.request(intent, budget)
                    self.assertEqual("tencent", result["_meta"]["provider_used"])
                    self.assertNotIn("request_budget_seconds", self.calls[-1][2])
                    self.assertIsNot(threading.main_thread(), self.calls[-1][3])

    def test_invalid_budget_and_other_intents_rejected_without_provider(self):
        for budget in (True, False, None, "1", [], {}, 0, -1, float("inf"), float("nan"), 10 ** 400):
            with self.subTest(budget=type(budget).__name__), self.assertRaises(ValueError):
                self.request(budget=budget)
        for intent in api._ALLOWED_PARAMS.keys() - {"realtime_market", "index_kline"}:
            with self.subTest(intent=intent), self.assertRaises(ValueError):
                query(intent, request_budget_seconds=1)
        self.assertEqual([], self.calls)

    def test_call_and_construction_are_bounded_and_do_not_start_fallback(self):
        for construction in (False, True):
            with self.subTest(construction=construction):
                entered, release = self.block("tencent", construction=construction)
                start = time.monotonic()
                result = self.request()
                self.assertLess(time.monotonic() - start, .25)
                self.assertTrue(entered.is_set())
                self.assertIsNone(result["data"])
                self.assertIsNone(result["_meta"]["provider_used"])
                self.assertEqual("QUERY_BUDGET_EXCEEDED", result["_meta"]["attempts"][0]["error_code"])
                self.assertFalse(any(c[0] == "eastmoney_index" for c in self.calls))
                release.set()
                self.release_workers()
                self.actions.clear()
                self.calls.clear()

    def test_busy_slot_ignores_params_dates_and_old_result_then_recovers(self):
        entered, release = self.block("tencent")
        self.providers = ("tencent",)
        timed_out = self.request("index_kline")
        self.assertTrue(entered.is_set())
        self.assertIsNone(timed_out["data"])
        for code, end in (("000001.SH", "20261116"), ("399001.SZ", "20261117")):
            self.now += timedelta(minutes=1)
            result = self.request("index_kline", index_code=code, end_date=end)
            self.assertEqual("REQUEST_INFLIGHT", result["_meta"]["attempts"][0]["error_code"])
            self.assertIsNone(result["data"])
        self.assertEqual(1, len(self.calls))
        release.set()
        self.release_workers()
        self.actions.clear()
        recovered = self.request("index_kline")
        self.assertEqual(3200, recovered["data"]["bars"][0]["close"])
        self.assertEqual(2, len(self.calls))

    def test_concurrent_requests_do_not_wait_or_multiply_workers(self):
        entered, release = self.block("tencent")
        self.providers = ("tencent",)
        results = []
        caller = threading.Thread(target=lambda: results.append(self.request(budget=.15)))
        caller.start()
        self.workers.append(caller)
        self.assertTrue(entered.wait(.5))
        start = time.monotonic()
        busy = self.request(budget=.5)
        self.assertLess(time.monotonic() - start, .1)
        self.assertEqual("REQUEST_INFLIGHT", busy["_meta"]["attempts"][0]["error_code"])
        self.assertEqual(1, len(self.calls))
        release.set()
        caller.join(1)

    def test_slot_is_scoped_to_intent_and_cleanup_has_identity_protection(self):
        entered, release = self.block("tencent")
        self.providers = ("tencent",)
        self.request()
        self.assertTrue(entered.is_set())
        self.actions.clear()
        self.assertEqual("tencent", self.request("index_kline")["_meta"]["provider_used"])
        key, newer = ("tencent", "realtime_market"), object()
        with api._request_inflight_lock:
            api._request_inflight[key] = newer
        try:
            release.set()
            self.release_workers()
            with api._request_inflight_lock:
                self.assertIs(newer, api._request_inflight[key])
        finally:
            with api._request_inflight_lock:
                api._request_inflight.pop(key, None)

    def test_fast_failure_uses_remaining_budget_and_fast_fallback_succeeds(self):
        self.actions["tencent"] = lambda *args: ProviderOutcome("tencent", "provider_error", error_code="UPSTREAM")
        result = self.request(budget=.2)
        self.assertEqual("eastmoney_index", result["_meta"]["provider_used"])
        self.assertEqual(["tencent", "eastmoney_index"], [c[0] for c in self.calls])
        self.assertEqual("degraded", result["_meta"]["status"])

    def test_sources_share_one_deadline_and_never_start_third_after_exhaustion(self):
        elapsed = [100.0]
        self.providers = ("tencent", "eastmoney_index", "sina")

        def first(*args):
            elapsed[0] += 2
            return ProviderOutcome("tencent", "provider_error", error_code="UPSTREAM")

        def second(*args):
            elapsed[0] += 4
            return ProviderOutcome("eastmoney_index", "success", {"上证指数": 1111})

        self.actions.update(tencent=first, eastmoney_index=second)
        with patch.object(api.time, "monotonic", side_effect=lambda: elapsed[0]):
            result = self.request(budget=5)
        self.assertIsNone(result["data"])
        self.assertEqual(["tencent", "eastmoney_index"], [c[0] for c in self.calls])
        self.assertEqual("QUERY_BUDGET_EXCEEDED", result["_meta"]["attempts"][1]["error_code"])
        self.assertEqual({"provider": "sina", "kind": "request_budget", "error_code": "QUERY_BUDGET_EXCEEDED",
                          "requested": False}, result["_meta"]["attempt_diagnostics"][-1])

    def test_budget_includes_quality_and_final_envelope_work(self):
        for phase in ("normalize_success", "_data_as_of", "build_result"):
            with self.subTest(phase=phase):
                elapsed = [100.0]
                original = getattr(api, phase)

                def consume(*args, original=original, elapsed=elapsed, **kwargs):
                    result = original(*args, **kwargs)
                    elapsed[0] = 106
                    return result

                with patch.object(api.time, "monotonic", side_effect=lambda elapsed=elapsed: elapsed[0]), \
                        patch.object(api, phase, side_effect=consume):
                    result = self.request(budget=5)
                self.assertIsNone(result["data"])
                self.assertIsNone(result["_meta"]["provider_used"])
                self.assertEqual("QUERY_BUDGET_EXCEEDED", result["_meta"]["attempts"][-1]["error_code"])

    def test_busy_timeout_do_not_write_global_breakers_or_block_default_read(self):
        entered, release = self.block("tencent")
        self.providers = ("tencent",)
        writes = []
        self.state.record_failure = lambda *args, **kwargs: writes.append(kwargs)
        self.state.record_success = lambda *args, **kwargs: writes.append(kwargs)
        self.request()
        self.assertTrue(entered.is_set())
        busy = self.request()
        self.assertEqual(False, busy["_meta"]["attempt_diagnostics"][0]["requested"])
        self.actions.clear()
        default = query("realtime_market")
        self.assertEqual("tencent", default["_meta"]["provider_used"])
        self.assertIs(threading.main_thread(), self.calls[-1][3])
        self.assertEqual([], writes)
        release.set()

    def test_source_and_start_exceptions_clear_slot_and_hide_secret(self):
        self.providers = ("tencent",)
        for construction in (False, True):
            def fail(*args):
                raise RuntimeError("fixture_password=SECRET")

            self.actions[("tencent", "load") if construction else "tencent"] = fail
            result = self.request()
            self.assertNotIn("SECRET", json.dumps(result))
            self.actions.clear()
            self.assertEqual("tencent", self.request()["_meta"]["provider_used"])
        with patch.object(api.threading.Thread, "start", side_effect=RuntimeError("SECRET")):
            result = self.request()
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual("REQUEST_START_FAILED", result["_meta"]["attempts"][0]["error_code"])
        self.assertEqual(False, result["_meta"]["attempt_diagnostics"][0]["requested"])
        self.assertEqual("tencent", self.request()["_meta"]["provider_used"])

    def test_default_is_synchronous_and_compare_does_not_use_request_guard(self):
        self.providers = ("tencent",)
        result = query("realtime_market")
        self.assertEqual("tencent", result["_meta"]["provider_used"])
        self.assertIs(threading.main_thread(), self.calls[-1][3])
        with patch.object(api, "_bounded_provider_call", wraps=api._bounded_provider_call) as bounded:
            query("index_intraday_compare", period="15m")
        self.assertEqual({}, bounded.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
