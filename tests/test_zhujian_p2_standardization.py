"""铸剑 P2-A 数据标准化：index_kline 的 CSI 后缀与涨停行 last_limit_time 透传。

只覆盖本包两项施工：

1. `index_kline` 显式 `000985.CSI` 经canonical StockToday 日线通路可用。
   SH/SZ 与裸代码既有行为不变；分钟/周月与后备源能力不因此扩张。
2. `limit_list_d` 的 `last_time` 逐条透传为 `last_limit_time`，
   `first_time` 独立为 `first_limit_time`，两者语义不折叠。

真实上游响应的**最小脱敏夹具**在仓库内 `tests/fixtures/zhujian_p2/`，
由 P1 封存响应逐行裁剪而来（夹具内 `_source_sealed_sha256` 记录原封存文件
hash）。这些是**已封存响应的离线重放**，不是新的在线证据；本文件不需要任何
Scratch 目录即可在干净 clone 中复现。
"""

import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ym_stock_data import api, query
from ym_stock_data.provider_policy import CompiledPolicy
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome

SECRET = "synthetic-zhujian-p2-test-token"

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "zhujian_p2"


def _sealed(name):
    path = FIXTURE_DIR / name
    if not path.is_file():
        raise AssertionError(f"fixture missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


class _NetworkBlocked(RuntimeError):
    """任何真实网络访问都让测试立即失败，而不是悄悄放行。"""


class _NoNetwork:
    """在 socket 层断网：本包的离线性质由断言保证，不靠耗时推断。"""

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


KLINE_FIELDS = ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"]


def kline_row(ts_code, close=1.5, day="20261016"):
    """A daily index bar that passes the existing kline quality gate."""
    return {
        "ts_code": ts_code, "trade_date": day, "open": 1.0, "high": 2.0,
        "low": 0.5, "close": close, "vol": 1000.0, "amount": 2000.0,
    }


class _OfflineStubProvider:
    """Fails a non-StockToday provider offline instead of dialling out."""

    def __init__(self, name):
        self.name = name
        self.calls = []

    def probe(self):
        return {"provider": self.name, "status": "ready"}

    def call(self, intent, params):
        self.calls.append((intent, params))
        return ProviderOutcome(
            provider=self.name,
            status="provider_error",
            error_code="OFFLINE_STUB",
            latency_ms=0,
        )


class ZhujianP2Base(unittest.TestCase):
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
        )
        self.state = ProviderState(Path(self.tmp.name) / "state.sqlite3")
        self.addCleanup(patch.stopall)
        # socket 层断网：本包全部证据是封存响应离线重放，任何真实网络访问
        # 都必须让测试失败，而不是被 mock 漏过或靠"跑得快"来间接证明。
        net = _NoNetwork()
        net.__enter__()
        self.addCleanup(net.__exit__, None, None, None)
        patch.object(
            api, "load_compiled_policy", return_value=CompiledPolicy(None, "inactive", None)
        ).start()
        patch.object(api, "_STATE", self.state).start()
        patch.dict(api.PROVIDER_REGISTRY, {"stocktoday": self.provider}).start()
        # 后备源一律用离线失败桩：本包证据是封存响应重放，任何真实
        # 网络回退都会把离线回归变成新的在线探针。
        patch.object(
            api,
            "_provider_for",
            side_effect=self._offline_providers,
        ).start()

    def _offline_providers(self, name):
        if name == "stocktoday":
            return self.provider
        return _OfflineStubProvider(name)

    def serve_table(self, fields, rows, total=None):
        """Replay a sealed wire table through the real adapter HTTP seam."""
        self.response.json.return_value = {
            "code": 0,
            "data": {"fields": list(fields), "items": list(rows)},
            "total": len(rows) if total is None else total,
        }


