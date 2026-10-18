"""P2-C1 终修：公共 query 质量缺口、炸板同义、close 不连带清空。

独立审查失败 ID 归并为 R1/R2/R3。本模块走真实 public query / normalize /
store，transport mock，socket 断网；断言质量字段本身，不靠业务载荷里
碰巧出现 limit_days 键。
"""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from ym_stock_data import api, query
from ym_stock_data.market_facts import MarketFactStore, normalize_stocktoday_limits
from ym_stock_data.provider_policy import CompiledPolicy
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.stocktoday import StockTodayProvider

SECRET = "synthetic-p2c1-final-test-token"
DAY = "20261016"
STAMP = "2026-10-16T17:10:00+08:00"


class _NetworkBlocked(RuntimeError):
    """任何真实网络访问都让测试立即失败。"""


class _NoNetwork:
    def __enter__(self):
        def blocked(*args, **kwargs):
            raise _NetworkBlocked("network access attempted in an offline replay test")

        self._patches = [
            patch.object(socket, "socket", blocked),
            patch.object(socket, "create_connection", blocked),
            patch.object(socket, "getaddrinfo", blocked),
        ]
        for item in self._patches:
            item.start()
        return self

    def __exit__(self, *exc):
        for item in self._patches:
            item.stop()
        return False


