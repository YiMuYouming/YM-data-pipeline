"""P2-C1 复核返修 round3：真实链上缺板数不得被补成确定值。

主控核实 `market_facts.normalize_stocktoday_limits`（约 L105）：
`step_boards.get(code, (1, ""))[0]` 在 `limit_times` 缺失时回退 STEP 板数，
STEP 也无该码时回退**字面量 1**；`_call_limit_state` 随后聚合这些数。
所以上一版报告「None 不进入该路径所以完整」的结论**是错的**。

本文件覆盖真实链：公共 `query("market_limit_state")` 输入含缺板数的 U 行
且 STEP 无该码，要求：
- 缺板数行的 `limit_days` 为 None，`max_board` 为 unknown；
- 有明确 quality gap，不把 `reference_unverified` STEP 当确定板数；
- `known_max_board` 只含有证据的板数；
- 名称 / 价格 / 计数等可靠字段保留；
- STEP 交叉核验**来源可留**，但缺原值不补 1，非法 `nums` 不 int 截断；
- 严格 store 拒写不完整板数，旧有效 run 原子不变。

走真实函数与公共 query，socket 层断网。
"""

import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ym_stock_data import api, query
from ym_stock_data.market_facts import normalize_stocktoday_limits
from ym_stock_data.provider_policy import CompiledPolicy
from ym_stock_data.provider_state import ProviderState

SECRET = "synthetic-p2c1-round3-test-token"


class _NetworkBlocked(RuntimeError):
    """任何真实网络访问都让测试立即失败。"""


class _NoNetwork:
    """socket 层断网。"""

    def __enter__(self):
        def blocked(*args, **kwargs):
            raise _NetworkBlocked(
                "network access attempted in an offline replay test"
            )

        self._patches = [
            patch.object(socket, "socket", blocked),
            patch.object(socket, "create_connection", blocked),
            patch.object(socket, "getaddrinfo", blocked),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


def _meta(status="success"):
    return {
        "status": status,
        "provider_used": "stocktoday",
        "fetched_at": "2026-10-16T18:17:01+08:00",
    }


def _res(items):
    return {
        "_meta": _meta(),
        "data": {
            "items": items,
            "truncated": False,
            "total": len(items),
            "total_present": True,
        },
    }


def _u(code, name, board, *, close=10.0, brk=0, industry="行业A"):
    # 必须覆盖 fields 声明的全部列，否则 _rows 判 invalid row width。
    return {
        "ts_code": code, "trade_date": "20261016", "industry": industry,
        "name": name, "close": close, "pct_chg": 10.0, "open_times": brk,
        "up_stat": "1/1", "limit_times": board, "limit_type": "U",
    }


def _step(code, nums, name="甲"):
    # 同理：覆盖 limit_step 声明的全部列。
    return {"ts_code": code, "trade_date": "20261016", "nums": nums,
            "name": name}


class NormalizeBoardFallbackTests(unittest.TestCase):
    """直接打真实 normalize_stocktoday_limits。"""

    def setUp(self):
        net = _NoNetwork()
        net.__enter__()
        self.addCleanup(net.__exit__, None, None, None)
        self.addCleanup(patch.stopall)

    def _norm(self, up, step, *, day="20261016"):
        return normalize_stocktoday_limits(day, {
            "U": _res(up), "D": _res([]), "Z": _res([]), "STEP": _res(step),
        })

    def test_missing_limit_times_without_step_code_is_unknown(self):
        out = self._norm(
            [_u("600001.SH", "甲", 3), _u("600002.SH", "乙", None)],
            [_step("600001.SH", 3, "甲")],
        )
        rows = {r["code"]: r for r in out["data"]["pools"]["zt"]}
        self.assertIsNone(
            rows["600002"]["limit_days"],
            "缺 limit_times 且 STEP 无该码 -> 必须 unknown，不能补 1",
        )
        self.assertEqual(3, rows["600001"]["limit_days"])

    def test_missing_limit_times_with_step_code_keeps_source_but_marks_unverified(self):
        # STEP 交叉核验来源可留，但它是 reference，不是确定板数。
        out = self._norm(
            [_u("600002.SH", "乙", None)],
            [_step("600002.SH", 4, "乙")],
        )
        row = out["data"]["pools"]["zt"][0]
        self.assertIsNone(
            row["limit_days"],
            "STEP 只是参考来源，缺原值时不得当成确定板数",
        )
        # 来源标记仍然保留，便于追溯
        self.assertIn("step", str(out["data"]["_board_source"]))

    def test_reliable_fields_survive_unknown_board(self):
        # STEP 里的非 ST 码必须出现在U 池，否则既有守卫会先抛
        # historical non-ST ladder stock absent from up-pool。
        out = self._norm(
            [_u("600001.SH", "甲", 3),
             _u("600002.SH", "乙", None, close=12.5, brk=3, industry="行业B")],
            [_step("600001.SH", 3, "甲")],
        )
        row = {r["code"]: r for r in out["data"]["pools"]["zt"]}["600002"]
        self.assertIsNone(row["limit_days"])
        self.assertEqual("乙", row["name"], "名称必须保留")
        self.assertEqual(12.5, row["price"], "价格必须保留")
        self.assertEqual(10.0, row["pct"])
        self.assertEqual(3, row["break_times"], "炸板次数独立且保留")
        self.assertEqual("行业B", row["industry"])
        self.assertEqual(2, out["data"]["zt_count"], "计数不因板数未知而丢弃")

    def test_illegal_step_nums_is_not_int_truncated(self):
        """需要截断的 nums 必须被拒，不得 int() 洗成合法板数。

        纯数字字符串（"3"）按整数接受：既有合同与调用链都传这种形态，
        封存证据不足以判定它非法；"2.5"/2.5/bool/NaN 这类**需要截断或无意义**
        的一律 fail closed。
        """
        for bad in (2.5, "2.5", None, True, False, float("nan"), float("inf")):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError) as ctx:
                    self._norm(
                        [_u("600001.SH", "甲", None)],
                        [_step("600001.SH", bad, "甲")],
                    )
                self.assertIn("board count invalid", str(ctx.exception))

    def test_clean_numeric_string_nums_accepted(self):
        """既有调用链传字符串 nums；不回归这一形态。"""
        out = self._norm(
            [_u("600001.SH", "甲", None)],
            [_step("600001.SH", "4", "甲")],
        )
        self.assertIsNone(
            out["data"]["pools"]["zt"][0]["limit_days"],
            "STEP 只是参考，缺 limit_times 仍不给确定板数",
        )

    def test_valid_limit_times_still_wins_and_disagreement_is_flagged(self):
        out = self._norm(
            [_u("600001.SH", "甲", 2)],
            [_step("600001.SH", 5, "甲")],
        )
        self.assertEqual(2, out["data"]["pools"]["zt"][0]["limit_days"])
        self.assertIn("disagreement", str(out["data"]["_board_source"]))

    def test_all_known_pool_is_unchanged(self):
        out = self._norm(
            [_u("600001.SH", "甲", 3), _u("600002.SH", "乙", 2)],
            [_step("600001.SH", 3, "甲"), _step("600002.SH", 2, "乙")],
        )
        rows = {r["code"]: r for r in out["data"]["pools"]["zt"]}
        self.assertEqual({"600001": 3, "600002": 2},
                         {c: r["limit_days"] for c, r in rows.items()})
        self.assertEqual("limit_step_crosschecked", out["data"]["_board_source"])


