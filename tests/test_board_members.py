"""W4：board_members 意图（审计回复 11 第二节第 2 条）。

成员关系的唯一生产者：StockToday 的 `ths_member`，行业和概念都用这一个，
按交易日缓存（成员变动慢）。Market_Watch 侧的 member_coverage、持仓所属板块、
持仓参考组全部从这里取——不保留第二个来源。

在线实测（2026-10-02）钉住的三件事：
1. `ts_code` 必须带 `.TI` 后缀：`881121` 查不到，`881121.TI` 返回 187 名成员；
2. 逗号合并多板块返回空——只能一个板块一次调用；
3. 板块码能解析到美股指数（`861292.TI` 房地产开发 = exchange US，成员是
   JOE.N 这类），所以成员列表要能原样带交易所后缀返回，由调用方判 A 股。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ym_stock_data import api as pipeline_api
from ym_stock_data import intent_registry, provider_policy, routing
from ym_stock_data.contracts import ProviderAttempt
from ym_stock_data.intent_normalizers import normalize_success


def _normalize(intent, params, data, provider):
    spec = routing.route_for(intent, params)
    return normalize_success(
        intent, params, data, provider=provider, spec=spec,
        attempts=[ProviderAttempt(provider=provider, status="ok",
                                  error_code=None, latency_ms=1)],
    )


def _member_rows(board_code, codes):
    return [{"ts_code": board_code, "con_code": code,
             "con_name": None, "is_new": None, "weight": None,
             "in_date": "0", "out_date": None} for code in codes]


class BoardMembersRouteTests(unittest.TestCase):
    """registry → routing → provider_policy（capability/允许 provider/方法/时效）。"""

    def test_phrase_resolves_to_board_members(self):
        resolved = intent_registry.resolve_intent("查板块成员", codes=["881121"])
        self.assertEqual("board_members", resolved["intent"])
        self.assertEqual({"codes": ["881121"]}, resolved["params"])

    def test_route_targets_stocktoday(self):
        spec = routing.route_for("board_members", {"codes": ["881121"]})
        self.assertEqual("board_members", spec.intent)
        self.assertEqual(("stocktoday",), spec.providers)

    def test_capability_declared_with_ths_member_method(self):
        capability = routing.capability_for("board_members", {"codes": ["881121"]})
        self.assertEqual("board_members", capability)
        self.assertIn(capability, provider_policy.CAPABILITIES)
        self.assertEqual(
            frozenset({"stocktoday"}),
            provider_policy._CAPABILITY_PROVIDER_ALLOWLIST[capability],
        )
        self.assertEqual(
            frozenset({"ths_member"}),
            provider_policy._CAPABILITY_METHODS[capability],
        )

    def test_members_are_cached_by_trade_day(self):
        """成员变动慢：时效按交易日给，不是分钟级。"""
        self.assertGreaterEqual(
            provider_policy._BASE_MAX_AGE_SEC["board_members"], 86400)


class BoardMembersApiTests(unittest.TestCase):
    """api 层参数校验：形状检查只挡形状（K1 第二道闸）。"""

    def test_industry_and_concept_codes_are_accepted(self):
        for code in ("881121", "885757", "864005"):
            pipeline_api._validate_params("board_members", {"codes": [code]})

    def test_stock_code_prefix_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            pipeline_api._validate_params("board_members", {"codes": ["600519"]})
        self.assertIn("board prefix", str(ctx.exception))

    def test_requires_codes(self):
        with self.assertRaises(ValueError) as ctx:
            pipeline_api._validate_params("board_members", {"codes": []})
        self.assertIn("requires codes", str(ctx.exception))

    def test_trade_date_accepts_both_written_forms(self):
        params = {"codes": ["881121"], "trade_date": "2026-09-30"}
        pipeline_api._validate_params("board_members", params)
        self.assertEqual("20260930", params["trade_date"])

    def test_trade_date_must_be_a_date(self):
        with self.assertRaises(ValueError):
            pipeline_api._validate_params(
                "board_members", {"codes": ["881121"], "trade_date": "昨天"})


class BoardMembersAdapterTests(unittest.TestCase):
    """适配器：后缀归一、逐板块取数、按交易日缓存、data_as_of。"""

    def _provider(self, rows_by_board, *, cache_root, calls=None):
        from ym_stock_data.providers import stocktoday as st

        provider = st.StockTodayProvider(
            token_loader=lambda: "t", member_cache_root=Path(cache_root))

        def _fake_request_table(name, nested, **kwargs):
            if calls is not None:
                calls.append((name, dict(nested)))
            code = str(nested.get("ts_code") or "")
            return st.ProviderOutcome(
                "stocktoday", "success" if code in rows_by_board else "empty",
                data={"items": rows_by_board.get(code, []), "_stocktoday": {}},
                fetched_at="2026-10-02T21:00:00+08:00", latency_ms=1)

        provider._request_table = _fake_request_table
        return provider

    def test_ti_suffix_is_appended_and_requested_id_is_echoed(self):
        rows = {"881121.TI": _member_rows("881121.TI", ["000011.SZ", "600123.SH"])}
        with TemporaryDirectory() as tmp:
            provider = self._provider(rows, cache_root=tmp)
            out = provider.call("board_members",
                                {"codes": ["881121"], "trade_date": "20260930"})
        board = out.data["boards"][0]
        self.assertEqual("881121", board["board_id"],
                         "回显要用调用方给的板块 id，不是上游的 .TI 形式")
        self.assertEqual(["000011.SZ", "600123.SH"], board["members"])
        self.assertEqual(2, board["member_count"])
        self.assertEqual("20260930", out.data["data_as_of"])

    def test_one_board_per_upstream_call(self):
        """合并查询上游返回空（在线实测），必须逐板块调。"""
        rows = {
            "881121.TI": _member_rows("881121.TI", ["000011.SZ"]),
            "885757.TI": _member_rows("885757.TI", ["300020.SZ"]),
        }
        calls = []
        with TemporaryDirectory() as tmp:
            provider = self._provider(rows, cache_root=tmp, calls=calls)
            provider.call("board_members",
                          {"codes": ["881121", "885757"], "trade_date": "20260930"})
        self.assertEqual(2, len(calls))
        self.assertEqual({"881121.TI", "885757.TI"},
                         {nested["ts_code"] for _, nested in calls})

    def test_board_without_members_is_reported_missing(self):
        with TemporaryDirectory() as tmp:
            provider = self._provider({}, cache_root=tmp)
            out = provider.call("board_members",
                                {"codes": ["881121"], "trade_date": "20260930"})
        self.assertEqual([], out.data["boards"])
        self.assertEqual(["881121"], out.data["missing"])

    def test_second_call_same_day_hits_cache(self):
        rows = {"881121.TI": _member_rows("881121.TI", ["000011.SZ"])}
        calls = []
        with TemporaryDirectory() as tmp:
            provider = self._provider(rows, cache_root=tmp, calls=calls)
            provider.call("board_members", {"codes": ["881121"],
                                            "trade_date": "20260930"})
            provider.call("board_members", {"codes": ["881121"],
                                            "trade_date": "20260930"})
        self.assertEqual(1, len(calls), "同一交易日的第二次调用应当走缓存")

    def test_cache_is_keyed_by_trade_day(self):
        rows = {"881121.TI": _member_rows("881121.TI", ["000011.SZ"])}
        calls = []
        with TemporaryDirectory() as tmp:
            provider = self._provider(rows, cache_root=tmp, calls=calls)
            provider.call("board_members", {"codes": ["881121"],
                                            "trade_date": "20260929"})
            provider.call("board_members", {"codes": ["881121"],
                                            "trade_date": "20260930"})
        self.assertEqual(2, len(calls), "换一个交易日必须重新取")

    def test_cache_survives_provider_restart(self):
        """缓存落在盘上，不是进程内存——换一个 provider 实例也要命中。"""
        rows = {"881121.TI": _member_rows("881121.TI", ["000011.SZ"])}
        calls = []
        with TemporaryDirectory() as tmp:
            first = self._provider(rows, cache_root=tmp, calls=calls)
            first.call("board_members", {"codes": ["881121"],
                                         "trade_date": "20260930"})
            second = self._provider(rows, cache_root=tmp, calls=calls)
            out = second.call("board_members", {"codes": ["881121"],
                                                "trade_date": "20260930"})
            self.assertEqual(1, len(calls))
            self.assertEqual(["000011.SZ"], out.data["boards"][0]["members"])
            cached = json.loads(
                (Path(tmp) / "20260930.json").read_text(encoding="utf-8"))
            self.assertIn("881121", cached["boards"])

    def test_cached_day_skips_upstream_even_for_uncached_board(self):
        """同日缓存只补缺的板块，不整日作废重取。"""
        rows = {
            "881121.TI": _member_rows("881121.TI", ["000011.SZ"]),
            "881155.TI": _member_rows("881155.TI", ["600789.SH"]),
        }
        calls = []
        with TemporaryDirectory() as tmp:
            provider = self._provider(rows, cache_root=tmp, calls=calls)
            provider.call("board_members", {"codes": ["881121"],
                                            "trade_date": "20260930"})
            provider.call("board_members", {"codes": ["881121", "881155"],
                                            "trade_date": "20260930"})
        self.assertEqual(2, len(calls))
        # 第二次调用只补缺的 881155；calls 是累积的，取第二次起
        self.assertEqual({"881155.TI"},
                         {nested["ts_code"] for _, nested in calls[1:]})

    def test_upstream_failure_is_not_swallowed_into_cache(self):
        """取数失败的板块不进缓存——下一次调用还能再试。"""
        from ym_stock_data.providers import stocktoday as st

        with TemporaryDirectory() as tmp:
            provider = st.StockTodayProvider(
                token_loader=lambda: "t", member_cache_root=Path(tmp))
            provider._request_table = (
                lambda name, nested, **kw: st.ProviderOutcome(
                    "stocktoday", "error", error_code="UPSTREAM_ERROR"))
            out = provider.call("board_members",
                                {"codes": ["881121"], "trade_date": "20260930"})
        self.assertEqual("error", out.status)
        self.assertEqual("UPSTREAM_ERROR", out.error_code)
        self.assertFalse((Path(tmp) / "20260930.json").exists())


class BoardMembersNormalizerTests(unittest.TestCase):
    """归一：板块计数、missing 透传。"""

    def test_normalizer_counts_boards_and_passes_missing(self):
        payload = {
            "data_as_of": "20260930",
            "boards": [{"board_id": "881121", "members": ["000011.SZ"],
                        "member_count": 1}],
            "missing": ["881999"],
        }
        business, quality = _normalize(
            "board_members", {"codes": ["881121", "881999"]},
            payload, "stocktoday")
        self.assertEqual(payload["boards"], business["boards"])
        self.assertEqual(["881999"], business["missing"])
        self.assertEqual(1, quality["returned_count"])
        self.assertEqual(["881999"], quality["missing"])


if __name__ == "__main__":
    unittest.main()


class BoardMembersFreshnessTests(unittest.TestCase):
    """按交易日缓存的成员快照，不按 fetched_at 年龄判 stale。

    在线教训（2026-10-03 回放）：缓存是 10-02 抓的 9-30 名册，10-03 回放时
    fetched_at 超过 max_age(86400) 被判 QUALITY_STALE、整条查询作废——
    对"某交易日名册"这是错的轴：data_as_of 已经是交易日，缓存保证 as-of 语义。
    """

    def test_cached_trade_day_members_are_not_stale_by_fetch_age(self):
        from ym_stock_data import api as pipeline_api
        from datetime import datetime, timedelta

        now = datetime(2026, 10, 3, 21, 40)
        old_fetch = (now - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
        # 直接打质量判定的内部函数：board_members 且 fetched_at 很旧 → None（不 stale）
        from ym_stock_data.providers.base import ProviderOutcome

        outcome = ProviderOutcome(
            "stocktoday", "success", data={"data_as_of": "20260930",
                                           "boards": []},
            fetched_at=old_fetch, latency_ms=1)
        failure = pipeline_api._quality_failure_code(
            "board_members", {"trade_date": "20260930"}, outcome.data, outcome,
            86400)
        self.assertIsNone(failure, "昨天的缓存被按 fetched_at 判 stale 了")

    def test_market_facts_style_date_param_stays_historical(self):
        """对照：显式 trade_date 的查询新鲜度标记 historical，不是 stale。"""
        from ym_stock_data import api as pipeline_api
        from datetime import datetime

        freshness = pipeline_api._data_freshness(
            "board_members", {"trade_date": "20260930"},
            datetime(2026, 9, 30, 15, 0), 86400)
        self.assertEqual("historical", freshness["status"])