def _meta():
    return {
        "status": "success",
        "provider_used": "stocktoday",
        "fetched_at": STAMP,
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


def _u(code, name, board, *, close=13.7, brk=0, pct=9.98, industry="合成行业"):
    return {
        "ts_code": code,
        "trade_date": DAY,
        "industry": industry,
        "name": name,
        "close": close,
        "pct_chg": pct,
        "open_times": brk,
        "up_stat": "1/1",
        "limit_times": board,
        "limit_type": "U",
        "first_time": "093100",
        "last_time": "145800",
    }


def _step(code, nums, name="合成甲"):
    return {"ts_code": code, "trade_date": DAY, "nums": nums, "name": name}


def _quality_gap_blob(result):
    """只看公共质量信息，不把业务行里的 limit_days 键算作缺口。"""
    quality = (result.get("_meta") or {}).get("quality") or {}
    return json.dumps(
        {
            "status": quality.get("status"),
            "reason_codes": quality.get("reason_codes"),
            "missing": quality.get("missing"),
            "gaps": quality.get("gaps"),
            "source_gaps": (result.get("data") or {}).get("source_gaps"),
        },
        ensure_ascii=False,
        default=str,
    ).lower()


class PublicChainMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        clock = Mock()
        clock.time.return_value = datetime.fromisoformat(STAMP).timestamp()
        clock.monotonic.return_value = 1.0
        self.transport = Mock()
        self.provider = StockTodayProvider(
            token_loader=lambda: SECRET,
            post=self.transport,
            budget_path=Path(self.tmp.name) / "budget.sqlite3",
            member_cache_root=Path(self.tmp.name) / "members",
            clock=clock,
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
        patch.object(api, "_now_shanghai", return_value=datetime.fromisoformat(STAMP)).start()
        patch.dict(api.PROVIDER_REGISTRY, {"stocktoday": self.provider}).start()

    def public(self, up_rows, step_rows):
        fields = [
            "ts_code", "trade_date", "industry", "name", "close",
            "pct_chg", "open_times", "up_stat", "limit_times",
        ]
        step_fields = ["ts_code", "trade_date", "nums", "name"]

        def table(rows, flds):
            resp = Mock(status_code=200)
            resp.json.return_value = {
                "code": 0,
                "data": {"fields": flds, "items": rows},
                "total": len(rows),
            }
            return resp

        by_key = {
            ("limit_list_d", "U"): table(up_rows, fields),
            ("limit_list_d", "D"): table([], fields),
            ("limit_list_d", "Z"): table([], fields),
            ("limit_step", None): table(step_rows, step_fields),
        }

        def dispatch(*args, **kwargs):
            body = kwargs.get("json") or {}
            params = body.get("params") or {}
            return by_key.get(
                (body.get("api_name"), params.get("limit_type")),
                by_key[("limit_step", None)],
            )

        self.transport.side_effect = dispatch
        return query("market_limit_state", date=DAY)


class R1PublicQualityGapTests(PublicChainMixin, unittest.TestCase):
    def test_public_unknown_board_quality_names_fields_and_codes(self):
        result = self.public(
            [_u("600091.SH", "合成甲", 2), _u("600092.SH", "合成乙", None, brk=4)],
            [_step("600091.SH", 2)],
        )
        self.assertIn(result["_meta"]["status"], {"success", "degraded"})
        data = result["data"]
        rows = {row["code"]: row for row in data["pools"]["zt"]}
        self.assertIsNone(rows["600092"]["limit_days"])
        self.assertIsNone(data["max_board"])
        self.assertEqual(2, data["known_max_board"])
        self.assertEqual("合成乙", rows["600092"]["name"])
        self.assertEqual(13.7, rows["600092"]["price"])
        self.assertEqual(4, rows["600092"]["break_times"])
        self.assertEqual(2, data["zt_count"])

        quality = result["_meta"]["quality"]
        self.assertEqual("partial", quality["status"])
        reasons = [str(code).lower() for code in (quality.get("reason_codes") or [])]
        self.assertTrue(
            any("limit_days" in code or "board" in code for code in reasons),
            reasons,
        )
        gaps = data.get("source_gaps") or quality.get("gaps") or []
        self.assertTrue(gaps, "质量缺口必须出现在 source_gaps/quality.gaps，不能只靠行内 limit_days 键")
        named = [gap for gap in gaps if isinstance(gap, dict)]
        self.assertTrue(named, gaps)
        self.assertTrue(
            any("600092" in [str(code) for code in (gap.get("affected_codes") or [])] for gap in named),
            gaps,
        )
        self.assertTrue(
            any(
                "limit_days" in str(gap.get("affected_fields") or gap.get("field") or "").lower()
                for gap in named
            ),
            gaps,
        )
        blob = _quality_gap_blob(result)
        self.assertNotEqual(blob.count("limit_days"), 0)
        self.assertNotIn('"status": "normal"', blob.replace(" ", ""))

    def test_all_known_boards_keep_normal_quality(self):
        result = self.public(
            [_u("600091.SH", "合成甲", 2), _u("600092.SH", "合成乙", 4, close=19.2, brk=1)],
            [_step("600091.SH", 2), _step("600092.SH", 4, "合成乙")],
        )
        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual("normal", result["_meta"]["quality"]["status"])
        self.assertEqual([], result["_meta"]["quality"]["reason_codes"])
        self.assertFalse(result["data"].get("source_gaps"))
        self.assertEqual(4, result["data"]["max_board"])


class R2BreakCountSameSemanticsTests(PublicChainMixin, unittest.TestCase):
    def test_public_break_matches_strict_limit_row_and_keeps_legal_zero(self):
        accepted = []
        for value in (True, -1, 2.5, "3"):
            result = self.public([_u("600091.SH", "合成甲", 2, brk=value)], [_step("600091.SH", 2)])
            row = result["data"]["pools"]["zt"][0]
            direct = StockTodayProvider._limit_row(_u("600091.SH", "合成甲", 2, brk=value))
            self.assertIsNone(direct["break_times"])
            self.assertIsNone(row["break_times"], repr(value))
            self.assertEqual(2, row["limit_days"])
            if row["break_times"] is not None:
                accepted.append(value)
        self.assertEqual([], accepted)

        zero = self.public([_u("600091.SH", "合成甲", 2, brk=0)], [_step("600091.SH", 2)])
        self.assertEqual(0, zero["data"]["pools"]["zt"][0]["break_times"])

    def test_store_direct_fractional_break_is_not_trusted_integer(self):
        store = MarketFactStore(Path(self.tmp.name) / "facts.sqlite3")
        payload = normalize_stocktoday_limits(DAY, {
            "U": _res([_u("600091.SH", "合成甲", 2, brk=0)]),
            "D": _res([]),
            "Z": _res([]),
            "STEP": _res([_step("600091.SH", 2)]),
        })
        payload["data"]["pools"]["zt"][0]["break_times"] = 2.5
        receipt = store.ingest_limits(DAY, payload)
        self.assertEqual(1, receipt["counts"]["up"])
        with store._connect() as conn:
            stored = dict(conn.execute(
                "SELECT break_times, typeof(break_times) AS storage_type FROM limit_events"
            ).fetchone())
        self.assertIsNone(stored["break_times"])
        self.assertEqual("null", stored["storage_type"])

    def test_public_fractional_break_ingest_writes_null_not_real(self):
        result = self.public([_u("600091.SH", "合成甲", 2, brk=2.5)], [_step("600091.SH", 2)])
        self.assertIsNone(result["data"]["pools"]["zt"][0]["break_times"])
        store = MarketFactStore(Path(self.tmp.name) / "public-facts.sqlite3")
        store.ingest_limits(DAY, result)
        with store._connect() as conn:
            stored = dict(conn.execute(
                "SELECT break_times, typeof(break_times) AS storage_type FROM limit_events"
            ).fetchone())
        self.assertIsNone(stored["break_times"])
        self.assertNotEqual("real", stored["storage_type"])

    def test_store_keeps_legal_zero_break(self):
        store = MarketFactStore(Path(self.tmp.name) / "zero.sqlite3")
        payload = normalize_stocktoday_limits(DAY, {
            "U": _res([_u("600091.SH", "合成甲", 2, brk=0)]),
            "D": _res([]),
            "Z": _res([]),
            "STEP": _res([_step("600091.SH", 2)]),
        })
        store.ingest_limits(DAY, payload)
        with store._connect() as conn:
            stored = dict(conn.execute("SELECT break_times FROM limit_events").fetchone())
        self.assertEqual(0, stored["break_times"])


class R3IndependentFieldTests(PublicChainMixin, unittest.TestCase):
    def test_missing_close_keeps_independent_pct_and_break(self):
        result = self.public(
            [_u("600091.SH", "合成甲", 2, close=None, brk=3, pct=9.98)],
            [_step("600091.SH", 2)],
        )
        row = result["data"]["pools"]["zt"][0]
        self.assertEqual(2, row["limit_days"])
        self.assertIsNone(row["price"])
        self.assertEqual(9.98, row["pct"])
        self.assertEqual(3, row["break_times"])

    def test_one_missing_close_does_not_wipe_healthy_row(self):
        result = self.public(
            [
                _u("600091.SH", "合成甲", 2, close=None, brk=0),
                _u("600092.SH", "合成乙", 4, close=19.2, brk=1),
            ],
            [_step("600091.SH", 2), _step("600092.SH", 4, "合成乙")],
        )
        rows = {row["code"]: row for row in result["data"]["pools"]["zt"]}
        self.assertEqual(2, rows["600091"]["limit_days"])
        self.assertIsNone(rows["600091"]["price"])
        self.assertEqual(4, rows["600092"]["limit_days"])
        self.assertEqual(19.2, rows["600092"]["price"])
        self.assertEqual(1, rows["600092"]["break_times"])
        self.assertEqual(9.98, rows["600092"]["pct"])

    def test_named_complete_structure_with_null_close_does_not_raise(self):
        out = normalize_stocktoday_limits(DAY, {
            "U": _res([_u("600091.SH", "合成甲", 2, close=None, brk=3)]),
            "D": _res([]),
            "Z": _res([]),
            "STEP": _res([_step("600091.SH", 2)]),
        })
        row = out["data"]["pools"]["zt"][0]
        self.assertEqual(2, row["limit_days"])
        self.assertIsNone(row["price"])
        self.assertEqual(9.98, row["pct"])
        self.assertEqual(3, row["break_times"])

    def test_type_wrong_close_majority_is_still_structural_failure(self):
        with self.assertRaises(ValueError):
            normalize_stocktoday_limits(DAY, {
                "U": _res([{
                    **_u("600091.SH", "合成甲", 2, brk=0),
                    "close": "测试股份",
                }]),
                "D": _res([]),
                "Z": _res([]),
                "STEP": _res([_step("600091.SH", 2)]),
            })


class PreserveUnknownBoardAndDailyTests(PublicChainMixin, unittest.TestCase):
    def test_unknown_board_still_rejected_and_old_run_unchanged(self):
        store = MarketFactStore(Path(self.tmp.name) / "reject.sqlite3")
        good = normalize_stocktoday_limits(DAY, {
            "U": _res([_u("600091.SH", "合成甲", 2)]),
            "D": _res([]),
            "Z": _res([]),
            "STEP": _res([_step("600091.SH", 2)]),
        })
        store.ingest_limits(DAY, good)

        def snapshot():
            with store._connect() as conn:
                return {
                    name: [tuple(row) for row in conn.execute("SELECT * FROM " + name + " ORDER BY 1")]
                    for name in ("limit_runs", "limit_events", "daily_runs", "daily_rows")
                }

        before = snapshot()
        for value in (None, 0, True, 1.5, "2"):
            bad = normalize_stocktoday_limits(DAY, {
                "U": _res([_u("600091.SH", "合成甲", value)]),
                "D": _res([]),
                "Z": _res([]),
                "STEP": _res([_step("600091.SH", 2)]),
            })
            with self.assertRaisesRegex(ValueError, "malformed"):
                store.ingest_limits(DAY, bad)
            self.assertEqual(before, snapshot())

    def test_dz_zero_board_semantics_unchanged(self):
        out = normalize_stocktoday_limits(DAY, {
            "U": _res([_u("600091.SH", "合成甲", 2)]),
            "D": _res([{
                "ts_code": "600093.SH", "trade_date": DAY, "limit_type": "D",
                "name": "跌停甲", "industry": "合成", "close": 8.0, "pct_chg": -10.0,
                "open_times": 0, "limit_times": None,
            }]),
            "Z": _res([{
                "ts_code": "600094.SH", "trade_date": DAY, "limit_type": "Z",
                "name": "炸板甲", "industry": "合成", "close": 9.0, "pct_chg": 2.0,
                "open_times": 3, "limit_times": None,
            }]),
            "STEP": _res([_step("600091.SH", 2)]),
        })
        self.assertEqual(0, out["data"]["pools"]["dt"][0]["limit_days"])
        self.assertEqual(0, out["data"]["pools"]["zb"][0]["limit_days"])
        self.assertEqual(2, out["data"]["pools"]["zt"][0]["limit_days"])


if __name__ == "__main__":
    unittest.main()
