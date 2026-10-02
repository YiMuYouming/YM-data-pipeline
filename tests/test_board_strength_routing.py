"""W4 S2：concept_index 与 market_board_strength 的路由（开工单 W4 第二节第 2 点）。

上一版只写了计算内核 `board_strength.py`，没接路由——在没接通之前
`query(...)` 会静默走不到，比不接更危险。这里把两个意图一次接通：
registry（短语 → 意图）→ routing（意图 → provider）→ provider_policy
（capability → 允许的 provider/方法/时效）→ api（参数校验）→ normalizer
（结果归一）→ provider adapter（真取数或本地合成）。
"""

from __future__ import annotations

import unittest

from ym_stock_data import api as pipeline_api
from ym_stock_data import intent_registry, provider_policy, routing
from ym_stock_data.board_strength import CONCEPT_PREFIX, INDUSTRY_PREFIX
from ym_stock_data.contracts import ProviderAttempt
from ym_stock_data.intent_normalizers import normalize_success


def _normalize(intent, params, data, provider):
    spec = routing.route_for(intent, params)
    return normalize_success(
        intent, params, data, provider=provider, spec=spec,
        attempts=[ProviderAttempt(provider=provider, status="ok",
                                  error_code=None, latency_ms=1)],
    )


class ConceptIndexRouteTests(unittest.TestCase):
    """概念板块是另一种类型（885xxx），走 StockToday，不与 881 行业混排（K1）。"""

    def test_phrase_resolves_to_concept_index(self):
        resolved = intent_registry.resolve_intent("查概念板块", names=["算力"])
        self.assertEqual("concept_index", resolved["intent"])
        self.assertEqual({"names": ["算力"]}, resolved["params"])

    def test_route_targets_stocktoday(self):
        spec = routing.route_for("concept_index", {"names": ["算力"]})
        self.assertEqual("concept_index", spec.intent)
        self.assertEqual(("stocktoday",), spec.providers)

    def test_capability_is_declared_and_allowlisted(self):
        capability = routing.capability_for("concept_index", {"names": ["算力"]})
        self.assertEqual("concept_index", capability)
        self.assertIn(capability, provider_policy.CAPABILITIES)
        self.assertEqual(
            frozenset({"stocktoday"}),
            provider_policy._CAPABILITY_PROVIDER_ALLOWLIST[capability],
        )
        self.assertTrue(provider_policy._CAPABILITY_METHODS[capability],
                        "capability 必须声明它允许调的 StockToday 方法")

    def test_codes_must_use_the_885_prefix(self):
        ok, _ = pipeline_api._validate_params("concept_index", {"codes": [f"{CONCEPT_PREFIX}001"]})
        self.assertTrue(ok)

    def test_industry_prefix_is_refused_for_concept_index(self):
        """881 是行业，不是概念——放过去就是两种类型混排。"""
        with self.assertRaises(ValueError) as ctx:
            pipeline_api._validate_params(
                "concept_index", {"codes": [f"{INDUSTRY_PREFIX}001"]}
            )
        self.assertIn("885", str(ctx.exception))

    def test_sector_index_still_refuses_concept_codes(self):
        with self.assertRaises(ValueError):
            pipeline_api._validate_params("concept_index", {"codes": []})

    def test_normalizer_passes_items_through(self):
        payload = {"items": [{"code": "885001", "name": "算力"}]}
        business, quality = _normalize("concept_index", {"names": ["算力"]},
                                       payload, "stocktoday")
        self.assertEqual(payload["items"], business["items"])
        self.assertEqual(1, quality["row_count"])


class MarketBoardStrengthRouteTests(unittest.TestCase):
    """板块强度是**本地合成**的：输入全部来自已注册的既有 capability，不新增数据源。"""

    def test_phrase_resolves_to_market_board_strength(self):
        resolved = intent_registry.resolve_intent("查板块强度", trade_date="20260930")
        self.assertEqual("market_board_strength", resolved["intent"])
        self.assertEqual({"trade_date": "20260930"}, resolved["params"])

    def test_route_targets_the_local_composite_provider(self):
        spec = routing.route_for("market_board_strength", {"trade_date": "20260930"})
        self.assertEqual("market_board_strength", spec.intent)
        self.assertEqual(("board_strength",), spec.providers)

    def test_capability_is_declared_and_not_borrowed_from_another_route(self):
        capability = routing.capability_for("market_board_strength", {"trade_date": "20260930"})
        self.assertIn(capability, provider_policy.CAPABILITIES)
        self.assertEqual(
            frozenset({"board_strength"}),
            provider_policy._CAPABILITY_PROVIDER_ALLOWLIST[capability],
        )

    def test_route_records_its_inputs(self):
        """路由要写明数据_scope：下游据此知道这些数是合成的、不是某个源的原始输出。"""
        spec = routing.route_for("market_board_strength", {"trade_date": "20260930"})
        for token in ("market_facts", "sector_index", "industry_flow"):
            self.assertIn(token, spec.data_scope)

    def test_trade_date_is_validated(self):
        ok, _ = pipeline_api._validate_params("market_board_strength", {"trade_date": "20260930"})
        self.assertTrue(ok)

    def test_normalizer_keeps_boards_and_gaps(self):
        payload = {
            "boards": [{"board_id": "881001", "fields": {}, "source_gaps": []}],
            "source_gaps": [],
        }
        business, quality = _normalize("market_board_strength", {"board_ids": ["881001"]},
                                       payload, "board_strength")
        self.assertEqual(payload["boards"], business["boards"])
        self.assertEqual([], business["source_gaps"])
        self.assertEqual(1, quality["row_count"])


class OneBoardOnePlaceTests(unittest.TestCase):
    """铁律 2：行业与概念是两种类型，分开装载，不混排。"""

    def test_both_prefixes_are_declared_and_distinct(self):
        self.assertEqual("881", INDUSTRY_PREFIX)
        self.assertEqual("885", CONCEPT_PREFIX)
        self.assertNotEqual(INDUSTRY_PREFIX, CONCEPT_PREFIX)

    def test_board_strength_and_concept_index_are_separate_intents(self):
        specs = {spec.intent for spec in intent_registry._SPECS}
        self.assertIn("concept_index", specs)
        self.assertIn("market_board_strength", specs)
        self.assertNotEqual(
            routing.route_for("concept_index", {}).providers,
            routing.route_for("sector_index", {}).providers,
        )


if __name__ == "__main__":
    unittest.main()