class PublicLimitStateChainTests(unittest.TestCase):
    """真实链：公共 query("market_limit_state") 遇缺板数 U 行。

    要求：max_board=unknown +明确 quality gap；known_max_board 只含有证据的板数；
    名称/价格/计数保留；不把 reference_unverified 的 STEP 当确定板数。
    """

    def setUp(self):
        from ym_stock_data.providers.stocktoday import StockTodayProvider

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transport = Mock()
        self.provider = StockTodayProvider(
            token_loader=lambda: SECRET,
            post=self.transport,
            budget_path=Path(self.tmp.name) / "budget.sqlite3",
            member_cache_root=Path(self.tmp.name) / "members",
        )
        self.state = ProviderState(Path(self.tmp.name) / "state.sqlite3")
        self.addCleanup(patch.stopall)
        net = _NoNetwork()
        net.__enter__()
        self.addCleanup(net.__exit__, None, None, None)
        patch.object(
            api, "load_compiled_policy",
            return_value=CompiledPolicy(None, "inactive", None),
        ).start()
        patch.object(api, "_STATE", self.state).start()
        patch.dict(api.PROVIDER_REGISTRY, {"stocktoday": self.provider}).start()

    def _query_limit_state(self, up_rows, step_rows):
        fields = ["ts_code", "trade_date", "industry", "name", "close",
                  "pct_chg", "open_times", "up_stat", "limit_times"]
        step_fields = ["ts_code", "trade_date", "nums", "name"]

        def table(rows, flds):
            resp = Mock(status_code=200)
            resp.json.return_value = {
                "code": 0, "data": {"fields": flds, "items": rows},
                "total": len(rows),
            }
            return resp

        by_key = {
            ("limit_list_d", "U"): table(up_rows, fields),
            ("limit_list_d", "D"): table([], fields),
            ("limit_list_d", "Z"): table([], fields),
            ("limit_step", None): table(step_rows, step_fields),
        }
        fallback = table(step_rows, step_fields)

        def dispatch(*a, **k):
            body = k.get("json") or {}
            params = body.get("params") or {}
            return by_key.get((body.get("api_name"), params.get("limit_type")),
                              fallback)

        self.transport.side_effect = dispatch
        return query("market_limit_state", date="20261016")

    def test_public_query_marks_max_board_unknown_with_gap(self):
        result = self._query_limit_state(
            [_u("600001.SH", "甲", 3), _u("600002.SH", "乙", None, close=12.5)],
            [_step("600001.SH", 3, "甲")],
        )
        self.assertIn(result["_meta"]["status"], {"success", "degraded"})
        self.assertIsNotNone(result.get("data"),
                            f"data 缺失: {result['_meta']['attempts']}")
        data = result["data"]
        rows = {r["code"]: r for r in data["pools"]["zt"]}
        self.assertIsNone(rows["600002"]["limit_days"])
        self.assertIsNone(
            data["max_board"],
            "非空池有未知板数 -> max_board 必须 unknown",
        )
        self.assertEqual(
            3, data["known_max_board"],
            "known_max_board 只含有证据的板数",
        )
        # 可靠字段保留
        self.assertEqual("乙", rows["600002"]["name"])
        self.assertEqual(12.5, rows["600002"]["price"])
        self.assertEqual(2, data["zt_count"])
        # 明确 quality gap：只看公共质量字段，不能把业务行里的 limit_days 键当成缺口。
        quality = result["_meta"]["quality"]
        self.assertEqual("partial", quality["status"], quality)
        reasons = [str(code).lower() for code in (quality.get("reason_codes") or [])]
        self.assertTrue(
            any("limit_days" in code or "board" in code for code in reasons),
            reasons,
        )
        gaps = data.get("source_gaps") or quality.get("gaps") or []
        self.assertTrue(gaps, "quality gap must be explicit, not inferred from payload keys")
        self.assertTrue(
            any(
                isinstance(gap, dict)
                and "600002" in [str(code) for code in (gap.get("affected_codes") or [])]
                for gap in gaps
            ),
            gaps,
        )

    def test_all_known_public_query_still_gives_true_max(self):
        result = self._query_limit_state(
            [_u("600001.SH", "甲", 3), _u("600002.SH", "乙", 2)],
            [_step("600001.SH", 3, "甲"), _step("600002.SH", 2, "乙")],
        )
        data = result["data"]
        self.assertEqual(3, data["max_board"])
        self.assertEqual(3, data["known_max_board"])


