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
from ym_stock_data.board_strength import CONCEPT_CODE_PREFIXES, INDUSTRY_PREFIX
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
    """概念板块是另一种类型，走 StockToday，不与行业混排（K1）。

    判据是数据源的 **type 字段**（N=概念）。实测同花顺概念不止 885：
    916 条概念分布在 864/865/875/883/885/886 六个前缀上，而 864/883 与
    "昨日涨幅超过X%" 这类选股筛选共用——只认 885 会拒掉 613 条真概念。
    """

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

    def test_every_observed_concept_prefix_is_accepted(self):
        """实测出现的六个概念前缀都要能过——它们都是真概念。"""
        for prefix in pipeline_api.CONCEPT_CODE_PREFIXES:
            # 不抛异常即通过
            pipeline_api._validate_params("concept_index", {"codes": [f"{prefix}005"]})

    def test_a_filter_code_prefix_is_refused(self):
        """864 是概念与选股筛选共用前缀；形状检查只挡形状，type 才挡类型。"""
        with self.assertRaises(ValueError) as ctx:
            pipeline_api._validate_params("concept_index", {"codes": ["991001"]})
        self.assertIn("864", str(ctx.exception))

    def test_industry_prefix_is_refused_for_concept_index(self):
        """881 是行业，不是概念——放过去就是两种类型混排。"""
        with self.assertRaises(ValueError) as ctx:
            pipeline_api._validate_params(
                "concept_index", {"codes": [f"{INDUSTRY_PREFIX}001"]}
            )
        self.assertIn("concept prefix", str(ctx.exception))

    def test_concept_index_requires_codes_or_names(self):
        with self.assertRaises(ValueError) as ctx:
            pipeline_api._validate_params("concept_index", {"codes": []})
        self.assertIn("requires codes or names", str(ctx.exception))

    def test_normalizer_passes_items_through(self):
        payload = {"items": [{"code": "885001", "name": "算力"}]}
        business, quality = _normalize("concept_index", {"names": ["算力"]},
                                       payload, "stocktoday")
        self.assertEqual(payload["items"], business["items"])
        self.assertEqual(1, quality["returned_count"])


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
        pipeline_api._validate_params("market_board_strength", {"trade_date": "20260930"})

    def test_normalizer_keeps_boards_and_gaps(self):
        payload = {
            "boards": [{"board_id": "881001", "fields": {}, "source_gaps": []}],
            "source_gaps": [],
        }
        business, quality = _normalize("market_board_strength", {"board_ids": ["881001"]},
                                       payload, "board_strength")
        self.assertEqual(payload["boards"], business["boards"])
        self.assertEqual([], business["source_gaps"])
        self.assertEqual(1, quality["returned_count"])
        self.assertEqual([], quality["gaps"])


class OneBoardOnePlaceTests(unittest.TestCase):
    """铁律 2：行业与概念是两种类型，分开装载，不混排。"""

    def test_industry_and_concept_prefixes_stay_disjoint(self):
        """行业前缀与概念前缀不许重叠——重叠就意味着类型只能靠 type 分。"""
        industry = {"700", "861", "871", "877", "881", "884"}
        concept = set(CONCEPT_CODE_PREFIXES)
        self.assertEqual(set(), concept & industry,
                         "行业与概念的前缀重叠了，按前缀分家就不再成立")
        self.assertEqual("881", INDUSTRY_PREFIX)
        self.assertEqual(concept, set(CONCEPT_CODE_PREFIXES))

    def test_board_type_is_the_real_discriminator(self):
        """前缀会重叠（864/883），type 才是判据。"""
        self.assertEqual("N", pipeline_api.CONCEPT_BOARD_TYPE)
        self.assertEqual("I", pipeline_api.INDUSTRY_BOARD_TYPE)
        self.assertNotEqual(pipeline_api.CONCEPT_BOARD_TYPE,
                            pipeline_api.INDUSTRY_BOARD_TYPE)

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