class IndexKlineCsIStandardizationTests(ZhujianP2Base):
    """R-A：index_kline 接受显式 .CSI 后缀，经 index_daily 标准化。"""

    def test_csi_suffix_is_accepted_and_routed_to_index_daily(self):
        sealed = _sealed("index_daily_000985_csi.json")
        items = sealed["items"]
        fields = sealed["fields"]
        self.serve_table(fields, items)

        result = query(
            "index_kline", index_code="000985.CSI", period="daily", count=3
        )

        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual("stocktoday", result["_meta"]["provider_used"])
        wire = self.transport.call_args.kwargs["json"]
        self.assertEqual("index_daily", wire["api_name"])
        self.assertEqual("000985.CSI", wire["params"]["ts_code"])
        bars = result["data"]["bars"]
        self.assertEqual(3, len(bars))
        self.assertEqual("000985.CSI", result["data"]["index_code"])
        # 真实行的标准化：日期补横线，量额单位换算由既有通路负责。
        self.assertEqual("2026-10-16", bars[-1]["datetime"])
        self.assertEqual(5289.37, bars[-1]["close"])
        # _meta 契约不因新增后缀而降级。
        self.assertEqual("stocktoday", result["_meta"]["attempts"][0]["provider"])
        self.assertIn("quality", result["_meta"])
        self.assertIn("fetched_at", result["_meta"])

    def test_csi_code_is_lowercased_input_normalized_to_upper(self):
        self.serve_table(KLINE_FIELDS, [kline_row("000985.CSI")])
        result = query("index_kline", index_code="000985.csi", period="daily")
        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual(
            "000985.CSI", self.transport.call_args.kwargs["json"]["params"]["ts_code"]
        )

    def test_sh_sz_and_bare_code_behaviour_is_unchanged(self):
        for requested, expected in (
            ("000001.SH", "000001.SH"),
            ("399001.SZ", "399001.SZ"),
            ("000300.SH", "000300.SH"),
            ("000001", "000001.SH"),
            ("399001", "399001.SZ"),
            ("000300", "000300.SZ"),
        ):
            with self.subTest(requested=requested):
                self.serve_table(KLINE_FIELDS, [kline_row(expected)])
                result = query("index_kline", index_code=requested, period="daily")
                self.assertEqual("success", result["_meta"]["status"])
                self.assertEqual(
                    expected, self.transport.call_args.kwargs["json"]["params"]["ts_code"]
                )

    def test_bare_code_is_still_inferred_not_routed_to_csi(self):
        # 裸 000985 继续按既有规则归一化为 .SZ；不静默改基准。
        self.serve_table(KLINE_FIELDS, [kline_row("000985.SZ")])
        query("index_kline", index_code="000985", period="daily")
        self.assertEqual(
            "000985.SZ", self.transport.call_args.kwargs["json"]["params"]["ts_code"]
        )

    def test_other_suffixes_are_still_rejected(self):
        for bad in ("000985.BJ", "000985.CSI.X", "985.CSI", "00098.CSI", "0009855.CSI"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    query("index_kline", index_code=bad, period="daily")
        self.assertFalse(
            self.transport.call_args, "rejected input must not call upstream"
        )

    def test_codes_list_accepts_mixed_sh_sz_and_csi(self):
        rows = [kline_row("000985.CSI"), kline_row("000300.SH")]
        self.serve_table(KLINE_FIELDS, rows)
        result = query("index_kline", codes=["000985.CSI", "000300.SH"], period="daily")
        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual("stocktoday", result["_meta"]["provider_used"])
        by_code = result["data"]["by_code"]
        self.assertIn("000985.CSI", by_code)
        self.assertIn("000300.SH", by_code)
        # 每个请求指数恰好一份结果，代码不被改写。
        self.assertEqual(
            {"000985.CSI", "000300.SH"},
            {item["index_code"] for item in result["data"]["items"]},
        )

    def test_provider_index_code_accepts_csi_and_rejects_others(self):
        self.assertEqual("000985.CSI", self.provider._index_code("000985.CSI"))
        self.assertEqual("000985.CSI", self.provider._index_code("000985.csi"))
        self.assertEqual("000001.SH", self.provider._index_code("000001.SH"))
        self.assertEqual("399006.SZ", self.provider._index_code("399006.SZ"))
        for bad in ("000985", "000985.BJ", "985.CSI", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.provider._index_code(bad)

    def test_csi_daily_upstream_empty_stays_legal_empty_not_substituted(self):
        # 上游空就是合法空；不得改用上证/沪深300冒充基准。
        self.serve_table(KLINE_FIELDS, [])
        result = query("index_kline", index_code="000985.CSI", period="daily")
        self.assertIn(result["_meta"]["status"], {"empty", "error"})
        self.assertIn(
            "EMPTY_UPSTREAM", [a.get("error_code") for a in result["_meta"]["attempts"]]
        )
        # 后备桩全部失败，没有任何来源声称提供了 000985 数据。
        self.assertEqual(0, result["_meta"]["quality"]["returned_count"])
        self.assertNotIn("000985.SH", json.dumps(result, ensure_ascii=False, default=str))

    def test_csi_minute_period_does_not_reach_stocktoday(self):
        # 分钟线不在本包扩程：idx_mins 不在套餐内，CSI 分钟只到后备源，
        # 且后备源没有 CSI 映射，必须诚实失败而不是凭空可用。
        self.serve_table(KLINE_FIELDS, [])
        result = query("index_kline", index_code="000985.CSI", period="5m")
        self.assertEqual("error", result["_meta"]["status"])
        providers = [a["provider"] for a in result["_meta"]["attempts"]]
        self.assertNotIn("stocktoday", providers)
        self.assertIsNone(result["_meta"]["provider_used"])


class LimitLastTimePassthroughTests(ZhujianP2Base):
    """R-B：last_time 逐条透传，first_time 独立，不折叠。"""

    def test_market_limit_board_passes_through_last_limit_time(self):
        sealed = _sealed("limit_list_d_up_20261016.json")
        items = sealed["items"]
        fields = sealed["fields"]
        self.serve_table(fields, items)

        result = query("market_limit_board", kind="up", date="20261016")

        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual("stocktoday", result["_meta"]["provider_used"])
        rows = {row["code"]: row for row in result["data"]["items"]}
        self.assertEqual(61, len(rows))
        for code, row in rows.items():
            self.assertIn("last_limit_time", row)
            self.assertIn("first_limit_time", row)
            self.assertIsNotNone(row["last_limit_time"])
            self.assertIsNotNone(row["first_limit_time"])
        # 真实样本：61 只里 30 只 first≠last，逐条保持不等。
        differing = [c for c, r in rows.items() if r["first_limit_time"] != r["last_limit_time"]]
        self.assertEqual(30, len(differing))
        self.assertEqual("111630", rows["000099"]["last_limit_time"])
        self.assertEqual("111630", rows["000099"]["first_limit_time"])

    def test_first_and_last_are_not_folded_into_one_field(self):
        sealed = _sealed("limit_list_d_up_20261016.json")
        self.serve_table(sealed["fields"], sealed["items"])
        result = query("market_limit_board", kind="up", date="20261016")
        rows = {row["code"]: row for row in result["data"]["items"]}
        sample = next(r for r in rows.values() if r["first_limit_time"] != r["last_limit_time"])
        self.assertNotEqual(sample["first_limit_time"], sample["last_limit_time"])
        # 不允许用 first 冒充 last：任一行 last 都必须等于真实 last_time。
        truth = {
            str(i["ts_code"]).split(".")[0]: i for i in sealed["items"]
        }
        for code, row in rows.items():
            self.assertEqual(truth[code]["last_time"], row["last_limit_time"])
            self.assertEqual(truth[code]["first_time"], row["first_limit_time"])

    def test_original_source_time_string_is_preserved_verbatim(self):
        # 原来源时点是 HHMMSS 字符串；不重写格式、不补零、不造时刻。
        row = {
            "ts_code": "000099.SZ", "trade_date": "20261016", "name": "中信海直",
            "close": 14.75, "pct_chg": 9.99, "limit_times": 1, "open_times": 0,
            "industry": "航空机场", "first_time": "92500", "last_time": "144500",
        }
        self.serve_table(list(row), [row])
        result = query("market_limit_board", kind="up", date="20261016")
        item = result["data"]["items"][0]
        self.assertEqual("92500", item["first_limit_time"])
        self.assertEqual("144500", item["last_limit_time"])

    def test_null_and_empty_last_time_are_distinct_and_not_inferred(self):
        # 两态，不是三态：null → None，空串 → ""。整列缺失与 null 同归None
        # （见下一个用例）；本包不声称能区分这两者。
        rows = [
            {"ts_code": "000001.SZ", "trade_date": "20261016", "name": "缺封板时点",
             "close": 10.0, "pct_chg": 10.0, "first_time": "100000", "last_time": None},
            {"ts_code": "000002.SZ", "trade_date": "20261016", "name": "空字符串",
             "close": 10.0, "pct_chg": 10.0, "first_time": "100000", "last_time": ""},
        ]
        self.serve_table(
            ["ts_code", "trade_date", "name", "close", "pct_chg", "first_time", "last_time"],
            rows,
        )
        result = query("market_limit_board", kind="up", date="20261016")
        self.assertEqual("success", result["_meta"]["status"])
        items = {row["code"]: row for row in result["data"]["items"]}
        self.assertIsNone(items["000001"]["last_limit_time"])
        self.assertEqual("", items["000002"]["last_limit_time"])
        self.assertNotEqual(
            items["000001"]["last_limit_time"], items["000002"]["last_limit_time"]
        )
        # 任何一行都不得回落到 first_limit_time。
        for row in items.values():
            if row["last_limit_time"] in (None, ""):
                self.assertNotEqual(row["first_limit_time"], row["last_limit_time"])

    def test_absent_column_normalizes_to_null_same_as_explicit_null(self):
        # 明确记录现行口径：**缺列与 null 都归一为 None**，本包不区分二者；
        # 两者都不是 0、不是空串、更不是首封。
        absent = self.provider._limit_row({"ts_code": "600519.SH", "name": "样本"})
        explicit = self.provider._limit_row(
            {"ts_code": "600519.SH", "name": "样本", "first_time": None, "last_time": None}
        )
        self.assertIsNone(absent["first_limit_time"])
        self.assertIsNone(absent["last_limit_time"])
        self.assertIsNone(explicit["last_limit_time"])
        self.assertEqual(
            absent["last_limit_time"], explicit["last_limit_time"],
            "缺列与显式 null 在现行契约下同归None",
        )
        zeroed = self.provider._limit_row(
            {"ts_code": "600519.SH", "name": "样本", "first_time": 0, "last_time": 0}
        )
        self.assertEqual("0", zeroed["last_limit_time"])
        self.assertNotEqual(zeroed["last_limit_time"], None)
        self.assertNotEqual(zeroed["first_limit_time"], "")

    def test_limit_row_keeps_untouched_fields_and_meta(self):
        sealed = _sealed("limit_list_d_up_20261016.json")
        self.serve_table(sealed["fields"], sealed["items"])
        result = query("market_limit_board", kind="up", date="20261016")
        item = next(r for r in result["data"]["items"] if r["code"] == "000099")
        self.assertEqual("中信海直", item["name"])
        self.assertEqual(14.75, item["price"])
        self.assertEqual(9.99, item["pct"])
        self.assertEqual(1, item["limit_days"])
        self.assertEqual("航空机场", item["industry"])
        self.assertEqual("20261016", result["_meta"]["data_as_of"][:10].replace("-", ""))
        self.assertEqual("stocktoday", result["_meta"]["attempts"][0]["provider"])
        self.assertIn("quality", result["_meta"])

    def test_down_and_broken_boards_also_carry_the_two_times(self):
        for kind, limit_type in (("down", "D"), ("broken", "Z")):
            with self.subTest(kind=kind):
                row = {
                    "ts_code": "600519.SH", "trade_date": "20261016", "name": "样本",
                    "close": 10.0, "pct_chg": -10.0, "limit_times": 1, "open_times": 2,
                    "industry": "白酒", "first_time": "100000", "last_time": "140000",
                }
                self.serve_table(list(row), [row])
                result = query("market_limit_board", kind=kind, date="20261016")
                item = result["data"]["items"][0]
                self.assertEqual("100000", item["first_limit_time"])
                self.assertEqual("140000", item["last_limit_time"])
                self.assertEqual(limit_type, self.transport.call_args.kwargs["json"]["params"]["limit_type"])

    def test_upstream_empty_pool_stays_legal_empty(self):
        self.serve_table(
            ["ts_code", "trade_date", "first_time", "last_time"], []
        )
        result = query("market_limit_board", kind="up", date="20261016")
        self.assertIn(result["_meta"]["status"], {"empty", "error"})
        self.assertEqual(0, result["_meta"]["quality"]["returned_count"])
        self.assertIn(
            "EMPTY_UPSTREAM", [a.get("error_code") for a in result["_meta"]["attempts"]]
        )


class NoBenchmarkSubstitutionTests(ZhujianP2Base):
    """阻断条款：基准缺失不换指数、不伪造后备映射。"""

    def test_fallback_index_markets_do_not_invent_a_csi_mapping(self):
        from ym_stock_data.sources import eastmoney_index

        self.assertNotIn("000985.CSI", eastmoney_index._INDEX_MARKETS)
        unsupported = eastmoney_index.fetch_index_kline("000985.CSI", period="daily")
        self.assertEqual("INVALID_PARAMS", unsupported.get("error_type"))
        self.assertEqual("unsupported index code", unsupported.get("error"))

    def test_csi_failure_chain_reports_honest_attempts(self):
        self.serve_table(KLINE_FIELDS, [])
        result = query("index_kline", index_code="000985.CSI", period="daily")
        meta = result["_meta"]
        self.assertEqual("error", meta["status"])
        self.assertIsNone(meta["provider_used"])
        codes = [a.get("error_code") for a in meta["attempts"]]
        self.assertIn("EMPTY_UPSTREAM", codes)
        for attempt in meta["attempts"]:
            self.assertEqual(
                {"provider", "status", "error_code", "latency_ms"}, set(attempt)
            )


class FixtureProvenanceTests(unittest.TestCase):
    """夹具在仓库内、自带来源与原封存 hash，且不含私有路径或凭据。"""

    def test_fixtures_are_in_repo_and_carry_source_provenance(self):
        for name, src_name in (
            ("index_daily_000985_csi.json", "probe-index-daily-000985-CSI.json"),
            ("limit_list_d_up_20261016.json", "probe-limit-list-d-U-20261016.json"),
        ):
            with self.subTest(fixture=name):
                data = _sealed(name)
                self.assertEqual(src_name, data["_source_sealed_file"])
                self.assertRegex(data["_source_sealed_sha256"], r"^[0-9a-f]{64}$")
                self.assertIn("tushare.citydata.club", data["_source_endpoint"])
                self.assertTrue(data["_source_collected_at"])
                self.assertTrue(data["items"])

    def test_fixtures_are_redacted_to_public_market_fields_only(self):
        kline = _sealed("index_daily_000985_csi.json")
        self.assertEqual(KLINE_FIELDS, kline["fields"])
        limit = _sealed("limit_list_d_up_20261016.json")
        # 不含本包不消费、且语义未证实或体量大的列。
        for dropped in ("fd_amount", "limit_amount", "total_mv", "float_mv",
                        "turnover_ratio", "up_stat", "limit_type"):
            self.assertNotIn(dropped, limit["fields"])
        for row in limit["items"]:
            self.assertEqual(set(limit["fields"]), set(row))

    def test_fixtures_contain_no_private_paths_or_credentials(self):
        banned = ("/Users/", "AgentScratch", "token", "secret", "password",
                  "api_key", "authorization", "credential")
        for name in ("index_daily_000985_csi.json", "limit_list_d_up_20261016.json"):
            raw = (FIXTURE_DIR / name).read_text(encoding="utf-8").lower()
            for word in banned:
                with self.subTest(fixture=name, word=word):
                    self.assertNotIn(word.lower(), raw)

    def test_limit_fixture_preserves_the_first_ne_last_evidence(self):
        rows = _sealed("limit_list_d_up_20261016.json")["items"]
        self.assertEqual(61, len(rows))
        differing = [r for r in rows if r["first_time"] != r["last_time"]]
        self.assertEqual(30, len(differing))


if __name__ == "__main__":
    unittest.main()
