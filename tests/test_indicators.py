import json
import re
import unittest
from pathlib import Path

from ym_stock_data import indicators


ROOT = Path(__file__).resolve().parents[1]


class IndicatorFormulaTests(unittest.TestCase):
    def test_emotion_excludes_flat_from_the_denominator(self):
        # 2026-09-24: up 1120, down 4306, flat 131 (market_facts daily rows).
        self.assertAlmostEqual(20.641356, indicators.emotion(1120, 4306), places=6)
        # The legacy up / all-rows value was 20.15; it must not come back.
        self.assertNotAlmostEqual(20.15, round(indicators.emotion(1120, 4306), 2))
        self.assertIsNone(indicators.emotion(0, 0))

    def test_emotion_bands_follow_glossary_boundaries(self):
        cases = {19.99: "冰点", 20: "低迷", 39.99: "低迷", 40: "主升",
                 69.99: "主升", 70: "强势", 80: "强势", 80.01: "高潮", None: None}
        for score, label in cases.items():
            with self.subTest(score=score):
                self.assertEqual(label, indicators.emotion_band(score))

    def test_breadth_counts_flat_separately(self):
        counts = indicators.breadth([1.2, -0.5, 0, 0.0, 3, -2])
        self.assertEqual((2, 2, 2, 6), (counts["up"], counts["down"], counts["flat"], counts["total"]))
        self.assertEqual(1.0, counts["up_down_ratio"])

    def test_broken_and_seal_rate(self):
        # 2026-09-24 pools: 52 sealed, 10 broken → 10 / 62.
        self.assertAlmostEqual(16.129032, indicators.broken_rate(10, 52), places=6)
        self.assertAlmostEqual(83.870968, indicators.seal_rate(10, 52), places=6)
        self.assertEqual("正常偏强", indicators.broken_rate_band(16.129032))
        self.assertEqual("一致性强", indicators.broken_rate_band(15.9))
        self.assertEqual("分歧严重", indicators.broken_rate_band(40))
        self.assertEqual("退潮信号", indicators.broken_rate_band(40.1))
        self.assertIsNone(indicators.broken_rate(0, 0))

    def test_limit_state_from_exchange_limit_prices(self):
        self.assertEqual("up", indicators.limit_state(11.0, 11.0, 11.0, 9.0))
        self.assertEqual("down", indicators.limit_state(9.0, 9.5, 11.0, 9.0))
        self.assertEqual("broken", indicators.limit_state(10.8, 11.0, 11.0, 9.0))
        self.assertIsNone(indicators.limit_state(10.8, 10.9, 11.0, 9.0))
        self.assertIsNone(indicators.limit_state(None, None, 11.0, 9.0))

    def test_tiered_promotion_counts_only_next_board_seals(self):
        previous = {"a": 1, "b": 1, "c": 2, "d": 2, "e": 3, "f": 5}
        current = {"a": 2, "c": 3, "e": 4, "f": 6, "x": 1}
        tiers = indicators.promotion(previous, current)
        self.assertEqual((1, 2, 50.0), (tiers["one_to_two"]["numerator"], tiers["one_to_two"]["denominator"], tiers["one_to_two"]["pct"]))
        self.assertEqual(["c"], tiers["two_to_three"]["promoted_codes"])
        self.assertEqual(100.0, tiers["three_to_four"]["pct"])
        self.assertEqual((4, 6), (tiers["overall"]["numerator"], tiers["overall"]["denominator"]))

    def test_board_heights_and_cohort_returns(self):
        self.assertEqual({"highest": 6, "second_highest": 4},
                         indicators.board_heights({"a": 6, "b": 4, "c": 4, "d": 1}))
        self.assertEqual({"highest": None, "second_highest": None}, indicators.board_heights({}))
        changes = {"a": 10.0, "b": -2.0, "c": 1.0}
        self.assertEqual(3.0, indicators.cohort_return(["a", "b", "c"], changes))
        self.assertIsNone(indicators.cohort_return(["a", "z"], changes))
        self.assertIsNone(indicators.cohort_return([], changes))

    def test_legacy_money_effect_and_board_risk_are_registered_for_review(self):
        self.assertEqual("好", indicators.money_effect(2.1))
        self.assertEqual("一般", indicators.money_effect(2.0))
        self.assertEqual("差", indicators.money_effect(-0.1))
        self.assertEqual(0.64, indicators.board_risk(20.0))
        pending = {item["key"] for item in indicators.needs_vault_detail()}
        self.assertEqual({"money_effect", "board_risk", "board_heights"}, pending)

    def test_summarize_uses_one_input_set(self):
        changes = {"a": 10.0, "b": 10.0, "c": 5.0, "d": -10.0, "e": 0.0}
        result = indicators.summarize(
            changes=changes,
            limit_sets={"up": ["a", "b"], "down": ["d"], "broken": ["c"]},
            current_boards={"a": 2, "b": 1},
            previous_boards={"a": 1, "x": 1},
            previous_broken=["c"],
        )
        self.assertEqual("indicators.v1", result["indicator_version"])
        self.assertEqual(75.0, result["emotion"])
        self.assertEqual((3, 1, 1), (result["up_count"], result["down_count"], result["flat_count"]))
        self.assertEqual((2, 1, 1), (result["limit_up_count"], result["limit_down_count"], result["broken_count"]))
        self.assertAlmostEqual(33.333333, result["broken_rate"], places=6)
        self.assertEqual(2, result["board_highest"])
        self.assertIsNone(result["limit_up_return"])  # "x" has no change → incomplete cohort
        self.assertEqual(5.0, result["broken_return"])
        self.assertEqual(50.0, result["promotion"]["one_to_two"]["pct"])


class IndicatorRegistryTests(unittest.TestCase):
    def test_every_registered_indicator_has_the_required_fields(self):
        registry = json.loads(indicators.REGISTRY_PATH.read_text(encoding="utf-8"))
        self.assertEqual(indicators.INDICATOR_VERSION, registry["version"])
        for item in registry["indicators"]:
            with self.subTest(key=item["key"]):
                for field in ("name", "glossary_section", "formula", "inputs",
                              "universe", "intraday_postclose_same", "definition_status"):
                    self.assertIn(field, item)
                self.assertIn(item["universe"], registry["universes"])
                self.assertIn(item["definition_status"], {"defined", "needs_vault_detail"})


class IndicatorSingleImplementationTests(unittest.TestCase):
    """Formulas may only live in ym_stock_data/indicators.py."""

    PATTERNS = (
        re.compile(r"/\s*\(\s*up\w*\s*\+\s*down\w*\s*\)"),
        re.compile(r"up_count\"?\]?\s*/\s*"),
        re.compile(r"炸板.{0,20}÷"),
        re.compile(r"上涨\s*[/÷]\s*\(?\s*上涨\s*\+\s*下跌"),
    )

    def test_no_indicator_formula_outside_the_ssot_module(self):
        allowed = {ROOT / "ym_stock_data" / "indicators.py"}
        offenders = []
        for path in (ROOT / "ym_stock_data").rglob("*.py"):
            if path in allowed:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in self.PATTERNS:
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    offenders.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)}")
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
