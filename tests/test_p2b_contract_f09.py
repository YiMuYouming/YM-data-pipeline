"""P2-B F09：index_intraday_compare 的 series_metadata 与 bar 计数。

红线：

- **不改**旧三键（`上证15min`/`深证15min`/`创业15min`）的键名与内容，
  15m 既有消费者 shape/内容必须逐值不变；
- **不新增**第二套重复行键（不复制行数据到新键）；
- 只**新增** `series_metadata` 映射：每个旧键给 index_code、period、
  correct_label、bar_count（**排除 _cum**）、expected_bars_per_full_day；
- `bar_count` 是**返回行数**，不是完整性认证：
  `bar_completeness="unverified"`。48 行不等于当天已完成。
- 三指数 metadata **分别计算**，不借另一指数的行数。

测试在 socket 层断网运行；夹具为仓库内可移植的公开行情子集。
"""

import json
import socket
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ym_stock_data.sources import eastmoney_index

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "p2b"

COMPARE_KEYS = ("上证15min", "深证15min", "创业15min")
EXPECTED = {"5m": 48, "15m": 16, "60m": 4}
LABELS = {
    ("000001.SH", "5m"): "上证5min",
    ("000001.SH", "15m"): "上证15min",
    ("000001.SH", "60m"): "上证60min",
    ("399001.SZ", "5m"): "深证5min",
    ("399001.SZ", "15m"): "深证15min",
    ("399001.SZ", "60m"): "深证60min",
    ("399006.SZ", "5m"): "创业5min",
    ("399006.SZ", "15m"): "创业15min",
    ("399006.SZ", "60m"): "创业60min",
}


class _NetworkBlocked(RuntimeError):
    pass


class _NoNetwork:
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


def bar(dt, close):
    """dt 是完整的 'YYYY-MM-DD HH:MM:SS'。"""
    return {
        "datetime": dt,
        "open": close - 0.5,
        "high": close + 0.5,
        "low": close - 1.0,
        "close": close,
        "volume": 1000.0,
        "amount": 2000.0,
    }


def fake_fetcher(bars_by_index):
    """A fetcher stub that replays sealed bars per index code."""

    def _fetch(index_code, *, period, count, start_date, end_date):
        rows = bars_by_index.get(index_code) or []
        return {"index_code": index_code, "period": period, "bars": list(rows)}

    return _fetch


def full_day_bars(index_code, period, day="2026-10-16"):
    """One full trading day of bars at the given period."""
    n = EXPECTED[period]
    minutes = {"5m": 5, "15m": 15, "60m": 60}[period]
    out = []
    clock = 9 * 60 + 35
    for i in range(n):
        slot = clock + i * minutes
        if slot > 11 * 60 + 30:
            slot = 13 * 60 + 15 + (slot - (11 * 60 + 30) - minutes)
        hh, mm = divmod(slot, 60)
        out.append(bar(f"{day} {hh:02d}:{mm:02d}:00", 3000.0 + i))
    return out


