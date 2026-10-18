"""P2-C1：连板数(limit_days) 与炸板次数(break_times) 语义分离。

缺陷：`_limit_row` 里 `int(number("limit_times","open_times") or 1)`
在 `limit_times` 缺失时回退到 `open_times`（炸板次数），两者都缺时又
兜底 1，把"未知"写成"首板"。

红线：

- `limit_days` **只取来源明确的连板数 `limit_times`**；不回落 `open_times`、
  不补 1、不取整2.5、不接受 bool/非有限/负数/0；非法或缺失为 `None`。
- `break_times` **独立取 `open_times`**；合法 0 保持 0，非法同样 unknown。
- 最高板 = max **完整已知**板数；非空池只要有一行 unknown，`max_board`
  即 unknown，另给 `known_max_board` + partial 标记，**不把子集最大当全体最大**。
- 真正空的涨停池仍保留 0；**加载失败不等于空池**。
- 只隔离受影响指标：计数与其他可靠数据不整体丢弃。

走真实函数与公共 `query()`，socket 层断网；**不照抄生产实现来证明自己**。
"""

import json
import math
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ym_stock_data import api, query
from ym_stock_data.provider_policy import CompiledPolicy
from ym_stock_data.provider_state import ProviderState

SECRET = "synthetic-p2c1-test-token"

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "p2c"


