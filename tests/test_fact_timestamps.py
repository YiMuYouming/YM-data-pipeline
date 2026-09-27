"""test_fact_timestamps.py — P3.1 字段合同与事实时间

验收（03 篇 P3.1 + 05 篇 G09–G12）：
- 为每个使用中的 setup 列出 required_fields，用 true/false/unknown 计算；
  缺 MA 只影响需要 MA 的判断（G09）；
- ``received_at`` 新、``event_at`` 旧时仍然算旧，不伪装实时（G10）；
- 时间缺失/未来/乱序 → 明确 unknown 或异常，不填 now（G11）；
- 未完成 K 线不得提前满足 closed-bar 条件（G13 的前置）；
- 派生指标不把计算时刻冒充事件时间。

全隔离：纯函数；不联网、不读生产缓存。
"""

import unittest
from datetime import datetime, timedelta, timezone

from ym_stock_data import fact_fields as ff

CN = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 10, 10, 0, 0, tzinfo=CN)


class FieldContractTest(unittest.TestCase):
    def test_every_contract_declares_optional_explicitly(self):
        for setup_id, contract in ff.FIELD_CONTRACTS.items():
            self.assertIn("required_fields", contract, setup_id)
            self.assertIn("optional_fields", contract, setup_id)
            self.assertIsInstance(contract["optional_fields"], tuple)

    def test_required_and_optional_do_not_overlap(self):
        for setup_id, contract in ff.FIELD_CONTRACTS.items():
            overlap = set(contract["required_fields"]) & set(contract["optional_fields"])
            self.assertEqual(overlap, set(), f"{setup_id} 字段同时被声明为必要与可选")

    def test_unknown_setup_raises(self):
        with self.assertRaises(KeyError):
            ff.contract_for("nope")

    def test_account_fields_are_listed(self):
        self.assertIn("sellable_qty", ff.ACCOUNT_FIELDS)

    def test_position_risk_requires_sellable_qty(self):
        contract = ff.contract_for("position_risk")
        self.assertIn("sellable_qty", contract["required_fields"])


class TriStateTest(unittest.TestCase):
    def test_all_true(self):
        self.assertTrue(ff.all_required([True, True]))

    def test_false_dominates(self):
        self.assertFalse(ff.all_required([True, None, False]))

    def test_none_is_unknown_not_false(self):
        self.assertIsNone(ff.all_required([True, None]))
        self.assertIsNone(ff.all_required([None]))

    def test_empty_is_true(self):
        self.assertTrue(ff.all_required([]))


class SetupEvaluationTest(unittest.TestCase):
    """G09：价格实时但必要均线缺失 → 该 setup unknown，其它动作继续。"""

    COMPLETE_FACTS = {
        "price": 10.0, "ma5_daily": 9.8, "ma10_daily": 9.7,
        "ma10_60m": 9.6, "ma10_60m_dir": "向上", "volume_ratio": 0.7,
        "change_pct": -2.0,
    }

    def test_complete_facts_are_evaluable(self):
        result = ff.evaluate_setup("trend_pullback", self.COMPLETE_FACTS)
        self.assertEqual(result["state"], "evaluable")
        self.assertEqual(result["missing_required"], [])
        self.assertEqual(result["condition_truth"], "computable",
                         "事实齐备只说明可计算，不等于已触发")
        self.assertNotIn(result["condition_truth"], (True, False))

    def test_missing_ma_only_affects_that_setup(self):
        facts = dict(self.COMPLETE_FACTS)
        facts.pop("ma10_60m")
        batch = ff.evaluate_setups(["trend_pullback", "position_risk"],
                                   {**facts, "sellable_qty": 100, "quantity": 100})
        self.assertEqual(batch["setups"]["trend_pullback"]["state"], "unknown")
        self.assertIn("ma10_60m", batch["setups"]["trend_pullback"]["missing_required"])
        self.assertEqual(batch["setups"]["position_risk"]["state"], "evaluable",
                         "缺 MA 不得清空其它 setup")
        self.assertEqual(batch["unknown_setups"], ["trend_pullback"])

    def test_missing_required_is_not_false(self):
        result = ff.evaluate_setup("trend_pullback", {"price": 10.0})
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["condition_truth"],
                          "必要事实未知不得当成条件为假")
        self.assertIn("ma5_daily", result["missing_required"])

    def test_optional_missing_does_not_block(self):
        facts = dict(self.COMPLETE_FACTS)
        result = ff.evaluate_setup("trend_pullback", facts)
        self.assertEqual(result["state"], "evaluable")
        self.assertIn("ma20_daily", result["missing_optional"])

    def test_unclosed_bar_blocks_closed_bar_conditions(self):
        """G13 前置：15/60 分钟 K 线未完成不得提前满足 closed-bar 条件。"""
        facts = dict(self.COMPLETE_FACTS)
        facts["ma10_60m__bar_closed"] = False
        result = ff.evaluate_setup("trend_pullback", facts)
        self.assertEqual(result["state"], "unknown")
        self.assertIn("ma10_60m", result["closed_bar_not_finished"])

    def test_closed_bar_ok_when_marked_closed(self):
        facts = dict(self.COMPLETE_FACTS)
        facts["ma10_60m__bar_closed"] = True
        self.assertEqual(ff.evaluate_setup("trend_pullback", facts)["state"],
                         "evaluable")

    def test_evaluate_setups_reports_both_buckets(self):
        batch = ff.evaluate_setups(
            ["trend_pullback", "position_risk"],
            {**self.COMPLETE_FACTS, "sellable_qty": 1, "quantity": 1})
        self.assertEqual(batch["evaluable_setups"], ["position_risk", "trend_pullback"])
        self.assertEqual(batch["unknown_setups"], [])