class StoreRejectionRound3Tests(unittest.TestCase):
    """严格 store 拒写不完整板数；旧有效 run 原子不变。"""

    def setUp(self):
        net = _NoNetwork()
        net.__enter__()
        self.addCleanup(net.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _payload(self, up_rows, step_rows, day="20261016"):
        return normalize_stocktoday_limits(day, {
            "U": _res(up_rows), "D": _res([]), "Z": _res([]),
            "STEP": _res(step_rows),
        })

    def _boards(self, store, day="20261016"):
        with store._connect() as conn:
            run = conn.execute(
                "SELECT id FROM limit_runs WHERE trade_date=? ORDER BY id",
                (day,),
            ).fetchall()
            if not run:
                return {}
            events = store._events(run[-1]["id"])
        return {e["code"]: e["board_count"] for e in events
                if e.get("kind") == "up"}

    def test_store_rejects_unknown_board_and_keeps_old_run(self):
        from ym_stock_data.market_facts import MarketFactStore

        store = MarketFactStore(Path(self.tmp.name) / "f.sqlite3")
        good = self._payload(
            [_u("600001.SH", "甲", 3), _u("600002.SH", "乙", 2)],
            [_step("600001.SH", 3, "甲"), _step("600002.SH", 2, "乙")],
        )
        store.ingest_limits("20261016", good)
        before = self._boards(store)
        self.assertEqual({"600001": 3, "600002": 2}, before)

        bad = self._payload(
            [_u("600001.SH", "甲", 3), _u("600002.SH", "乙", None)],
            [_step("600001.SH", 3, "甲")],
        )
        with self.assertRaises(ValueError) as ctx:
            store.ingest_limits("20261016", bad)
        self.assertIn("malformed", str(ctx.exception))
        self.assertEqual(before, self._boards(store),
                         "拒写后旧有效 run 原子不变")

    def test_refresh_rejects_only_limits_and_can_continue_daily(self):
        """生产 refresh 拒写 limit_events 后仍可继续独立 OHLC（不整包中止）。"""
        from ym_stock_data.market_facts import MarketFactStore

        store = MarketFactStore(Path(self.tmp.name) / "g.sqlite3")
        store.ingest_limits("20261016", self._payload(
            [_u("600001.SH", "甲", 3)], [_step("600001.SH", 3, "甲")]))
        before = self._boards(store)

        bad = self._payload([_u("600001.SH", "甲", None)],
                            [_step("600001.SH", 3, "甲")])
        try:
            store.ingest_limits("20261016", bad)
            self.fail("缺板数必须被 store 拒写")
        except (ValueError, OSError) as err:
            self.assertIn("malformed", str(err))
        # __main__ 的 refresh 捕获该异常后继续 daily_ohlc；
        # 这里断言 limit_events 未被污染，OHLC 通道独立可用。
        self.assertEqual(before, self._boards(store))


if __name__ == "__main__":
    unittest.main()