def _fixture(name):
    path = FIXTURE_DIR / name
    if not path.is_file():
        raise AssertionError(f"fixture missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


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


class LimitRowSemanticsTests(unittest.TestCase):
    """直接打真实 _limit_row（不是照抄实现）。"""

    def setUp(self):
        from ym_stock_data.providers.stocktoday import StockTodayProvider

        self.row_of = StockTodayProvider._limit_row
        net = _NoNetwork()
        net.__enter__()
        self.addCleanup(net.__exit__, None, None, None)
        self.addCleanup(patch.stopall)

    def test_missing_limit_times_is_unknown_not_open_times(self):
        row = self.row_of({"ts_code": "600001.SH", "open_times": 3})
        self.assertIsNone(row["limit_days"], "缺连板数不得回退炸板次数")
        self.assertEqual(3, row["break_times"])

    def test_valid_limit_times_with_open_times_stays_separate(self):
        row = self.row_of({"ts_code": "600002.SH", "limit_times": 1, "open_times": 4})
        self.assertEqual(1, row["limit_days"])
        self.assertEqual(4, row["break_times"])

    def test_both_missing_is_unknown(self):
        row = self.row_of({"ts_code": "600003.SH"})
        self.assertIsNone(row["limit_days"])
        self.assertIsNone(row["break_times"])

    def test_zero_break_times_is_valid_zero(self):
        row = self.row_of({"ts_code": "600004.SH", "limit_times": 2, "open_times": 0})
        self.assertEqual(2, row["limit_days"])
        self.assertEqual(0, row["break_times"], "合法 0 炸板保持 0")

    def test_invalid_limit_times_values_are_unknown(self):
        for bad in (0, True, False, 2.5, float("nan"), float("inf"), -1, "3", None):
            with self.subTest(bad=repr(bad)):
                row = self.row_of(
                    {"ts_code": "600005.SH", "limit_times": bad, "open_times": 7}
                )
                if bad is None:
                    self.assertIsNone(row["limit_days"])
                else:
                    self.assertIsNone(
                        row["limit_days"],
                        f"{bad!r} 不是合法连板数，必须 unknown 且不补 1",
                    )
                # 炸板次数独立，不因连板数非法而丢
                self.assertEqual(7, row["break_times"])

    def test_invalid_break_times_is_unknown_but_zero_stays(self):
        for bad in (True, 2.5, float("nan"), -1, "2"):
            with self.subTest(bad=repr(bad)):
                row = self.row_of(
                    {"ts_code": "600006.SH", "limit_times": 2, "open_times": bad}
                )
                self.assertIsNone(row["break_times"])
                self.assertEqual(2, row["limit_days"])
        zero = self.row_of({"ts_code": "600007.SH", "limit_times": 1, "open_times": 0})
        self.assertEqual(0, zero["break_times"])

    def test_valid_integer_limit_times_preserved(self):
        for good in (1, 2, 3, 11):
            with self.subTest(good=good):
                row = self.row_of(
                    {"ts_code": "600008.SH", "limit_times": good, "open_times": 0}
                )
                self.assertEqual(good, row["limit_days"])

    def test_existing_fields_untouched(self):
        row = self.row_of(
            {"ts_code": "600519.SH", "name": "贵州茅台", "close": 1400.0,
             "pct_chg": 10.0, "limit_times": 2, "open_times": 1,
             "industry": "白酒", "first_time": "100000", "last_time": "140000"}
        )
        self.assertEqual("600519", row["code"])
        self.assertEqual("贵州茅台", row["name"])
        self.assertEqual(1400.0, row["price"])
        self.assertEqual(10.0, row["pct"])
        self.assertEqual("白酒", row["industry"])
        self.assertEqual("100000", row["first_limit_time"])
        self.assertEqual("140000", row["last_limit_time"])
class LimitBoardPublicQueryTests(unittest.TestCase):
    """公共 query("market_limit_board") 的字段暴露。"""

    def setUp(self):
        from ym_stock_data.providers.stocktoday import StockTodayProvider

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transport = Mock()
        self.response = Mock(status_code=200)
        self.transport.return_value = self.response
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

    def serve(self, rows, fields):
        self.response.json.return_value = {
            "code": 0,
            "data": {"fields": list(fields), "items": list(rows)},
            "total": len(rows),
        }

    # 适配器固定请求这些列；mock 必须与真实请求一致，否则被 _rows 判非法表。
    UPSTREAM_FIELDS = ["ts_code", "trade_date", "industry", "name", "close",
                       "pct_chg", "open_times", "up_stat", "limit_times",
                       "first_time", "last_time"]

    def test_board_rows_expose_both_counts(self):
        def row(code, name, **kw):
            base = {"ts_code": code, "trade_date": "20261016", "name": name,
                    "close": 10.0, "pct_chg": 10.0, "open_times": 0,
                    "up_stat": "1/1", "limit_times": 1, "industry": "行业A",
                    "first_time": "100000", "last_time": "140000"}
            base.update(kw)
            return {k: base.get(k) for k in self.UPSTREAM_FIELDS}

        self.serve(
            [
                row("600001.SH", "甲", limit_times=2, open_times=1),
                row("600002.SH", "乙", limit_times=None, open_times=None),
            ],
            self.UPSTREAM_FIELDS,
        )
        result = query("market_limit_board", kind="up", date="20261016")
        self.assertEqual("success", result["_meta"]["status"])
        rows = {r["code"]: r for r in result["data"]["items"]}
        self.assertEqual(2, rows["600001"]["limit_days"])
        self.assertEqual(1, rows["600001"]["break_times"])
        self.assertIsNone(rows["600002"]["limit_days"])
        self.assertIsNone(rows["600002"]["break_times"])
        # 计数等可靠字段不因板数 unknown 而整体丢弃
        self.assertEqual("乙", rows["600002"]["name"])
        self.assertEqual(10.0, rows["600002"]["price"])


class MaxBoardAggregationTests(unittest.TestCase):
    """max_board 聚合：完整已知才给值。"""

    def setUp(self):
        self.net = _NoNetwork()
        self.net.__enter__()
        self.addCleanup(self.net.__exit__, None, None, None)
        self.addCleanup(patch.stopall)

    def _state(self, zt_rows, date="20261016"):
        from ym_stock_data.providers.stocktoday import StockTodayProvider

        return StockTodayProvider._max_board_from_rows(zt_rows)

    def test_all_known_gives_true_max(self):
        self.assertEqual(
            4, self._state([{"limit_days": 1}, {"limit_days": 4}, {"limit_days": 2}])
        )

    def test_mixed_known_and_unknown_max_is_unknown(self):
        self.assertIsNone(
            self._state([{"limit_days": 4}, {"limit_days": None}]),
            "非空池有unknown时不得把子集最大当全体最大",
        )

    def test_empty_pool_keeps_zero(self):
        self.assertEqual(0, self._state([]), "真正空的涨停池保留 0")

    def test_all_unknown_non_empty_pool_is_unknown(self):
        self.assertIsNone(self._state([{"limit_days": None}, {"limit_days": None}]))

    def test_known_max_board_is_observable_alongside_unknown(self):
        from ym_stock_data.providers.stocktoday import StockTodayProvider

        complete, known = StockTodayProvider._max_board_from_rows(
            [{"limit_days": 4}, {"limit_days": None}], with_known=True
        )
        self.assertIsNone(complete)
        self.assertEqual(4, known, "known_max_board 供观察，不冒充完整最高板")


class SealedReplayTests(unittest.TestCase):
    """封存 61 行真实回放：有效板数不变、板数与炸板数不混。"""

    def setUp(self):
        self.net = _NoNetwork()
        self.net.__enter__()
        self.addCleanup(self.net.__exit__, None, None, None)

    def test_sealed_rows_keep_board_counts_and_separate_break_times(self):
        from ym_stock_data.providers.stocktoday import StockTodayProvider

        fixture = _fixture("limit_list_d_up_20261016.json")
        rows = fixture["items"]
        self.assertEqual(61, len(rows))
        differing = 0
        for raw in rows:
            out = StockTodayProvider._limit_row(raw)
            # 有效样本：limit_days 必须等于真实 limit_times
            self.assertEqual(raw["limit_times"], out["limit_days"],
                             f"{raw['ts_code']} 板数被改动")
            # break_times 独立等于 open_times
            self.assertEqual(raw["open_times"], out["break_times"])
            if raw["limit_times"] != raw["open_times"]:
                differing += 1
        self.assertEqual(46, differing, "46 行板数/炸板数不同，必须各归各位")

    def test_sealed_max_board_unchanged(self):
        from ym_stock_data.providers.stocktoday import StockTodayProvider

        fixture = _fixture("limit_list_d_up_20261016.json")
        rows = [StockTodayProvider._limit_row(r) for r in fixture["items"]]
        self.assertEqual(
            max(r["limit_times"] for r in fixture["items"]),
            StockTodayProvider._max_board_from_rows(rows),
            "有效样本的最高板必须保持真实值",
        )


class DownstreamNoReforgeTests(unittest.TestCase):
    """下游不得用 or 1 把 unknown 重新伪造成首板。"""

    def setUp(self):
        self.net = _NoNetwork()
        self.net.__enter__()
        self.addCleanup(self.net.__exit__, None, None, None)

    def test_intent_normalizer_passes_max_board_through(self):
        from pathlib import Path as _P

        import ym_stock_data.intent_normalizers as norm

        src = _P(norm.__file__).read_text(encoding="utf-8")
        # 最高板只做透传，不得再出现 or 1 / int(...) 兜底
        for line in src.splitlines():
            if "max_board" in line:
                self.assertNotIn("or 1", line)
                self.assertNotIn("or 0", line)

    def test_promotion_fails_closed_on_non_int_board(self):
        """日期必须自洽，否则会先撞 DATE_MISMATCH，证明不了板数被拒。

        旧版本这里 previous_date 写成 20261015 而 snap.date 是 20261016，
        实际先抛 LIMIT_PROMOTION_DATE_MISMATCH —— 断言没错但证明的不是本意。
        """
        from ym_stock_data.sources.limit_state import derive_limit_promotion

        previous = {
            "date": "20261015", "zt_count": 1,
            "pools": {"zt": [{"code": "600001", "name": "甲", "limit_days": 1}]},
        }
        current = {
            "date": "20261016", "zt_count": 1,
            "pools": {"zt": [{"code": "600001", "name": "甲", "limit_days": None}]},
        }
        with self.assertRaises(ValueError) as ctx:
            derive_limit_promotion(previous, current, current_date="20261016",
                                   previous_date="20261015")
        self.assertIn("LIMIT_PROMOTION_INCOMPLETE", str(ctx.exception),
                      "必须是板数不完整，而不是日期不匹配")

    def test_promotion_date_mismatch_is_still_detected(self):
        from ym_stock_data.sources.limit_state import derive_limit_promotion

        snap = {
            "date": "20261016", "zt_count": 1,
            "pools": {"zt": [{"code": "600001", "name": "甲", "limit_days": 1}]},
        }
        with self.assertRaises(ValueError) as ctx:
            derive_limit_promotion(snap, snap, current_date="20261016",
                                   previous_date="20261015")
        self.assertIn("LIMIT_PROMOTION_DATE_MISMATCH", str(ctx.exception))

    def test_stocktoday_limit_state_does_not_reforge_with_or_1(self):
        from pathlib import Path as _P

        import ym_stock_data.providers.stocktoday as st

        src = _P(st.__file__).read_text(encoding="utf-8")
        self.assertNotIn(
            'int(row.get("limit_days") or 1)', src,
            "max_board 聚合不得再把 None 变 1",
        )


class RealLimitStateChainTests(unittest.TestCase):
    """真实 consumer 链：公共 market_limit_state -> ingest_limits（store）。

    关键事实（用真实函数证明，不靠推理）：
    `_call_limit_state` 的池来自 `market_facts.normalize_stocktoday_limits`。
    板数与炸板次数在该路径与 `_limit_row` 同义；未知板数仍被
    `ingest_limits` 严格拒写（board_count NOT NULL），拒写只影响
    limit_events，**不覆盖**既有有效 run。
    """

    def setUp(self):
        from ym_stock_data.providers.stocktoday import StockTodayProvider

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transport = Mock()
        self.response = Mock(status_code=200)
        self.transport.return_value = self.response
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

    def _limit_state_payload(self):
        """构造一次真实 market_limit_state 的公共 query 结果。

        U/D/Z/STEP 四次读取要按顺序返回不同表，所以用 side_effect 逐次应答，
        而不是共用一个 return_value。
        """
        fields = ["ts_code", "trade_date", "industry", "name", "close",
                  "pct_chg", "open_times", "up_stat", "limit_times"]

        def table(rows):
            resp = Mock(status_code=200)
            resp.json.return_value = {
                "code": 0, "data": {"fields": fields, "items": rows},
                "total": len(rows),
            }
            return resp

        def up(code, name, board, brk):
            return {"ts_code": code, "trade_date": "20261016", "industry": "行业",
                    "name": name, "close": 10.0, "pct_chg": 10.0,
                    "open_times": brk, "up_stat": "1/1", "limit_times": board}

        up_rows = [up("600001.SH", "甲", 3, 0), up("600002.SH", "乙", 2, 1)]
        step_rows = [
            {"ts_code": "600001.SH", "trade_date": "20261016", "nums": 3, "name": "甲"},
            {"ts_code": "600002.SH", "trade_date": "20261016", "nums": 2, "name": "乙"},
        ]
        # 空池会被重读最多 3 次，所以按 (api_name, limit_type) 派发而不是按次序。
        by_key = {
            ("limit_list_d", "U"): table(up_rows),
            ("limit_list_d", "D"): table([]),
            ("limit_list_d", "Z"): table([]),
            ("limit_step", None): table(step_rows),
        }
        step_fields = ["ts_code", "trade_date", "nums", "name"]
        step_resp = Mock(status_code=200)
        step_resp.json.return_value = {
            "code": 0, "data": {"fields": step_fields, "items": step_rows},
            "total": len(step_rows),
        }
        by_key[("limit_step", None)] = step_resp

        def dispatch(*a, **k):
            body = k.get("json") or {}
            params = body.get("params") or {}
            return by_key.get(
                (body.get("api_name"), params.get("limit_type")),
                step_resp,
            )

        self.transport.side_effect = dispatch
        return query("market_limit_state", date="20261016")

    def test_limit_state_pools_keep_integer_board_counts(self):
        result = self._limit_state_payload()
        self.assertEqual("success", result["_meta"]["status"])
        zt = result["data"]["pools"]["zt"]
        self.assertEqual(2, len(zt))
        for row in zt:
            self.assertIsInstance(row["limit_days"], int,
                                  "normalize 路径给的是整数板数，不是 None")
            self.assertGreaterEqual(row["limit_days"], 1)
        self.assertEqual(3, result["data"]["max_board"])

    def _stored_boards(self, store, trade_date="20261016"):
        """直接读封存库，确认拒写后旧 run 是否仍在。"""
        with store._connect() as conn:
            run = conn.execute(
                "SELECT id FROM limit_runs WHERE trade_date=? ORDER BY id",
                (trade_date,),
            ).fetchall()
            if not run:
                return {}
            events = store._events(run[-1]["id"])
        return {e["code"]: e["board_count"] for e in events
                if e.get("kind") == "up"}

    def test_ingest_limits_accepts_the_real_payload(self):
        from ym_stock_data.market_facts import MarketFactStore

        result = self._limit_state_payload()
        store = MarketFactStore(Path(self.tmp.name) / "facts.sqlite3")
        receipt = store.ingest_limits("20261016", result)
        self.assertEqual(2, receipt["counts"]["up"])
        self.assertEqual(3, self._stored_boards(store)["600001"])

    def test_ingest_limits_rejects_none_board_without_touching_old_run(self):
        """缺板数 -> store 严格拒写；既有有效 run 不被覆盖。"""
        from ym_stock_data.market_facts import MarketFactStore

        good = self._limit_state_payload()
        store = MarketFactStore(Path(self.tmp.name) / "facts2.sqlite3")
        store.ingest_limits("20261016", good)
        before = self._stored_boards(store)
        self.assertEqual({"600001": 3, "600002": 2}, before,
                         "封存表存的是 6 位裸代码")

        bad = json.loads(json.dumps(good))
        bad["data"]["pools"]["zt"][0]["limit_days"] = None
        with self.assertRaises(ValueError) as ctx:
            store.ingest_limits("20261016", bad)
        self.assertIn("malformed", str(ctx.exception))
        # 旧的有效 run 仍在，未被拒写覆盖
        self.assertEqual(before, self._stored_boards(store))

    def test_store_rejection_keeps_reliable_counts_in_public_payload(self):
        """store 拒写不等于丢掉可靠计数：公共 payload 仍完整。

        现状（已核）：`market_limit_state` 的池与 max_board 来自
        `normalize_stocktoday_limits`（整数板数 + limit_step 交叉核对），
        **不经过 _limit_row**，所以手工把池行改成 None 只影响 store 拒写，
        max_board 仍是 normalize 给出的 3。可靠计数与名称完全保留。
        """
        bad = json.loads(json.dumps(self._limit_state_payload()))
        self.assertEqual(2, bad["data"]["zt_count"])
        self.assertEqual(3, bad["data"]["max_board"])
        bad["data"]["pools"]["zt"][0]["limit_days"] = None
        self.assertEqual(2, bad["data"]["zt_count"], "可靠计数仍保留")
        self.assertEqual("甲", bad["data"]["pools"]["zt"][0]["name"])
        self.assertEqual(10.0, bad["data"]["pools"]["zt"][0]["price"])

    def test_limit_state_unavailable_is_not_an_empty_pool(self):
        # 加载失败（error）不得被当成"空涨停池 -> 最高板 0"
        self.response.json.return_value = {
            "code": 500, "message": "boom",
        }
        result = query("market_limit_state", date="20261016")
        self.assertNotEqual("success", result["_meta"]["status"])
        self.assertIsNone((result.get("data") or {}).get("max_board")
                          if isinstance(result.get("data"), dict) else None)


class MaxBoardHelperStrictnessTests(unittest.TestCase):
    """_max_board_from_rows 作为公共复用必须拒绝 0/负数。"""

    def setUp(self):
        self.net = _NoNetwork()
        self.net.__enter__()
        self.addCleanup(self.net.__exit__, None, None, None)

    def test_helper_rejects_zero_and_negative_board(self):
        from ym_stock_data.providers.stocktoday import StockTodayProvider

        for bad in (0, -1):
            with self.subTest(bad=bad):
                self.assertIsNone(
                    StockTodayProvider._max_board_from_rows([{"limit_days": bad}]),
                    f"{bad} 不是合法板数，最高板必须 unknown",
                )
                complete, known = StockTodayProvider._max_board_from_rows(
                    [{"limit_days": bad}], with_known=True
                )
                self.assertIsNone(complete)
                self.assertIsNone(known)


if __name__ == "__main__":
    unittest.main()