class FactTimeTest(unittest.TestCase):
    """G10 / G11：事件时间与接收时间分列，缺失即 unknown。"""

    def test_received_new_event_old_is_still_old(self):
        fact = ff.build_fact(
            "quotes",
            event_at="2026-09-10T08:00:00+08:00",
            received_at="2026-09-10T09:59:59+08:00",
            source="pytdx",
        )
        self.assertEqual(fact["status"], "measured")
        self.assertEqual(ff.event_age_ms(fact["event_at"], NOW), 2 * 60 * 60 * 1000)
        verdict = ff.fact_is_fresh(fact, max_age_ms=60_000, now=NOW)
        self.assertFalse(verdict["fresh"], "必须按 event_at 判定")
        self.assertEqual(verdict["reason"], "event_at_too_old")

    def test_missing_event_at_is_unknown_not_zero(self):
        fact = ff.build_fact("quotes", event_at=None,
                             received_at="2026-09-10T09:59:00+08:00")
        self.assertEqual(fact["status"], "unknown")
        self.assertEqual(fact["reason"], "event_at_missing")
        self.assertFalse(fact["event_at_verified"])
        self.assertFalse(ff.fact_is_fresh(fact, 60_000, NOW)["fresh"])

    def test_naive_timestamp_is_unverifiable(self):
        fact = ff.build_fact("quotes", event_at="2026-09-10T09:00:00")
        self.assertEqual(fact["status"], "unknown")

    def test_future_event_is_clock_skew(self):
        with self.assertRaises(ValueError) as ctx:
            ff.event_age_ms("2026-09-10T10:05:00+08:00", NOW)
        self.assertEqual(str(ctx.exception), "clock_skew")

    def test_small_drift_clamps_to_zero(self):
        self.assertEqual(ff.event_age_ms("2026-09-10T10:00:00.400+08:00", NOW), 0)

    def test_naive_now_raises(self):
        with self.assertRaises(ValueError) as ctx:
            ff.event_age_ms("2026-09-10T09:00:00+08:00", datetime(2026, 9, 10, 10, 0))
        self.assertEqual(str(ctx.exception), "timezone_required")

    def test_fresh_fact_passes(self):
        fact = ff.build_fact("quotes", event_at="2026-09-10T09:59:30+08:00")
        verdict = ff.fact_is_fresh(fact, max_age_ms=60_000, now=NOW)
        self.assertTrue(verdict["fresh"])
        self.assertEqual(verdict["age_ms"], 30_000)

    def test_fact_carries_source_and_quality_separately(self):
        fact = ff.build_fact("quotes", event_at="2026-09-10T09:59:00+08:00",
                             source="pytdx", source_mode="unified",
                             quality="normal", instrument="000001.SZ",
                             trading_date="2026-09-10")
        self.assertEqual(fact["source"], "pytdx")
        self.assertEqual(fact["source_mode"], "unified")
        self.assertEqual(fact["quality"], "normal")
        self.assertEqual(fact["instrument"], "000001.SZ")
        self.assertEqual(fact["trading_date"], "2026-09-10")


class DerivedFactTest(unittest.TestCase):
    def test_derived_fact_uses_latest_input_event_time(self):
        inputs = [
            {"event_at": "2026-09-10T09:55:00+08:00"},
            {"event_at": "2026-09-10T09:57:00+08:00"},
        ]
        fact = ff.build_derived_fact(
            "ma10_60m", inputs=inputs, computed_at="2026-09-10T09:58:00+08:00")
        self.assertEqual(fact["event_at"], "2026-09-10T09:57:00+08:00",
                         "派生指标事件时间取输入最新，而不是计算时刻")
        self.assertEqual(fact["computed_at"], "2026-09-10T09:58:00+08:00")

    def test_derived_fact_without_input_event_time_is_unknown(self):
        fact = ff.build_derived_fact(
            "ma10_60m", inputs=[{}, {"event_at": None}],
            computed_at="2026-09-10T09:58:00+08:00")
        self.assertEqual(fact["status"], "unknown")
        self.assertIsNone(fact["event_at"])
        self.assertEqual(fact["computed_at"], "2026-09-10T09:58:00+08:00")


class NoFabricationTest(unittest.TestCase):
    """不得用 fetched_now 冒充实时。"""

    def test_build_fact_does_not_invent_event_at(self):
        fact = ff.build_fact("quotes", received_at="2026-09-10T09:59:00+08:00")
        self.assertIsNone(fact["event_at"])

    def test_event_age_none_not_zero_for_missing(self):
        self.assertIsNone(ff.event_age_ms(None, NOW))
        self.assertIsNone(ff.event_age_ms("", NOW))
        self.assertIsNone(ff.event_age_ms("not-a-time", NOW))

    def test_contracts_do_not_treat_provider_status_as_condition_truth(self):
        """provider 的 success/empty/degraded/error 与条件真假是两套含义。"""
        result = ff.evaluate_setup("trend_pullback", {
            "price": 1.0, "ma5_daily": 1.0, "ma10_daily": 1.0, "ma10_60m": 1.0,
            "ma10_60m_dir": "向上", "volume_ratio": 0.5, "change_pct": -1.0,
            "provider_status": "degraded",
        })
        self.assertEqual(result["state"], "evaluable")
        self.assertNotIn(result["condition_truth"], (True, False),
                         "provider 状态不得被翻译成条件真假")


if __name__ == "__main__":
    unittest.main()
