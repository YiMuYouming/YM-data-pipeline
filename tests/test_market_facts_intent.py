"""Public market-facts queries preserve dated quality and never create a database."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ym_stock_data import query
from ym_stock_data.market_facts import MarketFactStore
from tests.test_market_facts import limit_result, row


class MarketFactsIntentTests(unittest.TestCase):
    def test_dated_promotion_comes_from_existing_fact_store(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "facts.sqlite3"
            store = MarketFactStore(path)
            store.ingest_limits("20260922", limit_result("20260922", [row("000001", 1), row("000002", 2)]))
            store.ingest_limits("20260923", limit_result("20260923", [row("000001", 2), row("000003", 1)]))
            with patch.dict(os.environ, {"YM_MARKET_FACTS_DB": str(path)}):
                result = query("market_facts", trade_date="20260923")
            self.assertEqual(result["_meta"]["provider_used"], "market_facts")
            self.assertEqual(result["_meta"]["intent"], "market_facts")
            self.assertEqual(result["data"]["promotion_overall_by_code"]["pct"], 50.0)
            self.assertEqual(result["data"]["trade_date"], "20260923")
            self.assertEqual(result["_meta"]["status"], "degraded")
            self.assertIn("all_market_daily_breadth_missing", result["_meta"]["quality"]["reason_codes"])

    def test_missing_store_is_error_and_not_created(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "missing" / "facts.sqlite3"
            with patch.dict(os.environ, {"YM_MARKET_FACTS_DB": str(path)}):
                result = query("market_facts", trade_date="20260923")
            self.assertEqual(result["_meta"]["status"], "error")
            self.assertEqual(result["_meta"]["attempts"][0]["error_code"], "FACT_STORE_MISSING")
            self.assertFalse(path.parent.exists())

    def test_invalid_date_is_rejected_before_provider(self):
        with self.assertRaises(ValueError):
            query("market_facts", trade_date="2026-09-23")
