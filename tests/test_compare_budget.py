"""Index comparison must not stall the live poll when a source fails."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ym_stock_data import api
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.sources import eastmoney_index


def _comparison():
    row = {"t": "09:45", "chg": 0.1, "vol": 100, "volRatio": 1.0,
           "amount": 1000, "yesterdayAmt": 900}
    return {name: [dict(row)] for name in ("上证15min", "深证15min", "创业15min")}


class _Provider:
    def __init__(self, name, outcome, delay=0):
        self.name = name
        self.outcome = outcome
        self.delay = delay
        self.calls = 0

    def call(self, _intent, _params):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return self.outcome


class CompareBudgetTests(unittest.TestCase):
    def setUp(self):
        with api._compare_lock:
            api._compare_failures.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = ProviderState(Path(self.tmp.name) / "providers.sqlite3")

    def _query(self, providers):
        return api._query_with(
            "index_intraday_compare", {"period": "15m"},
            provider_loader=lambda name: providers[name],
            state_loader=lambda: self.state,
        )

    def test_total_budget_and_fixed_fallback(self):
        providers = {
            "eastmoney_index": _Provider("eastmoney_index", ProviderOutcome("eastmoney_index", "success", _comparison()), .2),
            "sina_index": _Provider("sina_index", ProviderOutcome("sina_index", "success", _comparison())),
            "stocktoday": _Provider("stocktoday", ProviderOutcome("stocktoday", "success", _comparison())),
        }
        with patch.object(api, "_COMPARE_BUDGET_SECONDS", .11), patch.object(api, "_COMPARE_PROVIDER_SECONDS", (.04, .04, .04)):
            started = time.monotonic()
            result = self._query(providers)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, .15)
        self.assertEqual("sina_index", result["_meta"]["provider_used"])
        self.assertEqual("QUERY_BUDGET_EXCEEDED", result["_meta"]["attempts"][0]["error_code"])
        self.assertEqual(0, providers["stocktoday"].calls)

    def test_fast_eastmoney_compare_does_not_stack_source_retries(self):
        with patch.object(eastmoney_index.CLIENT, "get", side_effect=ConnectionError("offline")) as get:
            data, error = eastmoney_index.get_json_payload(
                "https://example.invalid", params={}, headers={}, timeout=2.5, fast=True
            )
        self.assertIsNone(data)
        self.assertEqual("ConnectionError", error["error_type"])
        get.assert_called_once()
        self.assertFalse(get.call_args.kwargs["retry"])

    def test_consecutive_failure_skips_then_probes_and_recovers(self):
        primary = _Provider("eastmoney_index", ProviderOutcome("eastmoney_index", "provider_error", error_code="UPSTREAM"))
        fallback = _Provider("sina_index", ProviderOutcome("sina_index", "success", _comparison()))
        providers = {"eastmoney_index": primary, "sina_index": fallback,
                     "stocktoday": _Provider("stocktoday", ProviderOutcome("stocktoday", "empty", {}))}
        with patch.object(api, "_COMPARE_BREAKER_SECONDS", 1):
            self._query(providers)
            self._query(providers)
            skipped = self._query(providers)
            self.assertEqual(2, primary.calls)
            self.assertEqual("breaker_open", skipped["_meta"]["attempts"][0]["status"])
            time.sleep(1.05)
            primary.outcome = ProviderOutcome("eastmoney_index", "success", _comparison())
            recovered = self._query(providers)
        self.assertEqual("eastmoney_index", recovered["_meta"]["provider_used"])
        self.assertEqual(3, primary.calls)
        self.assertIsNone(self.state.active_breaker(api._compare_key("eastmoney_index")))


if __name__ == "__main__":
    unittest.main()