class ConceptTypeGateTests(unittest.TestCase):
    """适配器按 type 收口：同前缀的选股筛选条件不能混进概念板块。

    实测 ths_index 里 `864005 区块链`(type=N) 与 `864001 昨日涨幅超过10%`(type=S)
    前缀完全一样。api 层的前缀检查挡不住这种，只能在返回结果上按 type 过滤。
    """

    def _provider_with_rows(self, rows):
        from ym_stock_data.providers import stocktoday as st

        provider = st.StockTodayProvider(token_loader=lambda: "t")
        outcome = provider._request_table = lambda name, nested, **kw: st.ProviderOutcome(
            "stocktoday", "success",
            data={"items": rows, "_stocktoday": {}},
            fetched_at="2026-10-02T15:00:00+08:00", latency_ms=1,
        )
        return provider

    def test_same_prefix_different_type_is_separated(self):
        provider = self._provider_with_rows([
            {"ts_code": "864005.TI", "name": "区块链", "type": "N"},
            {"ts_code": "864001.TI", "name": "昨日涨幅超过10%", "type": "S"},
            {"ts_code": "864006.TI", "name": "固态电池", "type": "N"},
        ])
        out = provider.call("concept_index", {"codes": ["864005", "864006"]})
        names = sorted(row["name"] for row in out.data["items"])
        self.assertEqual(["区块链", "固态电池"], names,
                         "type=S 的选股筛选条件混进概念板块了")

    def test_requested_code_that_is_not_a_concept_is_reported_as_missing(self):
        """请求了但不是概念的代码要进 missing——不回声不等于没有。"""
        provider = self._provider_with_rows([
            {"ts_code": "864005.TI", "name": "区块链", "type": "N"},
            {"ts_code": "883001.TI", "name": "昨日成交量前十", "type": "S"},
        ])
        out = provider.call("concept_index", {"codes": ["864005.TI", "883001.TI"]})
        self.assertEqual(["883001.TI"], out.data["missing"])
        self.assertEqual(["区块链"], [r["name"] for r in out.data["items"]])
        self.assertEqual("success", out.status)

    def test_codes_get_the_ti_suffix(self):
        """ts_code 必须带 .TI 后缀：不带上游静默返空（在线实测，2026-10-02）。

        与 board_members 查到的是同一个坑：``885957`` 查不到，
        ``885957.TI`` 才返回。适配器补后缀，回显/ missing 用调用方的裸 id。
        """
        provider = self._provider_with_rows([
            {"ts_code": "885957.TI", "name": "东数西算(算力)", "type": "N"},
        ])
        seen = {}
        original = provider._request_table

        def _spy(name, nested, **kwargs):
            seen.update(nested)
            return original(name, nested, **kwargs)

        provider._request_table = _spy
        out = provider.call("concept_index", {"codes": ["885957"]})
        self.assertEqual("885957.TI", seen["ts_code"])
        self.assertEqual(["东数西算(算力)"],
                         [row["name"] for row in out.data["items"]])
        self.assertEqual([], out.data["missing"])

    def test_missing_type_field_is_not_silently_accepted(self):
        """没有 type 的行一律不收——宁可少给，不能把类型不明的东西当概念发出去。"""
        provider = self._provider_with_rows([
            {"ts_code": "864007.TI", "name": "太阳能"},
        ])
        out = provider.call("concept_index", {"codes": ["864007"]})
        self.assertEqual([], out.data["items"])
        self.assertEqual(["864007"], out.data["missing"])


class BoardRowCountTests(unittest.TestCase):
    """板块行数必须被算出来，否则 provider 报 empty、整次查询被拒。

    在线烟测（trade_date=20260930）时发现的：三个板块查得出来，
    attempts 里却是 STATUS_DATA_MISMATCH——`_row_count` 不认识 boards 键，
    provider 因此报 status=empty，而上层发现数据其实不空。
    """

    def test_boards_key_counts_as_rows(self):
        from ym_stock_data.providers import local

        raw = {"boards": [{"board_id": "881121"}, {"board_id": "881155"}],
               "quality": {"returned_count": 2}}
        self.assertEqual(2, local._row_count("market_board_strength", raw))

    def test_empty_boards_is_zero(self):
        from ym_stock_data.providers import local

        self.assertEqual(0, local._row_count("market_board_strength", {"boards": []}))


class PoolJoinTests(unittest.TestCase):
    """涨停池记的是**行业名**，board_id 是代码——两个键都要认。

    在线烟测发现的：池里有 52 条（industry="房地产开发"），而 board_id 是
    881121，拿名字跟代码比永远匹配不上，于是七个字段全部 missing，
    而且从结果上看不出是哪儿错的。
    """

    POOL = [
        {"code": "000011", "name": "深物业A", "industry": "房地产开发", "board": 3},
        {"code": "600123", "name": "示例B", "industry": "光伏设备", "board": 2},
    ]

    def test_pool_row_matches_by_board_name(self):
        from ym_stock_data.board_strength import board_strength

        out = board_strength("881121", board_name="房地产开发", pool=self.POOL,
                            coverage=1.0)
        self.assertEqual(1, out["fields"]["limit_up_count"]["value"])

    def test_pool_row_matches_by_board_id(self):
        from ym_stock_data.board_strength import board_strength

        pool = [{"code": "000011", "board_id": "881121"}]
        out = board_strength("881121", board_name="房地产开发", pool=pool,
                            coverage=1.0)
        self.assertEqual(1, out["fields"]["limit_up_count"]["value"])

    def test_without_member_coverage_nothing_is_reported(self):
        """没给覆盖率就全给 null——**不给就不给**，不猜。

        这正是"覆盖率必须由拿着成员名单的一方算出来"的落地形态：
        管道这一侧只有涨停池，给不出成员比例，于是宁可不报。
        """
        from ym_stock_data.board_strength import board_strength

        out = board_strength("881121", board_name="房地产开发", pool=self.POOL)
        self.assertEqual("missing", out["fields"]["limit_up_count"]["status"])
        self.assertIsNone(out["fields"]["limit_up_count"]["value"])

    def test_unrelated_board_stays_empty(self):
        from ym_stock_data.board_strength import board_strength

        # 调用方声明样本齐全（coverage=1.0），那么 0 就是一次真实测量：
        # 这个板块今天确实没有涨停。凑不准才该报 null。
        out = board_strength("881999", board_name="银行", pool=self.POOL,
                             coverage=1.0)
        self.assertEqual(0, out["fields"]["limit_up_count"]["value"])
        self.assertEqual("ok", out["fields"]["limit_up_count"]["status"])
