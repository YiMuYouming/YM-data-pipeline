"""Tests for the canonical midcap above 20-day MA indicator (glossary §2.6)."""

import unittest
from ym_stock_data import indicators
from ym_stock_data.indicators import MIN_COVERAGE_FOR_PCT
from ym_stock_data.market_facts import MarketFactStore


class TestMidcapIndicator(unittest.TestCase):
    def test_midcap_above_ma20_pure_calculation(self):
        # 4 stocks:
        # code1: 20 days constant 10.0, today 12.0 -> above MA20 (MA20=10.1, close=12.0)
        # code2: 20 days constant 10.0, today 8.0 -> below MA20 (MA20=9.9, close=8.0)
        # code3: 15 days only -> missing bars -> excluded
        # code4: 20 days, but day 10 has ex-rights (pre_close 5.0 != prev_close 10.0) -> excluded
        # code5: BJ code '920001' -> excluded
        # code6: ST code '000006' -> excluded

        bars_by_code = {
            "000001": [(10.0, 10.0)] * 19 + [(12.0, 10.0)],
            "000002": [(10.0, 10.0)] * 19 + [(8.0, 10.0)],
            "000003": [(10.0, 10.0)] * 15,
            "000004": [(10.0, 10.0)] * 10 + [(5.0, 5.0)] + [(5.0, 5.0)] * 9,
            "920001": [(10.0, 10.0)] * 20,
            "000006": [(10.0, 10.0)] * 20,
        }
        # Notice in 000004, bar 10 has pre_close 5.0 while bar 9 has close 10.0

        res = indicators.midcap_above_ma20_pct(
            bars_by_code,
            st_codes={"000006"},
        )

        self.assertEqual(res["cohort_size"], 6)
        self.assertEqual(res["row_count"], 2)  # only 000001 and 000002 valid
        self.assertEqual(res["above_count"], 1)
        self.assertEqual(res["above_codes"], ["000001"])
        # 覆盖率 2/6 低于下限 → 比例不给数（N2）；纯计算本身由 test_midcap_cohort
        # 里覆盖率达标的那组夹具验证
        # N2：按规则剔除的进 excluded_codes（带原因），真缺数据的才叫 missing
        self.assertEqual(res["excluded_codes"], {
            "000003": "suspended_or_new",
            "000004": "ex_right",
            "000006": "st",
            "920001": "beijing",
        })
        self.assertEqual(res["missing_codes"], [])
        self.assertIsNone(res["pct"])
        self.assertIsNone(res["score"])
        self.assertAlmostEqual(res["coverage"], 2 / 6, places=4)
        self.assertTrue(any("midcap_coverage_below_floor" in gap["gap_code"]
                            for gap in res["source_gaps"]))

    def test_midcap_scoring(self):
        self.assertEqual(indicators.midcap_score(65.0), 10)
        self.assertEqual(indicators.midcap_score(60.1), 10)
        self.assertEqual(indicators.midcap_score(60.0), 7)
        self.assertEqual(indicators.midcap_score(50.0), 7)
        self.assertEqual(indicators.midcap_score(45.0), 7)
        self.assertEqual(indicators.midcap_score(44.9), 4)
        self.assertEqual(indicators.midcap_score(30.0), 4)
        self.assertEqual(indicators.midcap_score(29.9), 0)
        self.assertEqual(indicators.midcap_score(0.0), 0)
        self.assertIsNone(indicators.midcap_score(None))

    def test_market_facts_style_inputs_real_db(self):
        store = MarketFactStore()
        inputs = store.style_inputs("20260930")
        self.assertNotIn("大市值赚钱比例", inputs)
        self.assertNotIn("large_cap_profit_pct", inputs)
        self.assertIn("midcap_above_ma20_pct", inputs)
        self.assertIn("中军站上20日线比例", inputs)
        self.assertEqual(inputs["midcap_above_ma20_pct"], inputs["中军站上20日线比例"])
        self.assertAlmostEqual(inputs["midcap_above_ma20_pct"], 35.2941, places=3)
        self.assertIn("midcap_evidence", inputs)
        ev = inputs["midcap_evidence"]
        self.assertEqual(ev["cohort_size"], 100)
        self.assertEqual(ev["row_count"], 85)
        self.assertEqual(ev["above_count"], 30)
        # N2：这 15 只全是"按规则剔除"（ST / 停牌新股 / 上市不足 20 期 / 除权 / 北交所），
        # 没有一只是"读不到数据"。以前两档混在一栏，看覆盖率时说不清代表什么。
        self.assertEqual(len(ev["missing_codes"]), 0, "真缺数据的应当为 0")
        self.assertEqual(len(ev["excluded_codes"]), 15)
        self.assertGreaterEqual(ev["coverage"], MIN_COVERAGE_FOR_PCT,
                                "真实样本的覆盖率达到下限，比例才给得出 35.2941")
        self.assertEqual(ev["source_gaps"], [])


if __name__ == "__main__":
    unittest.main()