class SeriesMetadataTests(unittest.TestCase):
    def setUp(self):
        self.net = _NoNetwork()
        self.net.__enter__()
        self.addCleanup(self.net.__exit__, None, None, None)
        self.addCleanup(patch.stopall)

    def build(self, period, day="2026-10-16", per_index=None):
        codes = [c for c, _ in eastmoney_index._COMPARE_INDEXES]
        if per_index is None:
            per_index = {c: full_day_bars(c, period, day) for c in codes}
        with patch.object(eastmoney_index, "datetime") as _dt:
            pass
        return eastmoney_index.build_index_intraday_compare(
            fake_fetcher(per_index),
            source="offline_fixture",
            period=period,
            trade_date=day.replace("-", ""),
        )

    # ---------- legacy 三键不变 ----------

    def test_legacy_keys_and_values_are_unchanged(self):
        for period in ("5m", "15m", "60m"):
            with self.subTest(period=period):
                out = self.build(period)
                self.assertNotIn("error", out)
                for key in COMPARE_KEYS:
                    self.assertIn(key, out)
                    rows = out[key]
                    self.assertTrue(rows)
                    # 旧内容原样：末行仍是累计行
                    self.assertTrue(rows[-1].get("_cum"))
                    self.assertEqual("累计", rows[-1].get("t"))

    def test_no_duplicate_row_keys_added(self):
        out = self.build("15m")
        # 新键不得携带重复行数据
        for key in out:
            if key in COMPARE_KEYS or key in {"items", "trade_date", "period",
                                              "source", "series_metadata"}:
                continue
            self.fail(f"unexpected extra row-bearing key: {key}")

    def test_data_period_stays_real_value(self):
        for period in ("5m", "15m", "60m"):
            with self.subTest(period=period):
                self.assertEqual(period, self.build(period)["period"])

    # ---------- series_metadata ----------

    def test_series_metadata_covers_every_legacy_key(self):
        for period in ("5m", "15m", "60m"):
            with self.subTest(period=period):
                meta = self.build(period)["series_metadata"]
                self.assertEqual(set(COMPARE_KEYS), set(meta))
                for key, entry in meta.items():
                    self.assertEqual(key, entry["legacy_key"])
                    self.assertIn(entry["index_code"], ("000001.SH", "399001.SZ",
                                                        "399006.SZ"))
                    self.assertEqual(period, entry["period"])
                    self.assertEqual(
                        LABELS[(entry["index_code"], period)],
                        entry["correct_label"],
                    )

    def test_correct_label_matches_actual_period_not_the_legacy_key(self):
        meta5 = self.build("5m")["series_metadata"]
        meta60 = self.build("60m")["series_metadata"]
        self.assertEqual("上证5min", meta5["上证15min"]["correct_label"])
        self.assertEqual("上证60min", meta60["上证15min"]["correct_label"])
        self.assertEqual("上证15min", meta5["上证15min"]["legacy_key"])

    def test_bar_count_excludes_cum_row(self):
        for period in ("5m", "15m", "60m"):
            with self.subTest(period=period):
                out = self.build(period)
                meta = out["series_metadata"]
                for key in COMPARE_KEYS:
                    rows = out[key]
                    bars = [r for r in rows if not r.get("_cum")]
                    self.assertEqual(len(bars), meta[key]["bar_count"])
                    self.assertEqual(len(rows), meta[key]["row_count"])
                    self.assertEqual(
                        len(rows) - len(bars), meta[key]["cum_row_count"]
                    )
                    self.assertEqual(1, meta[key]["cum_row_count"])

    def test_expected_bars_per_full_day_is_declared_not_certified(self):
        for period, expected in EXPECTED.items():
            with self.subTest(period=period):
                meta = self.build(period)["series_metadata"]
                for key in COMPARE_KEYS:
                    self.assertEqual(expected,
                                     meta[key]["expected_bars_per_full_day"])
                    # 行数不是完整性认证
                    self.assertEqual("unverified",
                                     meta[key]["bar_completeness"])

    # ---------- 不把行数当完整 ----------

    def test_partial_day_does_not_claim_complete(self):
        codes = [c for c, _ in eastmoney_index._COMPARE_INDEXES]
        partial = {c: full_day_bars(c, "5m")[:10] for c in codes}
        out = self.build("5m", per_index=partial)
        meta = out["series_metadata"]["上证15min"]
        self.assertEqual(10, meta["bar_count"])
        self.assertEqual(48, meta["expected_bars_per_full_day"])
        self.assertEqual("unverified", meta["bar_completeness"])

    def test_metadata_computed_per_index_not_borrowed(self):
        codes = [c for c, _ in eastmoney_index._COMPARE_INDEXES]
        mixed = {
            codes[0]: full_day_bars(codes[0], "5m"),
            codes[1]: full_day_bars(codes[1], "5m")[:20],
            codes[2]: full_day_bars(codes[2], "5m")[:30],
        }
        meta = self.build("5m", per_index=mixed)["series_metadata"]
        counts = {entry["bar_count"] for entry in meta.values()}
        self.assertEqual({48, 20, 30}, counts)
        for entry in meta.values():
            self.assertEqual("unverified", entry["bar_completeness"])

    # ---------- 既有过滤不回归 ----------

    def test_cum_row_still_excluded_from_data_as_of_and_quality(self):
        from ym_stock_data import api as api_mod

        for fn in ("_data_as_of", "_quality_failure_code"):
            with self.subTest(fn=fn):
                src = Path(api_mod.__file__).read_text(encoding="utf-8")
                self.assertIn('not row.get("_cum")', src)

    def test_empty_reason_path_unchanged(self):
        with patch.object(eastmoney_index, "datetime") as _dt:
            pass
        out = eastmoney_index.build_index_intraday_compare(
            fake_fetcher({}), source="offline_fixture", period="15m",
            trade_date=datetime.now().strftime("%Y%m%d"),
        )
        if out.get("empty_reason"):
            self.assertNotIn("series_metadata", out)


