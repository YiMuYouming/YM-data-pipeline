"""R3 item7c：code -> 所属行业/概念反查 + 概念名映射（Bots #5/#8）。

RED 事实（2026-10-26 真实探测，evidence/R3/item7/raw/probe_reverse_membership.json）：
- Bots 拿不到"某只票属于哪个行业/哪些概念"：DP 只有 board_members（板块 -> 成员）
  这一个方向，反查没有入口；
- 概念名映射也取不到：concept_index 只认 codes，names 参数被静默忽略（返回 empty 且
  不给原因）。真实名册（ths_index 全量 3102 行）里「乳品」是**行业 I 884125**、
  「婴童」是**概念 N 875111**——不区分 type 就分不清"这个概念不存在"和"它其实是行业"。

钉住的行为：
1. 新 intent stock_boards：codes 反查（行业 881 + 概念 N + 其他类型只计数），
   每行带 gaps；names 走同一意图返回名册映射（board_id/name/type）；
2. concept_index 的 names 通过 DP 既有名册（ths_index）解析：解析到概念的给出指数行，
   解析到非概念类型的如实报 type_mismatch（附真实 board_id/type），查无此名报
   CONCEPT_NAME_NOT_IN_ROSTER——三种情况的 reason_code 都能看见；
3. 取不到就进 missing/gaps，不 hardcode 别名、不猜。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ym_stock_data import api as pipeline_api
from ym_stock_data import intent_registry, provider_policy, routing

ROSTER = [
    {"ts_code": "881273.TI", "name": "白酒", "count": 19, "type": "I"},
    {"ts_code": "881126.TI", "name": "汽车零部件", "count": 187, "type": "I"},
    {"ts_code": "884125.TI", "name": "乳品", "count": 21, "type": "I"},
    {"ts_code": "885525.TI", "name": "白酒概念", "count": 48, "type": "N"},
    {"ts_code": "875111.TI", "name": "婴童", "count": 62, "type": "N"},
    {"ts_code": "885462.TI", "name": "乳业", "count": 33, "type": "N"},
    {"ts_code": "700055.TI", "name": "茅指数", "count": 42, "type": "TH"},
]

MEMBERS = {
    "600519.SH": [
        {"ts_code": "881273.TI", "con_code": "600519.SH", "con_name": "贵州茅台"},
        {"ts_code": "885525.TI", "con_code": "600519.SH", "con_name": "贵州茅台"},
        {"ts_code": "700055.TI", "con_code": "600519.SH", "con_name": "贵州茅台"},
    ],
    "001219.SZ": [
        {"ts_code": "881126.TI", "con_code": "001219.SZ", "con_name": "青岛食品"},
        {"ts_code": "885462.TI", "con_code": "001219.SZ", "con_name": "青岛食品"},
        {"ts_code": "875111.TI", "con_code": "001219.SZ", "con_name": "青岛食品"},
    ],
}


class _Clock:
    def time(self):
        return 1793000000.0

    def monotonic(self):
        return 0.0


def _outcome(status, items, *, error_code=None):
    from ym_stock_data.providers.base import ProviderOutcome

    return ProviderOutcome("stocktoday", status, data={"items": items},
                           error_code=error_code, fetched_at="2026-10-26T15:40:00+08:00")


def _provider(cache_root, *, roster=None, members=None, index_rows=None):
    from ym_stock_data.providers import stocktoday as st

    provider = st.StockTodayProvider(
        token_loader=lambda: "t", clock=_Clock(),
        roster_cache_root=Path(cache_root) / "roster",
        code_board_cache_root=Path(cache_root) / "codes",
        industry_basic_cache_root=Path(cache_root) / "industry")
    roster = ROSTER if roster is None else roster
    members = MEMBERS if members is None else members
    index_rows = {} if index_rows is None else index_rows

    def fake_request_table(name, nested, **kwargs):
        if name == "ths_index" and not nested.get("ts_code"):
            return _outcome("success", roster)
        if name == "ths_index":
            code = str(nested.get("ts_code") or "").split(".")[0]
            rows = index_rows.get(code, [])
            return _outcome("success" if rows else "empty", rows)
        if name == "ths_member":
            con_code = str(nested.get("con_code") or "")
            rows = members.get(con_code, [])
            return _outcome("success", rows)
        return _outcome("empty", [])

    provider._request_table = fake_request_table
    return provider


class RegistryAndRoutingTests(unittest.TestCase):
    def test_phrase_resolves_to_stock_boards(self):
        resolved = intent_registry.resolve_intent("查个股行业概念", codes=["600519"])
        self.assertEqual("stock_boards", resolved["intent"])
        self.assertEqual({"codes": ["600519"]}, resolved["params"])

    def test_route_targets_stocktoday_and_is_declared_in_policy(self):
        spec = routing.route_for("stock_boards", {"codes": ["600519"]})
        self.assertEqual("stock_boards", spec.intent)
        self.assertEqual(("stocktoday",), spec.providers)
        self.assertIn("stock_boards", provider_policy.CAPABILITIES)
        self.assertEqual(
            frozenset({"stocktoday"}),
            provider_policy._CAPABILITY_PROVIDER_ALLOWLIST["stock_boards"])
        self.assertEqual(
            frozenset({"ths_member", "ths_index"}),
            provider_policy._CAPABILITY_METHODS["stock_boards"])

    def test_api_params_accept_codes_or_names(self):
        pipeline_api._validate_params("stock_boards", {"codes": ["600519"]})
        pipeline_api._validate_params("stock_boards", {"names": ["婴童"]})
        with self.assertRaises(ValueError):
            pipeline_api._validate_params("stock_boards", {})
        with self.assertRaises(ValueError):
            pipeline_api._validate_params("stock_boards", {"codes": ["6005199"]})


class StockBoardsAdapterTests(unittest.TestCase):
    def test_codes_are_reversed_to_industry_and_concepts(self):
        with TemporaryDirectory() as tmp:
            provider = _provider(tmp)
            out = provider.call("stock_boards", {"codes": ["600519", "001219.SZ"]})
        rows = {row["code"]: row for row in out.data["items"]}
        self.assertEqual("白酒", rows["600519"]["industry"]["name"])
        self.assertEqual("881273", rows["600519"]["industry"]["board_id"])
        self.assertEqual(["白酒概念"],
                         [item["name"] for item in rows["600519"]["concepts"]])
        # 其他类型（TH/BB/S）只计数，不冒充行业或概念。
        self.assertEqual({"TH": 1}, rows["600519"]["other_board_type_counts"])
        self.assertEqual("汽车零部件", rows["001219"]["industry"]["name"])
        self.assertEqual({"乳业", "婴童"},
                         {item["name"] for item in rows["001219"]["concepts"]})
        self.assertIsNone(out.data["membership_as_of"])
        self.assertEqual("unknown", out.data["membership_time_status"])

    def test_names_are_mapped_through_the_roster_with_types(self):
        with TemporaryDirectory() as tmp:
            provider = _provider(tmp)
            out = provider.call("stock_boards", {"names": ["婴童", "乳品", "不存在概念"]})
        mapping = {row["requested"]: row for row in out.data["names"]}
        self.assertEqual(("resolved", "875111", "N"),
                         (mapping["婴童"]["status"], mapping["婴童"]["board_id"],
                          mapping["婴童"]["type"]))
        self.assertEqual("type_mismatch", mapping["乳品"]["status"])
        self.assertEqual("884125", mapping["乳品"]["board_id"])
        self.assertEqual("I", mapping["乳品"]["type"])
        self.assertEqual("not_found", mapping["不存在概念"]["status"])
        self.assertIn("CONCEPT_NAME_NOT_IN_ROSTER", out.data["source_gaps"])

    def test_code_without_membership_is_reported_as_gap(self):
        with TemporaryDirectory() as tmp:
            provider = _provider(tmp, members={})
            out = provider.call("stock_boards", {"codes": ["600519"]})
        row = out.data["items"][0]
        self.assertIsNone(row["industry"])
        self.assertEqual([], row["concepts"])
        self.assertIn("BOARD_MEMBERSHIP_EMPTY",
                      [gap["gap_code"] for gap in row["gaps"]])


class ConceptNameResolutionTests(unittest.TestCase):
    def test_concept_name_resolves_to_index_row(self):
        index_rows = {"875111": [{"ts_code": "875111.TI", "name": "婴童",
                                  "type": "N", "count": 62}]}
        with TemporaryDirectory() as tmp:
            provider = _provider(tmp, index_rows=index_rows)
            out = provider.call("concept_index", {"names": ["婴童"]})
        self.assertEqual(["875111.TI"], [row["ts_code"] for row in out.data["items"]])
        resolution = out.data["name_resolution"]["婴童"]
        self.assertEqual("resolved", resolution["status"])
        self.assertEqual("875111", resolution["board_id"])

    def test_industry_name_used_as_concept_is_reported_as_type_mismatch(self):
        with TemporaryDirectory() as tmp:
            provider = _provider(tmp)
            out = provider.call("concept_index", {"names": ["乳品"]})
        self.assertEqual([], out.data["items"])
        self.assertEqual(["乳品"], out.data["missing"])
        resolution = out.data["name_resolution"]["乳品"]
        self.assertEqual("type_mismatch", resolution["status"])
        self.assertEqual("CONCEPT_NAME_TYPE_MISMATCH", resolution["reason_code"])
        self.assertEqual("884125", resolution["board_id"])
        self.assertEqual("I", resolution["type"])

    def test_unknown_name_reports_not_in_roster(self):
        with TemporaryDirectory() as tmp:
            provider = _provider(tmp)
            out = provider.call("concept_index", {"names": ["不存在概念"]})
        self.assertEqual("not_found", out.data["name_resolution"]["不存在概念"]["status"])
        self.assertEqual("CONCEPT_NAME_NOT_IN_ROSTER",
                         out.data["name_resolution"]["不存在概念"]["reason_code"])


if __name__ == "__main__":
    unittest.main()
