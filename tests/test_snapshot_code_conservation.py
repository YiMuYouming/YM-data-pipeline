"""B1（2026-10-26 批次4）：stock_snapshot 申请/返回/缺失计数守恒。

原始证据：
- 09:12 兆威 7 码：stocktoday 原始 7 行（1 个重复）、唯一 6 码、003021 真实缺失，
  腾讯补查也失败 → 申请 7 = 返回 6 + 缺失 1（计数守恒，属真实漏码）。
- 午盘 24 码：stocktoday 原始 27 行、唯一 23 码，腾讯补齐 003021 后 24 码都有价，
  但 quality 仍为 `missing=[]` 却 `missing_count=1`、`coverage=0.958`，并保留
  `missing_items`/`coverage_shortfall` 告警：补齐后计数没有重算（缺陷）。

本文件锁定：请求码去重后，申请数 = 返回数 + 缺失数；`missing_count == len(missing)`；
`coverage == 返回/申请`；补齐后不再保留覆盖类告警；真实无数据仍如实保留缺失。
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import ym_stock_data.api as api
from ym_stock_data.provider_policy import CompiledPolicy
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome

NOW = datetime(2026, 10, 26, 10, 5, tzinfo=timezone(timedelta(hours=8)))
NOW_ISO = NOW.isoformat(timespec="seconds")


def _row(code, price=1400.0):
    return {
        "name": code,
        "price": price,
        "last_close": price - 10,
        "open": price - 5,
        "high": price + 10,
        "low": price - 15,
        "change_pct": 0.7,
        "amount": 1_000_000.0,
        "amount_wan": 100.0,
        "volume": 1000.0,
        "quote_time": NOW_ISO,
        "source": "stocktoday",
    }


def _observation(**kwargs):
    base = {
        "api_name": "rt_k",
        "returned_count": 0,
        "unique_code_count": 0,
        "duplicate_code_count": 0,
        "missing_codes": [],
        "status": "fresh",
        "timestamp_coverage": 1.0,
        "filter_violations": [],
        "truncated": False,
        "latest_at": NOW_ISO,
        "oldest_at": NOW_ISO,
        "age_sec": 0,
        "max_age_sec": 600,
    }
    base.update(kwargs)
    return base


class _FakeProvider:
    def __init__(self, name, outcomes):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = []

    def call(self, intent, params):
        self.calls.append((intent, dict(params)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _outcome(provider, status, data=None, error_code=None):
    return ProviderOutcome(provider=provider, status=status, data=data,
                           error_code=error_code, latency_ms=1,
                           auth={"required": True, "status": "ok"})


class SnapshotCodeConservationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = ProviderState(Path(temp.name) / "state.sqlite3")
        self.providers = {}
        clock = patch.object(api, "_now_shanghai", lambda: NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def _run(self, codes, stocktoday_outcome, tencent_outcome=None):
        self.providers = {"stocktoday": _FakeProvider("stocktoday", [stocktoday_outcome])}
        if tencent_outcome is not None:
            self.providers["tencent"] = _FakeProvider("tencent", [tencent_outcome])
        return api._query_with(
            "stock_snapshot",
            {"codes": list(codes)},
            provider_loader=lambda name: self.providers[name],
            state_loader=lambda: self.state,
            policy_loader=lambda: CompiledPolicy(None, "inactive", None),
        )

    def test_duplicate_request_codes_are_counted_once(self):
        data = {
            "600519": _row("600519"),
            "000001": _row("000001"),
            "_stocktoday": _observation(returned_count=4, unique_code_count=2,
                                        duplicate_code_count=2),
        }
        result = self._run(
            ["600519", "000001", "600519"],
            _outcome("stocktoday", "success", data),
        )
        quality = result["_meta"]["quality"]
        self.assertEqual(2, quality["requested_count"])
        self.assertEqual(2, quality["returned_count"])
        self.assertEqual([], quality["missing"])
        self.assertEqual(0, quality["missing_count"])
        self.assertEqual(1.0, quality["coverage"])
        self.assertNotIn("missing_items", quality["reason_codes"])
        self.assertNotIn("coverage_shortfall", quality["reason_codes"])
        # 原始行重复仍是事实，按 DP-01 契约保留为 provenance 码
        self.assertIn("DUPLICATE_CODES", quality["reason_codes"])
        self.assertEqual(2, result["_meta"]["observation"]["duplicate_code_count"])

    def test_complete_fallback_fill_recomputes_counts_and_drops_coverage_alarms(self):
        data = {
            "600519": _row("600519"),
            "_stocktoday": _observation(returned_count=1, unique_code_count=1,
                                        missing_codes=["000001"]),
        }
        filled = _row("000001")
        filled["source"] = "tencent"
        result = self._run(
            ["600519", "000001"],
            _outcome("stocktoday", "success", data),
            _outcome("tencent", "success", {"000001": filled}),
        )
        quality = result["_meta"]["quality"]
        self.assertEqual(["000001"], result["_meta"]["filled_by_fallback"])
        self.assertIn("000001", result["data"])
        self.assertEqual(2, quality["requested_count"])
        self.assertEqual(2, quality["returned_count"])
        self.assertEqual([], quality["missing"])
        self.assertEqual(0, quality["missing_count"])
        self.assertEqual(1.0, quality["coverage"])
        self.assertNotIn("missing_items", quality["reason_codes"])
        self.assertNotIn("coverage_shortfall", quality["reason_codes"])
        self.assertIn("FILLED_BY_FALLBACK", quality["reason_codes"])
        self.assertEqual([], result["_meta"]["observation"]["missing_codes"])

    def test_unfilled_missing_code_keeps_conserved_gap(self):
        data = {
            "600519": _row("600519"),
            "_stocktoday": _observation(returned_count=1, unique_code_count=1,
                                        missing_codes=["000001"]),
        }
        result = self._run(
            ["600519", "000001"],
            _outcome("stocktoday", "success", data),
            _outcome("tencent", "provider_error", error_code="UPSTREAM_ERROR"),
        )
        quality = result["_meta"]["quality"]
        self.assertNotIn("000001", result["data"])
        self.assertNotIn("000001", result["data"])  # 不补空行凑数
        self.assertEqual(2, quality["requested_count"])
        self.assertEqual(1, quality["returned_count"])
        self.assertEqual(["000001"], quality["missing"])
        self.assertEqual(1, quality["missing_count"])
        self.assertEqual(0.5, quality["coverage"])
        self.assertIn("missing_items", quality["reason_codes"])
        self.assertIn("coverage_shortfall", quality["reason_codes"])
        self.assertIn("MISSING_CODES", quality["reason_codes"])


if __name__ == "__main__":
    unittest.main()
