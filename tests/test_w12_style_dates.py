import tempfile
import unittest
from pathlib import Path
from ym_stock_data.market_facts import MarketFactStore


class StyleDatesTest(unittest.TestCase):
    def test_prior_volume_retains_prior_date_and_cannot_fill_target(self):
        with tempfile.TemporaryDirectory() as td:
            store = MarketFactStore(Path(td) / "facts.sqlite3")
            with store._connect() as conn:
                conn.execute("INSERT INTO daily_runs VALUES (1,'20261009','fixture','2026-10-09T17:00:00+08:00','hash',1,1,0,0,'fixture')")
                conn.execute("INSERT INTO daily_rows VALUES (1,'600001',10,10,10,10,10,0,100,1000)")
            inputs = store.style_inputs("20261012")
            self.assertIsNone(inputs["市场量能"])
            cell = inputs["field_evidence"]["市场量能"]
            self.assertEqual(cell["data_as_of"], "2026-10-09")
            self.assertEqual(cell["status"], "stale")
            self.assertEqual(cell["source_gap"]["gap_code"], "market_volume_target_daily_missing")
            self.assertEqual(cell["source_gap"]["affected_actions"], ["d0_assessment"])
