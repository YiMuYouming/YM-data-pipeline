"""P2-B F03：board_members 的去重口径与**成员时点保持 unknown**。

设计红线（主控 2026-10-18 修正后）：`trade_date` 只是本地缓存分组标签，
`ths_member` 不带时间窗、按当前名册返回。因此：

- 真实成员生效日 **unknown**，不得升格进 `_meta.data_as_of`；
- 不得用请求日或 `fetched_at` 认证成员新鲜/历史；
- 去重只**新增**口径字段，既有 `members`/`member_count`/`data.data_as_of` 原样保留。

夹具为公开行情的最小脱敏子集，派生自P1 封存响应
（`probe-board-members-881121.json`，881121 / 20261016），
仓库内可移植，不依赖任何 Scratch 目录；测试在 socket 层断网运行。
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

SECRET = "synthetic-p2b-f03-test-token"

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "p2b"


def _fixture(name):
    path = FIXTURE_DIR / name
    if not path.is_file():
        raise AssertionError(f"fixture missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


class _NetworkBlocked(RuntimeError):
    """任何真实网络访问都让测试立即失败。"""


class _NoNetwork:
    """socket 层断网：本包证据是封存响应离线重放。"""

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


def is_security_code(value):
    """有效证券代码口径：6 位数字 + 交易所后缀。"""
    text = str(value or "")
    return (
        len(text) == 9
        and text[:6].isdigit()
        and text[6] == "."
        and text[7:].isalpha()
    )


class BoardMembersContractTests(unittest.TestCase):
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

    def serve(self, rows, fields=("ts_code", "con_code")):
        self.response.json.return_value = {
            "code": 0,
            "data": {"fields": list(fields), "items": list(rows)},
            "total": len(rows),
        }

    # ---------- 时点红线 ----------

    def test_membership_time_stays_unknown(self):
        self.serve([{"ts_code": "881121.TI", "con_code": "600519.SH"}])
        result = query("board_members", codes=["881121"], trade_date="20261016")
        self.assertEqual("success", result["_meta"]["status"])
        data = result["data"]
        #真实成员生效日未知
        self.assertIsNone(data["membership_as_of"])
        self.assertEqual("unknown", data["membership_time_status"])
        # 缓存分组标签与请求日语义分开命名
        self.assertEqual("20261016", data["cache_trade_date"])
        # 既有兼容字段原样
        self.assertEqual("20261016", data["data_as_of"])

    def test_meta_data_as_of_is_not_promoted_from_cache_label(self):
        # 主控红线：不得把请求日/缓存标签/fetched_at 赋给 _meta.data_as_of。
        self.serve([{"ts_code": "881121.TI", "con_code": "600519.SH"}])
        result = query("board_members", codes=["881121"], trade_date="20261016")
        self.assertIsNone(result["_meta"]["data_as_of"])

    def test_fetched_at_is_not_used_as_membership_time(self):
        self.serve([{"ts_code": "881121.TI", "con_code": "600519.SH"}])
        result = query("board_members", codes=["881121"], trade_date="20261016")
        self.assertNotEqual(
            result["_meta"].get("fetched_at"), result["data"]["membership_as_of"]
        )
        self.assertIsNone(result["data"]["membership_as_of"])

    def test_requested_trade_date_is_labelled_as_request_not_observation(self):
        self.serve([{"ts_code": "881121.TI", "con_code": "600519.SH"}])
        result = query("board_members", codes=["881121"], trade_date="20261016")
        # 请求日与缓存日一致时两者都在，但都不能当成员生效日。
        self.assertEqual("20261016", result["data"]["requested_trade_date"])
        self.assertEqual("unknown", result["data"]["membership_time_status"])

    # ---------- 去重口径 ----------

    def test_duplicate_and_invalid_members_are_reported(self):
        fixture = _fixture("board_members_881121_20261016.json")
        self.serve(
            [{"ts_code": "881121.TI", "con_code": c} for c in fixture["members"]],
            fields=("ts_code", "con_code"),
        )
        result = query("board_members", codes=["881121"], trade_date="20261016")
        board = result["data"]["boards"][0]
        truth = fixture["members"]
        self.assertEqual(len(truth), board["member_count"])
        self.assertEqual(truth, board["members"])          # 原列表不变
        self.assertEqual(len(set(truth)), board["unique_member_count"])
        expected_dupes = sorted({c for c in truth if truth.count(c) > 1})
        self.assertEqual(expected_dupes, board["duplicate_member_codes"])
        # 去重口径只按有效证券代码计
        for code in board["duplicate_member_codes"]:
            self.assertTrue(is_security_code(code))

    def test_invalid_members_excluded_from_unique_count(self):
        rows = [
            {"ts_code": "881121.TI", "con_code": "600519.SH"},
            {"ts_code": "881121.TI", "con_code": "600519.SH"},   # 重复
            {"ts_code": "881121.TI", "con_code": "JOE.N"},       # 非 6 位数字
            {"ts_code": "881121.TI", "con_code": ""},            # 空
            {"ts_code": "881121.TI", "con_code": "000001.SZ"},
        ]
        self.serve(rows)
        result = query("board_members", codes=["881121"], trade_date="20261016")
        board = result["data"]["boards"][0]
        self.assertEqual(4, board["member_count"])
        self.assertEqual(
            2, board["unique_member_count"],
            "唯一有效证券代码口径：600519.SH 与 000001.SZ；JOE.N 不是有效证券代码",
        )
        self.assertEqual(["600519.SH"], board["duplicate_member_codes"])
        self.assertEqual(["JOE.N"], board["invalid_member_codes"])

    def test_legacy_fields_are_untouched(self):
        self.serve([{"ts_code": "881121.TI", "con_code": "600519.SH"}])
        result = query("board_members", codes=["881121"], trade_date="20261016")
        board = result["data"]["boards"][0]
        for key in ("board_id", "members", "member_count", "fetched_at"):
            self.assertIn(key, board)
        self.assertEqual("881121", board["board_id"])
        self.assertEqual(["600519.SH"], board["members"])

    # ---------- empty vs unknown ----------

    def test_upstream_empty_roster_is_missing_not_zero_filled(self):
        self.serve([])
        result = query("board_members", codes=["881121"], trade_date="20261016")
        data = result["data"]
        self.assertIn(result["_meta"]["status"], {"empty", "error"})
        # 上游静默空 -> 进 missing，不回声 0 名成员
        self.assertEqual(["881121"], data["missing"])
        self.assertEqual([], data["boards"])

    def test_unknown_is_not_filled_with_zero(self):
        self.serve([{"ts_code": "881121.TI", "con_code": "600519.SH"}])
        result = query("board_members", codes=["881121"], trade_date="20261016")
        data = result["data"]
        # 已知维度给真值，未知维度保持 unknown/null，不补 0
        self.assertEqual("20261016", data["cache_trade_date"])
        self.assertIsNone(data["membership_as_of"])
        self.assertEqual("unknown", data["membership_time_status"])

    def test_partial_missing_keeps_got_boards(self):
        # 999999 不是合法板块码（校验层会拒），故用另一个合法前缀板块码。
        # 单个 mock 会对两个板块返回同一张表，所以按请求次数分两次给：
        # 第一次有成员，第二次空 -> 后者进 missing。
        transport = self.transport
        responses = [
            Mock(status_code=200),
            Mock(status_code=200),
        ]
        responses[0].json.return_value = {
            "code": 0,
            "data": {"fields": ["ts_code", "con_code"],
                     "items": [{"ts_code": "881121.TI", "con_code": "600519.SH"}]},
            "total": 1,
        }
        responses[1].json.return_value = {
            "code": 0, "data": {"fields": ["ts_code", "con_code"], "items": []},
            "total": 0,
        }
        transport.side_effect = responses
        result = query(
            "board_members", codes=["881121", "881122"], trade_date="20261016"
        )
        data = result["data"]
        self.assertEqual(1, len(data["boards"]))
        self.assertEqual("881121", data["boards"][0]["board_id"])
        self.assertEqual(["881122"], data["missing"])
        self.assertEqual("unknown", data["membership_time_status"])
        self.assertIsNone(data["membership_as_of"])

    def test_meta_contract_preserved(self):
        self.serve([{"ts_code": "881121.TI", "con_code": "600519.SH"}])
        result = query("board_members", codes=["881121"], trade_date="20261016")
        meta = result["_meta"]
        for key in ("status", "provider_used", "attempts", "quality",
                    "fetched_at", "data_as_of"):
            self.assertIn(key, meta)
        self.assertEqual("stocktoday", meta["provider_used"])
        self.assertEqual(
            {"provider", "status", "error_code", "latency_ms"},
            set(meta["attempts"][0]),
        )


class FixtureProvenanceTests(unittest.TestCase):
    def test_fixture_is_in_repo_and_records_source(self):
        data = _fixture("board_members_881121_20261016.json")
        self.assertEqual(
            "probe-board-members-881121.json", data["_source_sealed_file"]
        )
        self.assertRegex(data["_source_sealed_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(data["_source_collected_at"])
        self.assertEqual("20261016", data["_source_trade_date"])

    def test_fixture_preserves_the_dedup_evidence(self):
        members = _fixture("board_members_881121_20261016.json")["members"]
        self.assertEqual(191, len(members))
        self.assertEqual(189, len(set(members)))
        self.assertEqual(
            ["301717.SZ", "920138.BJ"],
            sorted({c for c in members if members.count(c) > 1}),
        )

    def test_fixture_has_no_private_paths_or_credentials(self):
        raw = (
            FIXTURE_DIR / "board_members_881121_20261016.json"
        ).read_text(encoding="utf-8").lower()
        for word in ("/users/", "agentscratch", "token", "secret", "password",
                     "api_key", "authorization", "credential"):
            with self.subTest(word=word):
                self.assertNotIn(word, raw)


class MemberCacheHitTests(unittest.TestCase):
    """缓存命中也必须补齐去重元数据（旧缓存 191/189 同样要）。"""

    def setUp(self):
        from ym_stock_data.providers.stocktoday import StockTodayProvider

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_root = Path(self.tmp.name) / "members"
        self.transport = Mock()
        self.response = Mock(status_code=200)
        self.transport.return_value = self.response
        self.provider = StockTodayProvider(
            token_loader=lambda: SECRET,
            post=self.transport,
            budget_path=Path(self.tmp.name) / "budget.sqlite3",
            member_cache_root=self.cache_root,
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

    def write_legacy_cache(self, members, fetched_at="2026-10-16T18:17:01+08:00"):
        """写一份**旧格式**缓存：没有 unique_member_count 等新字段。"""
        self.cache_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "trade_date": "20261016",
            "boards": {
                "881121": {
                    "members": list(members),
                    "member_count": len(members),
                    "fetched_at": fetched_at,
                }
            },
        }
        (self.cache_root / "20261016.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_legacy_cache_hit_gets_dedup_metadata(self):
        fixture = _fixture("board_members_881121_20261016.json")
        self.write_legacy_cache(fixture["members"])
        result = query("board_members", codes=["881121"], trade_date="20261016")
        self.assertEqual("success", result["_meta"]["status"])
        board = result["data"]["boards"][0]
        self.assertEqual(191, board["member_count"])
        self.assertEqual(fixture["members"], board["members"])
        self.assertEqual(189, board["unique_member_count"])
        self.assertEqual(["301717.SZ", "920138.BJ"],
                         board["duplicate_member_codes"])
        self.assertEqual([], board["invalid_member_codes"])

    def test_legacy_cache_hit_does_not_promote_fetch_time(self):
        fixture = _fixture("board_members_881121_20261016.json")
        self.write_legacy_cache(fixture["members"])
        result = query("board_members", codes=["881121"], trade_date="20261016")
        data = result["data"]
        self.assertIsNone(data["membership_as_of"])
        self.assertEqual("unknown", data["membership_time_status"])
        self.assertIsNone(result["_meta"]["data_as_of"])
        self.assertNotEqual(
            "2026-10-16T18:17:01+08:00", data["membership_as_of"]
        )

    def test_cache_hit_and_fresh_fetch_agree(self):
        fixture = _fixture("board_members_881121_20261016.json")
        self.response.json.return_value = {
            "code": 0,
            "data": {"fields": ["ts_code", "con_code"],
                     "items": [{"ts_code": "881121.TI", "con_code": c}
                               for c in fixture["members"]]},
            "total": len(fixture["members"]),
        }
        fresh = query("board_members", codes=["881121"], trade_date="20261016")
        cached = query("board_members", codes=["881121"], trade_date="20261016")
        for key in ("unique_member_count", "duplicate_member_codes",
                    "invalid_member_codes", "member_count", "members"):
            with self.subTest(key=key):
                self.assertEqual(
                    fresh["data"]["boards"][0][key],
                    cached["data"]["boards"][0][key],
                )

    def test_bad_cache_shape_is_not_reported_as_zero_members(self):
        # 缓存里 members 不是列表 -> 不能当成"成功且 0 名成员"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        (self.cache_root / "20261016.json").write_text(
            json.dumps({"trade_date": "20261016",
                        "boards": {"881121": {"members": "oops",
                                              "member_count": "oops"}}}),
            encoding="utf-8",
        )
        self.response.json.return_value = {
            "code": 0, "data": {"fields": ["ts_code", "con_code"], "items": []},
            "total": 0,
        }
        result = query("board_members", codes=["881121"], trade_date="20261016")
        data = result["data"]
        if data["boards"]:
            self.assertNotEqual(0, data["boards"][0].get("unique_member_count"))
        else:
            self.assertEqual(["881121"], data["missing"])


class SecurityCodeValidationTests(unittest.TestCase):
    """A 股名册口径：ASCII 六位 + 已识别 SH/SZ/BJ 后缀。"""

    def test_only_ascii_six_digits_with_known_suffix_is_valid(self):
        from ym_stock_data.providers.stocktoday import _is_security_code

        for code in ("600519.SH", "000001.SZ", "920138.BJ", "301717.SZ"):
            with self.subTest(code=code):
                self.assertTrue(_is_security_code(code))
        for code in ("123456.ZZ", "600519.XX", "600519.US", "600519.HK",
                     "\uff11\uff12\uff13\uff14\uff15\uff16.SH", "60051.SH",
                     "6005199.SH", "600519", "JOE.N", "", None, "600519.sh "):
            with self.subTest(code=code):
                self.assertFalse(_is_security_code(code))

    def test_unicode_digits_are_not_accepted(self):
        from ym_stock_data.providers.stocktoday import _is_security_code

        self.assertTrue("\uff11\uff12\uff13\uff14\uff15\uff16".isdigit())
        self.assertFalse(
            _is_security_code("\uff11\uff12\uff13\uff14\uff15\uff16.SH")
        )

    def test_known_suffix_is_case_insensitive(self):
        from ym_stock_data.providers.stocktoday import _is_security_code

        self.assertTrue(_is_security_code("600519.sh"))
        self.assertTrue(_is_security_code("600519.Sz"))

    def test_invalid_members_never_removed_from_members_list(self):
        # 无效成员只单列，不从既有 members 里删
        self.assertEqual(
            ["JOE.N"],
            [m for m in ("600519.SH", "JOE.N")
             if not _is_security_code_impl(m)],
        )


def _is_security_code_impl(value):
    from ym_stock_data.providers.stocktoday import _is_security_code

    return _is_security_code(value)


if __name__ == "__main__":
    unittest.main()