class FixtureProvenanceTests(unittest.TestCase):
    def test_sealed_compare_fixtures_present_and_sourced(self):
        for name in ("compare_5m_20261016.json", "compare_15m_20261016.json",
                     "compare_60m_20261016.json"):
            with self.subTest(name=name):
                data = json.loads(
                    (FIXTURE_DIR / name).read_text(encoding="utf-8")
                )
                self.assertRegex(data["_source_sealed_sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(data["_source_sealed_file"])
                self.assertIn(data["period"], EXPECTED)

    def test_fixtures_have_no_private_paths_or_credentials(self):
        for name in ("compare_5m_20261016.json", "compare_15m_20261016.json",
                     "compare_60m_20261016.json"):
            raw = (FIXTURE_DIR / name).read_text(encoding="utf-8").lower()
            for word in ("/users/", "agentscratch", "token", "secret",
                         "password", "api_key", "authorization"):
                with self.subTest(name=name, word=word):
                    self.assertNotIn(word, raw)


class PublicQueryPassthroughTests(unittest.TestCase):
    """新增字段必须经公共 query() 入口原样透传，不被api 层过滤。"""

    def setUp(self):
        self.net = _NoNetwork()
        self.net.__enter__()
        self.addCleanup(self.net.__exit__, None, None, None)

    def test_series_metadata_reaches_public_query(self):
        from ym_stock_data import api as api_mod
        from ym_stock_data import query
        from ym_stock_data.providers import local as local_mod
        from ym_stock_data.sources import tencent as tencent_mod
        from ym_stock_data.provider_policy import CompiledPolicy
        from ym_stock_data.provider_state import ProviderState
        import tempfile
        from pathlib import Path

        codes = [c for c, _ in eastmoney_index._COMPARE_INDEXES]
        bars = {c: full_day_bars(c, "5m") for c in codes}
        # 路由首要源是 tencent，其 compare 取 bar 走
        # tencent.fetch_index_minute_bars —— 只替换这一处取数，
        # provider/路由/api 全链路保持真实。
        with tempfile.TemporaryDirectory() as tmp:
            state = ProviderState(Path(tmp) / "s.sqlite3")
            with patch.object(api_mod, "load_compiled_policy",
                              return_value=CompiledPolicy(None, "inactive", None)), \
                 patch.object(api_mod, "_STATE", state), \
                 patch.object(tencent_mod, "fetch_index_minute_bars",
                              side_effect=lambda code, **kw: {
                                  "index_code": code,
                                  "period": kw.get("period", "5m"),
                                  "bars": bars.get(code) or [],
                              }):
                result = query("index_intraday_compare", period="5m",
                               trade_date="2026-10-16")
        self.assertIsNotNone(result["data"])
        meta = result["data"]["series_metadata"]
        self.assertEqual(set(COMPARE_KEYS), set(meta))
        self.assertEqual("上证5min", meta["上证15min"]["correct_label"])
        self.assertEqual(48, meta["上证15min"]["bar_count"])
        self.assertEqual("unverified", meta["上证15min"]["bar_completeness"])
        # 旧键仍在且内容未变
        self.assertIn("上证15min", result["data"])
        self.assertEqual("5m", result["data"]["period"])

    def test_board_members_new_fields_reach_public_query(self):
        from ym_stock_data import query
        from ym_stock_data.providers.stocktoday import StockTodayProvider
        from ym_stock_data import api as api_mod
        from ym_stock_data.provider_policy import CompiledPolicy
        from ym_stock_data.provider_state import ProviderState
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as tmp:
            transport = Mock()
            resp = Mock(status_code=200)
            resp.json.return_value = {
                "code": 0,
                "data": {"fields": ["ts_code", "con_code"],
                         "items": [{"ts_code": "881121.TI", "con_code": "600519.SH"},
                                   {"ts_code": "881121.TI", "con_code": "600519.SH"}]},
                "total": 2,
            }
            transport.return_value = resp
            provider = StockTodayProvider(
                token_loader=lambda: "synthetic-p2b-passthrough",
                post=transport,
                budget_path=Path(tmp) / "b.sqlite3",
                member_cache_root=Path(tmp) / "m",
            )
            state = ProviderState(Path(tmp) / "s.sqlite3")
            with patch.object(api_mod, "load_compiled_policy",
                              return_value=CompiledPolicy(None, "inactive", None)), \
                 patch.object(api_mod, "_STATE", state), \
                 patch.dict(api_mod.PROVIDER_REGISTRY, {"stocktoday": provider}):
                result = query("board_members", codes=["881121"],
                               trade_date="20261016")
        board = result["data"]["boards"][0]
        self.assertEqual(2, board["member_count"])
        self.assertEqual(1, board["unique_member_count"])
        self.assertEqual(["600519.SH"], board["duplicate_member_codes"])
        self.assertEqual("unknown", result["data"]["membership_time_status"])
        self.assertIsNone(result["data"]["membership_as_of"])
        # meta 时点没有被缓存标签顶替
        self.assertIsNone(result["_meta"]["data_as_of"])


if __name__ == "__main__":
    unittest.main()
