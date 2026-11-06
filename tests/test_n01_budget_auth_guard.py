"""N01：StockToday 本地预算缺省、跨进程原子性、临时限流与真鉴权分离。

离线测试：transport 全部注入，不访问真实供应商、不写生产预算库。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from tests.fixed_clock import FIXED_NOW, FIXED_NOW_ISO
from ym_stock_data.providers import stocktoday as st
from ym_stock_data.providers.stocktoday import RequestBudget, StockTodayProvider

SECRET = "synthetic-n01-token-0000000000"
PIPELINE_ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = timezone(timedelta(hours=8))


def _shanghai_ts(year, month, day, hour, minute, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=SHANGHAI).timestamp()


def _response(status_code=200, payload=None, text=""):
    response = Mock(status_code=status_code)
    response.text = text
    if payload is None:
        response.json.side_effect = ValueError("plain-text gateway response")
    else:
        response.json.return_value = payload
    return response


class _Clock:
    def __init__(self, now):
        self._now = now

    def time(self):
        return self._now

    def monotonic(self):
        return 0.0


def _provider(transport, budget, *, now=1_790_000_000.0):
    return StockTodayProvider(
        token_loader=lambda: SECRET,
        post=transport,
        budget=budget,
        clock=_Clock(now),
    )


def _tencent_snapshot_stub(quote_time):
    """回退源夹具：quote_time 由调用方给出，不由运行当日/当前墙钟推导。"""

    from ym_stock_data.providers.base import ProviderOutcome

    class TencentStub:
        name = "tencent"

        def call(self, _intent, params):
            rows = {
                code: {
                    "code": code, "price": 10.0, "last_close": 9.5, "open": 9.8,
                    "high": 10.2, "low": 9.7, "volume": 1000, "amount": 10000.0,
                    "quote_time": quote_time, "change_pct": 1.0,
                }
                for code in params["codes"]
            }
            return ProviderOutcome("tencent", "success", data=rows)

    return TencentStub()


class DefaultLimitTests(unittest.TestCase):
    """缺省限额必须遵从文档的 80/分钟、18000/日，显式配置仍然有效。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "budget.sqlite3"

    def test_defaults_follow_documented_limits(self):
        with patch.dict(os.environ, {}, clear=True):
            budget = RequestBudget(self.path)
        self.assertEqual(80, budget.per_minute)
        self.assertEqual(18000, budget.per_day)

    def test_invalid_environment_falls_back_to_documented_default(self):
        env = {"YM_STOCKTODAY_PER_MINUTE": "eighty", "YM_STOCKTODAY_PER_DAY": "-1"}
        with patch.dict(os.environ, env, clear=True):
            budget = RequestBudget(self.path)
        self.assertEqual(80, budget.per_minute)
        self.assertEqual(18000, budget.per_day)

    def test_explicit_environment_wins_and_zero_stays_an_explicit_opt_out(self):
        with patch.dict(os.environ, {"YM_STOCKTODAY_PER_MINUTE": "7", "YM_STOCKTODAY_PER_DAY": "9"}, clear=True):
            budget = RequestBudget(self.path)
        self.assertEqual((7, 9), (budget.per_minute, budget.per_day))

        alias = {"YIMU_STOCKTODAY_PER_MINUTE": "11", "YIMU_STOCKTODAY_PER_DAY": "13"}
        with patch.dict(os.environ, alias, clear=True):
            budget = RequestBudget(self.path)
        self.assertEqual((11, 13), (budget.per_minute, budget.per_day))

        opt_out = {"YM_STOCKTODAY_PER_MINUTE": "0", "YM_STOCKTODAY_PER_DAY": "0"}
        with patch.dict(os.environ, opt_out, clear=True):
            budget = RequestBudget(self.path)
        self.assertEqual((0, 0), (budget.per_minute, budget.per_day))

    def test_budget_database_can_be_isolated_from_the_production_file(self):
        isolated = self.path.parent / "isolated.sqlite3"
        with patch.dict(os.environ, {"YM_STOCKTODAY_BUDGET_PATH": str(isolated)}, clear=True):
            provider = StockTodayProvider(token_loader=lambda: SECRET, post=Mock())
        self.assertEqual(isolated, provider.budget.path)

    def test_provider_without_environment_uses_the_default_database_path(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = StockTodayProvider(token_loader=lambda: SECRET, post=Mock())
        self.assertEqual(st.DEFAULT_BUDGET_PATH, provider.budget.path)

    def test_request_source_defaults_to_known_entrypoints(self):
        cases = {
            "scripts/build_next_day_plan.py": "mw-c6",
            "/workspace/tianyuan/src/tianyuan/web/server.py": "tianyuan-web",
            "/workspace/tianyuan/src/tianyuan/live.py": "tianyuan-live",
            "/workspace/DailyBriefing/scripts/run_current_a_share_query.py": "ymintel-a-share",
            "/workspace/live-dashboard/scripts/bridge.py": "live-dashboard",
            "/workspace/.venv/bin/ym-data": "ym-data-cli",
            "/workspace/.venv/bin/pytest": "pytest",
            "/unrelated/scripts/bridge.py": "unknown",
        }
        with patch.dict(os.environ, {}, clear=True):
            for entrypoint, source in cases.items():
                with self.subTest(entrypoint=entrypoint), patch.object(sys, "argv", [entrypoint]):
                    self.assertEqual(source, st.request_source())
            with patch.object(Path, "cwd", return_value=Path("/workspace/live-dashboard")):
                with patch.object(sys, "argv", ["scripts/bridge.py"]):
                    self.assertEqual("live-dashboard", st.request_source())

    def test_explicit_request_source_preserves_sanitization_and_priority(self):
        with patch.object(sys, "argv", ["scripts/build_next_day_plan.py"]):
            with patch.dict(os.environ, {st.REQUEST_SOURCE_ENV: "mw-check /A"}, clear=True):
                self.assertEqual("mw-checkA", st.request_source())
            with patch.dict(os.environ, {st.REQUEST_SOURCE_ENV: " 无效 "}, clear=True):
                self.assertEqual("unknown", st.request_source())


class BoundaryTests(unittest.TestCase):
    """分钟窗口与上海日界。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "budget.sqlite3"

    def test_minute_window_slides(self):
        budget = RequestBudget(self.path, per_minute=2, per_day=0)
        now = 1_790_000_000.0
        self.assertTrue(budget.acquire(now=now))
        self.assertTrue(budget.acquire(now=now + 0.5))
        self.assertFalse(budget.acquire(now=now + 1.0))
        self.assertTrue(budget.acquire(now=now + 61.0))

    def test_day_boundary_is_shanghai_midnight(self):
        budget = RequestBudget(self.path, per_minute=0, per_day=2)
        late = _shanghai_ts(2026, 11, 3, 23, 59, 50)
        self.assertTrue(budget.acquire(now=late))
        self.assertTrue(budget.acquire(now=late + 5))
        self.assertFalse(budget.acquire(now=late + 9))
        self.assertTrue(budget.acquire(now=_shanghai_ts(2026, 11, 4, 0, 0, 5)))

    def test_budget_is_shared_atomically_across_processes(self):
        script = (
            "import sys;"
            f"sys.path.insert(0, {str(PIPELINE_ROOT)!r});"
            "from ym_stock_data.providers.stocktoday import RequestBudget;"
            f"budget = RequestBudget({str(self.path)!r}, per_minute=5, per_day=0);"
            "print('1' if budget.acquire(now=1790000000.0) else '0')"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(12)
        ]
        granted = 0
        for process in processes:
            stdout, stderr = process.communicate(timeout=120)
            self.assertEqual(0, process.returncode, stderr)
            granted += stdout.strip() == "1"
        self.assertEqual(5, granted)


class ClassificationTests(unittest.TestCase):
    """429 / IP 临时封禁是限流（带冷却），401/403 真鉴权失败不冷却。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "budget.sqlite3"
        self.budget = RequestBudget(self.path, per_minute=0, per_day=0)

    def _call(self, clock):
        provider = StockTodayProvider(
            token_loader=lambda: SECRET, post=self.transport, budget=self.budget, clock=clock
        )
        return provider._request_table("daily", {"ts_code": "600519.SH"})

    def test_temporary_ip_ban_403_is_rate_limited_and_cools_down(self):
        self.transport = Mock(return_value=_response(
            403, text="您的IP存在异常，已被临时封禁，请稍后再试"))
        clock = _Clock(1_790_000_000.0)
        first = self._call(clock)
        self.assertEqual("RATE_LIMITED", first.error_code)
        self.assertEqual("ip_temporary_ban", first.provenance["vendor_reason"])
        self.assertEqual(1, self.transport.call_count)
        second = self._call(clock)
        self.assertEqual("RATE_LIMITED", second.error_code)
        self.assertIsNotNone(second.provenance.get("cooldown_remaining_sec"))
        self.assertEqual(1, self.transport.call_count)

    def test_rate_limit_cooldown_expires(self):
        self.transport = Mock(return_value=_response(429))
        clock = _Clock(1_790_000_000.0)
        self.assertEqual("RATE_LIMITED", self._call(clock).error_code)
        clock._now += st.VENDOR_COOLDOWN_SECONDS + 1
        self._call(clock)
        self.assertEqual(2, self.transport.call_count)

    def test_real_authentication_failure_is_not_a_cooldown(self):
        self.transport = Mock(return_value=_response(401, text="unauthorized"))
        clock = _Clock(1_790_000_000.0)
        first = self._call(clock)
        self.assertEqual("AUTH_DENIED", first.error_code)
        self.assertNotIn("vendor_reason", first.provenance)
        second = self._call(clock)
        self.assertEqual("AUTH_DENIED", second.error_code)
        self.assertEqual(2, self.transport.call_count)

    def test_403_with_auth_message_stays_auth_denied(self):
        self.transport = Mock(return_value=_response(
            403, payload={"code": 403, "msg": "token invalid"}))
        outcome = self._call(_Clock(1_790_000_000.0))
        self.assertEqual("AUTH_DENIED", outcome.error_code)

    def test_upstream_ban_message_without_http_status_is_rate_limited(self):
        self.transport = Mock(return_value=_response(
            200, payload={"code": 403, "msg": "IP异常，临时封禁10分钟"}))
        outcome = self._call(_Clock(1_790_000_000.0))
        self.assertEqual("RATE_LIMITED", outcome.error_code)
        self.assertEqual("ip_temporary_ban", outcome.provenance["vendor_reason"])

    def test_cooldown_never_echoes_the_token(self):
        self.transport = Mock(return_value=_response(429))
        outcome = self._call(_Clock(1_790_000_000.0))
        self.assertNotIn(SECRET, json.dumps(outcome.provenance))


class BudgetDiagnosticsTests(unittest.TestCase):
    """预算耗尽的归属诊断必须脱敏且能区分分钟/日上限。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _provider(self, traffic, budget):
        return StockTodayProvider(
            token_loader=lambda: SECRET,
            post=traffic,
            budget=budget,
            clock=_Clock(1_790_000_000.0),
        )

    def test_minute_and_day_scope_are_reported_without_secrets(self):
        traffic = Mock(return_value=_response(
            200, payload={"code": 0, "data": [{"ts_code": "600519.SH"}]}))
        minute = RequestBudget(self.root / "minute.sqlite3", per_minute=1, per_day=0)
        provider = self._provider(traffic, minute)
        self.assertEqual("success", provider._request_table("daily", {"ts_code": "600519.SH"}).status)
        refused = provider._request_table("daily", {"ts_code": "600519.SH"})
        self.assertEqual("LOCAL_BUDGET_EXHAUSTED", refused.error_code)
        attribution = refused.provenance["local_budget"]
        self.assertEqual(("minute", 1, 1), (attribution["scope"], attribution["used"], attribution["cap"]))
        self.assertNotIn(SECRET, json.dumps(refused.provenance))

        day = RequestBudget(self.root / "day.sqlite3", per_minute=0, per_day=1)
        provider = self._provider(traffic, day)
        self.assertEqual("success", provider._request_table("daily", {"ts_code": "600519.SH"}).status)
        refused = provider._request_table("daily", {"ts_code": "600519.SH"})
        attribution = refused.provenance["local_budget"]
        self.assertEqual(("day", 1, 1), (attribution["scope"], attribution["used"], attribution["cap"]))

    def test_failed_upstream_calls_still_count_against_the_cap(self):
        traffic = Mock(side_effect=requests.Timeout("upstream"))
        budget = RequestBudget(self.root / "cap.sqlite3", per_minute=2, per_day=0)
        provider = self._provider(traffic, budget)
        self.assertEqual("TIMEOUT", provider._request_table("daily", {"ts_code": "600519.SH"}).error_code)
        self.assertEqual("TIMEOUT", provider._request_table("daily", {"ts_code": "600519.SH"}).error_code)
        refused = provider._request_table("daily", {"ts_code": "600519.SH"})
        self.assertEqual("LOCAL_BUDGET_EXHAUSTED", refused.error_code)
        self.assertEqual(2, traffic.call_count)

    def test_local_budget_failure_is_reported_before_any_transport_call(self):
        traffic = Mock()
        budget = RequestBudget(self.root / "zero.sqlite3", per_minute=1, per_day=0)
        provider = self._provider(traffic, budget)
        self.assertTrue(budget.acquire(now=1_790_000_000.0))
        refused = provider._request_table("daily", {"ts_code": "600519.SH"})
        self.assertEqual("LOCAL_BUDGET_EXHAUSTED", refused.error_code)
        traffic.assert_not_called()


class ConsumerRetryTests(unittest.TestCase):
    """消费链只对可重试失败重试，限流/预算/鉴权不重试。"""

    def test_collect_daily_does_not_retry_rate_limit_or_budget_failures(self):
        from ym_stock_data import __main__ as cli

        store = Mock()
        for code in ("RATE_LIMITED", "LOCAL_BUDGET_EXHAUSTED", "AUTH_DENIED"):
            with self.subTest(code=code):
                response = {"_meta": {"status": "error", "attempts": [{"error_code": code}]}}
                with patch.object(cli, "canonical_query", return_value=response) as call:
                    cli._collect_daily(store, "20261103")
                self.assertEqual(1, call.call_count)

    def test_collect_daily_still_retries_a_transient_timeout(self):
        from ym_stock_data import __main__ as cli

        store = Mock()
        timeout = {"_meta": {"status": "error", "attempts": [{"error_code": "TIMEOUT"}]}}
        success = {"_meta": {"status": "success", "attempts": []}, "data": {}}
        with patch.object(cli, "canonical_query", side_effect=[timeout, success]) as call:
            cli._collect_daily(store, "20261103")
        self.assertEqual(2, call.call_count)


class CooldownDurationTests(unittest.TestCase):
    """网关给出的预计解封时间优先；解析不到才用保守默认。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "budget.sqlite3"
        self.budget = RequestBudget(self.path, per_minute=0, per_day=0)
        self.clock = _Clock(_shanghai_ts(2026, 11, 3, 10, 0, 0))
        self.transport = Mock()

    def _provider(self):
        return StockTodayProvider(
            token_loader=lambda: SECRET,
            post=self.transport,
            budget=self.budget,
            clock=self.clock,
        )

    def _call(self):
        return self._provider()._request_table("daily", {"ts_code": "600519.SH"})

    def test_fourteen_minute_ban_is_honoured_end_to_end(self):
        self.transport.return_value = _response(
            403, text="您的IP存在异常，预计14分钟后自动解封，请稍后再试")
        outcome = self._call()
        self.assertEqual("RATE_LIMITED", outcome.error_code)
        self.assertEqual(840, outcome.provenance["cooldown_seconds"])
        self.assertTrue(outcome.provenance["cooldown_persisted"])
        remaining = self.budget.cooldown_remaining(now=self.clock._now + 839, scope="stocktoday")
        self.assertGreater(remaining, 0)
        self.assertIsNone(self.budget.cooldown_remaining(now=self.clock._now + 841, scope="stocktoday"))

        clock_later = _Clock(self.clock._now + 60)
        provider = StockTodayProvider(
            token_loader=lambda: SECRET, post=self.transport, budget=self.budget, clock=clock_later
        )
        blocked = provider._request_table("daily", {"ts_code": "600519.SH"})
        self.assertEqual("RATE_LIMITED", blocked.error_code)
        self.assertEqual("cooldown", blocked.provenance["vendor_reason"])
        self.assertEqual(1, self.transport.call_count)

    def test_retry_after_header_is_preserved_past_one_hour(self):
        response = _response(429)
        response.headers = {"Retry-After": "120"}
        self.transport.return_value = response
        self.assertEqual(120, self._call().provenance["cooldown_seconds"])

        self.budget = RequestBudget(Path(self.path.parent) / "b.sqlite3", per_minute=0, per_day=0)
        response = _response(429)
        response.headers = {"Retry-After": "7200"}
        self.transport.return_value = response
        self.assertEqual(7200, self._call().provenance["cooldown_seconds"])
        self.budget = RequestBudget(self.budget.path, per_minute=0, per_day=0)
        self.clock._now += 3601
        blocked = self._call()
        self.assertEqual(3599, blocked.provenance["cooldown_remaining_sec"])
        self.assertEqual(2, self.transport.call_count)

    def test_retry_after_http_date_uses_the_provider_clock(self):
        response = _response(429)
        response.headers = {"Retry-After": formatdate(self.clock.time() + 7201, usegmt=True)}
        self.transport.return_value = response
        self.assertEqual(7201, self._call().provenance["cooldown_seconds"])

    def test_rejection_is_kept_without_response_body_or_credential(self):
        self.transport.return_value = _response(
            403, payload={"code": 1, "msg": f"IP异常，临时封禁，预计2小时后解封 {SECRET}"})
        with patch.dict(os.environ, {st.REQUEST_SOURCE_ENV: "n01-first-hop"}):
            result = self._call()
        self.assertEqual(7200, result.provenance["cooldown_seconds"])
        self.clock._now += 61
        self.assertEqual("cooldown", self._call().provenance["vendor_reason"])

        with sqlite3.connect(self.path) as con:
            rows = con.execute(
                "SELECT source, http_status, upstream_code, reason, wait_source, wait_seconds "
                "FROM vendor_cooldown_events"
            ).fetchall()
        self.assertEqual([("n01-first-hop", 403, 1, "ip_temporary_ban", "message", 7200)], rows)
        self.assertNotIn(SECRET.encode(), self.path.read_bytes())

    def test_unknown_ban_wait_uses_conservative_default(self):
        self.transport.return_value = _response(403, text="IP异常，临时封禁")
        self.assertEqual(900, self._call().provenance["cooldown_seconds"])

    def test_preparation_spaces_wire_requests_using_the_shared_budget(self):
        clock = Mock()
        elapsed = 0.0
        starts = []

        def sleep(seconds):
            nonlocal elapsed
            elapsed += seconds

        clock.time = lambda: self.clock.time() + elapsed
        clock.monotonic = lambda: elapsed
        clock.sleep = sleep

        def post(*args, **kwargs):
            starts.append(elapsed)
            sleep(0.25)
            return _response(200, payload={"code": 0, "data": [{"ts_code": "600519.SH"}]})

        with patch.dict(os.environ, {"YM_STOCKTODAY_REQUEST_INTERVAL_SECONDS": "2"}), \
             patch.object(st, "_last_paced_request_at", None):
            for _ in range(3):
                # Public query constructs a fresh provider for each intent call.
                provider = StockTodayProvider(token_loader=lambda: SECRET, post=post,
                                              budget=self.budget, clock=clock)
                self.assertEqual("success", provider._request_table("daily", {"ts_code": "600519.SH"}).status)
        self.assertEqual([0.0, 2.0, 4.0], starts)
        with sqlite3.connect(self.path) as con:
            self.assertEqual(3, con.execute("SELECT COUNT(*) FROM requests").fetchone()[0])

    def test_realtime_path_ignores_preparation_clock_without_opt_in(self):
        self.transport.return_value = _response(
            200, payload={"code": 0, "data": [{"ts_code": "600519.SH"}]})
        with patch.dict(os.environ), patch.object(st, "_last_paced_request_at", 0.0):
            os.environ.pop("YM_STOCKTODAY_REQUEST_INTERVAL_SECONDS", None)
            provider = self._provider()
            for _ in range(2):
                result = provider._request_intraday_table("rt_k", {"ts_code": "600519.SH"}, timeout_sec=4)
                self.assertEqual("success", result.status)
        self.assertEqual(2, self.transport.call_count)

    def test_concurrent_cooldowns_never_shorten_the_deadline(self):
        start = _shanghai_ts(2026, 11, 3, 10, 0, 0)
        self.budget.start_cooldown(840, now=start, scope="stocktoday")
        self.budget.start_cooldown(60, now=start + 100, scope="stocktoday")
        self.assertGreater(self.budget.cooldown_remaining(now=start + 101, scope="stocktoday"), 700)
        self.budget.start_cooldown(900, now=start + 200, scope="stocktoday")
        self.assertGreater(self.budget.cooldown_remaining(now=start + 201, scope="stocktoday"), 890)

    def test_corrupted_state_store_fails_closed(self):
        self.path.write_bytes(b"not a sqlite database at all")
        self.transport.return_value = _response(
            200, payload={"code": 0, "data": [{"ts_code": "600519.SH"}]})
        outcome = self._call()
        self.assertEqual("LOCAL_BUDGET_UNAVAILABLE", outcome.error_code)
        self.transport.assert_not_called()

    def test_new_diagnostics_do_not_drop_existing_provenance_fields(self):
        self.transport.return_value = _response(429)
        provenance = self._call().provenance
        for key in ("http_status", "upstream_code", "total_present"):
            self.assertIn(key, provenance)
        self.assertEqual(429, provenance["http_status"])


class AttributionAndSchemaTests(unittest.TestCase):
    """归属诊断可定位 pid/来源；旧预算库只做增量迁移，不清历史。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_refusal_reports_pid_source_and_minute_sources(self):
        path = self.root / "budget.sqlite3"
        now = _shanghai_ts(2026, 11, 3, 10, 0, 0)
        budget = RequestBudget(path, per_minute=1, per_day=0)
        with patch.dict(os.environ, {"YM_STOCKTODAY_REQUEST_SOURCE": "tianyuan-test"}, clear=False):
            self.assertTrue(budget.acquire(now=now))
            attribution = budget.describe_refusal(now=now + 1)
        self.assertEqual("minute", attribution["scope"])
        self.assertEqual(os.getpid(), attribution["requester"]["pid"])
        self.assertEqual("tianyuan-test", attribution["requester"]["source"])
        self.assertEqual([{"source": "tianyuan-test", "count": 1}], attribution["minute_sources"])
        self.assertGreater(attribution["wait_seconds"], 0)
        self.assertLessEqual(attribution["wait_seconds"], 60)

    def test_old_request_log_is_migrated_without_resetting_history(self):
        import sqlite3

        path = self.root / "legacy.sqlite3"
        now = _shanghai_ts(2026, 11, 3, 10, 0, 0)
        with sqlite3.connect(path) as con:
            con.execute("CREATE TABLE requests (at REAL NOT NULL)")
            con.execute("INSERT INTO requests VALUES (?)", (now - 5,))
            con.execute("INSERT INTO requests VALUES (?)", (now - 4,))
        budget = RequestBudget(path, per_minute=10, per_day=100)
        self.assertTrue(budget.acquire(now=now))
        with sqlite3.connect(path) as con:
            rows = con.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            columns = {row[1] for row in con.execute("PRAGMA table_info(requests)")}
            legacy = con.execute("SELECT COUNT(*) FROM requests WHERE pid IS NULL").fetchone()[0]
        self.assertEqual(3, rows)
        self.assertEqual(2, legacy)
        self.assertTrue({"at", "pid", "source"}.issubset(columns))


class ColdStartTests(unittest.TestCase):
    """父目录不存在是正常初始化，不是状态库损坏。"""

    def test_first_query_bootstraps_a_missing_budget_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "deeper" / "budget.sqlite3"
            self.assertFalse(path.parent.exists())
            transport = Mock(return_value=_response(
                200, payload={"code": 0, "data": [{"ts_code": "600519.SH"}]}))
            provider = StockTodayProvider(
                token_loader=lambda: SECRET,
                post=transport,
                budget=RequestBudget(path, per_minute=5, per_day=10),
                clock=_Clock(1_790_000_000.0),
            )
            outcome = provider._request_table("daily", {"ts_code": "600519.SH"})
            self.assertEqual("success", outcome.status)
            self.assertEqual(1, transport.call_count)
            self.assertTrue(path.exists())

    def test_corrupted_store_still_fails_closed_on_the_same_path_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "budget.sqlite3"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"definitely not sqlite")
            transport = Mock()
            provider = StockTodayProvider(
                token_loader=lambda: SECRET,
                post=transport,
                budget=RequestBudget(path, per_minute=5, per_day=10),
                clock=_Clock(1_790_000_000.0),
            )
            self.assertEqual("LOCAL_BUDGET_UNAVAILABLE", provider._request_table(
                "daily", {"ts_code": "600519.SH"}).error_code)
            transport.assert_not_called()


class PublicDiagnosticsTests(unittest.TestCase):
    """公共 query 信封必须带脱敏的预算/冷却归属。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        from ym_stock_data import api
        from ym_stock_data.provider_state import ProviderState

        self.api = api
        self.addCleanup(patch.stopall)
        patch.object(api, "_STATE", ProviderState(self.root / "state.sqlite3")).start()
        patch.dict(os.environ, {"YM_STOCKTODAY_REQUEST_SOURCE": "tianyuan-test"}, clear=False).start()

    def _provider(self, transport, budget):
        return StockTodayProvider(
            token_loader=lambda: SECRET, post=transport, budget=budget,
            clock=_Clock(1_790_000_000.0),
        )

    def _query(self, provider, intent="stocktoday_data", **params):
        with patch.dict(self.api.PROVIDER_REGISTRY, {"stocktoday": provider}):
            return self.api.query(intent, **params)

    def _call(self, provider):
        return self._query(provider, api_name="daily", params={"ts_code": "600519.SH"})

    def test_public_envelope_exposes_minute_budget_scope_and_rolling_wait(self):
        transport = Mock(return_value=_response(
            200, payload={"code": 0, "data": [{"ts_code": "600519.SH"}]}))
        provider = self._provider(transport, RequestBudget(
            self.root / "minute.sqlite3", per_minute=1, per_day=0))
        self.assertEqual("success", self._call(provider)["_meta"]["status"])
        result = self._call(provider)
        self.assertEqual("error", result["_meta"]["status"])
        budget = result["_meta"]["local_budget"]
        self.assertEqual("stocktoday", budget["provider"])
        self.assertEqual(("minute", 1, 1), (budget["scope"], budget["used"], budget["cap"]))
        self.assertGreater(budget["wait_seconds"], 0)
        self.assertLessEqual(budget["wait_seconds"], 60)
        self.assertEqual("tianyuan-test", budget["requester"]["source"])
        self.assertEqual(os.getpid(), budget["requester"]["pid"])
        self.assertEqual("tianyuan-test", budget["minute_sources"][0]["source"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual("LOCAL_BUDGET_EXHAUSTED", result["_meta"]["reason_code"])
        kinds = [item["kind"] for item in result["_meta"]["attempt_diagnostics"]]
        self.assertIn("local_budget", kinds)

    def test_day_cap_is_prior_and_has_no_minute_wait(self):
        transport = Mock(return_value=_response(
            200, payload={"code": 0, "data": [{"ts_code": "600519.SH"}]}))
        provider = self._provider(transport, RequestBudget(
            self.root / "day.sqlite3", per_minute=1, per_day=1))
        self.assertEqual("success", self._call(provider)["_meta"]["status"])
        result = self._call(provider)
        budget = result["_meta"]["local_budget"]
        self.assertEqual("day", budget["scope"])
        self.assertIsNone(budget["wait_seconds"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_public_envelope_exposes_vendor_cooldown_retry_after(self):
        transport = Mock(return_value=_response(
            403, text="您的IP存在异常，预计14分钟后自动解封，请稍后再试"))
        provider = self._provider(transport, RequestBudget(
            self.root / "ban.sqlite3", per_minute=0, per_day=0))
        result = self._call(provider)
        self.assertEqual("error", result["_meta"]["status"])
        cooldown = result["_meta"]["vendor_cooldown"]
        self.assertEqual("stocktoday", cooldown["provider"])
        self.assertEqual("ip_temporary_ban", cooldown["vendor_reason"])
        self.assertEqual(840, cooldown["retry_after_seconds"])
        self.assertEqual(403, cooldown["http_status"])
        self.assertEqual("message", cooldown["wait_source"])
        self.assertEqual(840, cooldown["vendor_wait_seconds"])
        self.assertNotIn(SECRET, json.dumps(result))

    def _fallback_snapshot(self, *, clock, quote_time, filename="fallback.sqlite3"):
        """回退链公共信封：验证时钟与 quote_time 成对冻结，质量校验仍走真实实现。"""

        stub = _tencent_snapshot_stub(quote_time)
        transport = Mock(return_value=_response(
            200, payload={"code": 0, "data": [{"ts_code": "600519.SH"}]}))
        provider = self._provider(transport, RequestBudget(
            self.root / filename, per_minute=1, per_day=0))
        provider._request_table("daily", {"ts_code": "600519.SH"})  # 用掉唯一名额

        with patch.object(self.api, "_now_shanghai", return_value=clock), patch.dict(
            self.api.PROVIDER_REGISTRY, {"stocktoday": provider, "tencent": stub}
        ):
            return self.api.query("stock_snapshot", codes=["600000"], use_case="realtime_poll")

    def test_fallback_success_is_not_reported_as_the_refused_provider(self):
        # 跨午夜真实墙钟下行情时点必然落在闭市区间，只会命中真实质量门的
        # QUALITY_SNAPSHOT_STALE。这里把时钟与 quote_time 冻结到同一盘中时点，
        # 只隔离时间，不绕过、不放宽质量校验。
        result = self._fallback_snapshot(clock=FIXED_NOW, quote_time=FIXED_NOW_ISO)

        self.assertEqual("tencent", result["_meta"]["provider_used"])
        self.assertIn("600000", result["data"])
        self.assertEqual("stocktoday", result["_meta"]["local_budget"]["provider"])
        self.assertEqual("stocktoday", result["_meta"]["attempt_diagnostics"][0]["provider"])
        self.assertNotEqual("error", result["_meta"]["status"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_fallback_quality_boundary_is_measured_on_the_same_frozen_clock(self):
        from ym_stock_data.routing import route_for

        max_age = route_for("stock_snapshot", {"use_case": "realtime_poll"}).max_age_sec
        fresh = self._fallback_snapshot(
            clock=FIXED_NOW + timedelta(seconds=max_age),
            quote_time=FIXED_NOW_ISO,
            filename="boundary-ok.sqlite3",
        )
        self.assertEqual("tencent", fresh["_meta"]["provider_used"])
        self.assertNotEqual("error", fresh["_meta"]["status"])

        stale = self._fallback_snapshot(
            clock=FIXED_NOW + timedelta(seconds=max_age + 1),
            quote_time=FIXED_NOW_ISO,
            filename="boundary-stale.sqlite3",
        )
        self.assertIsNone(stale["_meta"]["provider_used"])
        self.assertEqual("error", stale["_meta"]["status"])
        attempts = {item["provider"]: item for item in stale["_meta"]["attempts"]}
        self.assertEqual("quality_failure", attempts["tencent"]["status"])
        self.assertEqual("QUALITY_SNAPSHOT_STALE", attempts["tencent"]["error_code"])
        self.assertEqual("stocktoday", stale["_meta"]["local_budget"]["provider"])
        self.assertNotIn(SECRET, json.dumps(stale))

    def test_auth_failure_stays_the_public_reason_code(self):
        transport = Mock(return_value=_response(401, text="unauthorized"))
        provider = self._provider(transport, RequestBudget(
            self.root / "auth.sqlite3", per_minute=0, per_day=0))
        result = self._call(provider)
        self.assertEqual("AUTH_DENIED", result["_meta"]["reason_code"])
        self.assertNotIn("vendor_cooldown", result["_meta"])


if __name__ == "__main__":
    unittest.main()
