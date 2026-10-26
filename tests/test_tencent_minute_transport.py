"""R3 item8a：腾讯分钟 HTTP/传输失败不得吞成合法 empty。

RED 证据（evidence/R3/item8a/red-8a-transport.json，真实 query() 调用＋仅替换
腾讯分钟 HTTP 传输）：修复前 ConnectionResetError / HTTP 503 / socket.timeout
三种失败全部返回 `status=empty`、`attempts[0].error_code=EMPTY_UPSTREAM`、
`latency_ms=0`，与「当天确实无数据」完全不可区分。

修复后要求：
- 传输失败 ⇒ `status=error`（不再 empty），原因码区分：
  `TENCENT_TRANSPORT_ERROR` / `HTTP_<status>` / `TIMEOUT`；
- 保留可诊断信息：错误类型、HTTP 状态、耗时（`_meta.attempt_diagnostics`）；
- 合法空（当日确实无行）仍是 `status=empty` + `EMPTY_UPSTREAM`，不进诊断。
"""

import json
import socket
import tempfile
import unittest
import urllib.error
import urllib.request as urllib_request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from ym_stock_data import api, query
from ym_stock_data.provider_policy import CompiledPolicy
from ym_stock_data.provider_state import ProviderState

NOW = datetime(2026, 10, 26, 14, 30, tzinfo=timezone(timedelta(hours=8)))
MINUTE_URL = "web.ifzq.gtimg.cn/appstock/app/day/query"
REAL_URLOPEN = urllib_request.urlopen

LEGAL_EMPTY_PAYLOAD = {
    "code": 0,
    "data": {"sh688018": {"data": [
        {"date": "20261023", "data": ["1500 154.36 17472695 2551454100.00"]},
    ]}},
}


def _response(payload):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps(payload if payload is not None else LEGAL_EMPTY_PAYLOAD).encode()
    return response


class TencentMinuteTransportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = ProviderState(Path(temp.name) / "state.sqlite3")
        for target, value in [
            ("_STATE", state),
            ("load_compiled_policy", lambda: CompiledPolicy(None, "inactive", None)),
            ("_now_shanghai", lambda: NOW),
        ]:
            patched = patch.object(api, target, value)
            patched.start()
            self.addCleanup(patched.stop)

    def _query_with_transport(self, transport):
        def fake_urlopen(request, *args, **kwargs):
            url = getattr(request, "full_url", str(request))
            if MINUTE_URL in url:
                return transport()
            return REAL_URLOPEN(request, *args, **kwargs)

        with patch("ym_stock_data.sources.pytdx.urllib.request.urlopen", side_effect=fake_urlopen):
            return query("stock_kline", code="688018", period="5m",
                         start_date="20261026", end_date="20261026", count=20)

    def _diagnostics(self, result):
        return result["_meta"].get("attempt_diagnostics") or []

    def test_connection_error_is_not_reported_as_legal_empty(self):
        def _reset():
            raise ConnectionResetError("reset by peer")

        result = self._query_with_transport(_reset)
        meta = result["_meta"]
        self.assertEqual("error", meta["status"])
        self.assertIsNone(meta["provider_used"])
        attempt = meta["attempts"][0]
        self.assertEqual("tencent", attempt["provider"])
        self.assertIn(attempt["error_code"],
                      {"TENCENT_TRANSPORT_ERROR", "NETWORK_ERROR"})
        self.assertNotEqual("EMPTY_UPSTREAM", attempt["error_code"])
        diagnostics = self._diagnostics(result)
        self.assertEqual(1, len(diagnostics))
        self.assertEqual("ConnectionResetError", diagnostics[0]["error_type"])
        self.assertIsNone(diagnostics[0]["http_status"])
        self.assertIsInstance(diagnostics[0]["elapsed_ms"], int)
        self.assertIn(attempt["error_code"], meta["quality"]["reason_codes"])

    def test_http_status_is_diagnosable(self):
        def _http_503():
            raise urllib.error.HTTPError("https://web.ifzq.gtimg.cn/", 503,
                                         "Service Unavailable", None, None)

        result = self._query_with_transport(_http_503)
        meta = result["_meta"]
        self.assertEqual("error", meta["status"])
        self.assertEqual("HTTP_503", meta["attempts"][0]["error_code"])
        diagnostics = self._diagnostics(result)
        self.assertEqual(1, len(diagnostics))
        self.assertEqual("HTTPError", diagnostics[0]["error_type"])
        self.assertEqual(503, diagnostics[0]["http_status"])

    def test_timeout_is_a_distinct_failure(self):
        def _timeout():
            raise socket.timeout("timed out")

        result = self._query_with_transport(_timeout)
        meta = result["_meta"]
        self.assertEqual("error", meta["status"])
        self.assertEqual("TIMEOUT", meta["attempts"][0]["error_code"])
        diagnostics = self._diagnostics(result)
        self.assertEqual(1, len(diagnostics))
        # Python 3.10+ 的 socket.timeout 是 TimeoutError 别名，两者都接受。
        self.assertIn(diagnostics[0]["error_type"], {"timeout", "TimeoutError"})

    def test_legal_empty_stays_empty_upstream_without_transport_diagnostics(self):
        result = self._query_with_transport(lambda: _response(LEGAL_EMPTY_PAYLOAD))
        meta = result["_meta"]
        self.assertEqual("empty", meta["status"])
        self.assertEqual("tencent", meta["provider_used"])
        self.assertEqual("EMPTY_UPSTREAM", meta["attempts"][0]["error_code"])
        self.assertEqual([], self._diagnostics(result))
        self.assertIsNone(meta["data_as_of"])
        self.assertEqual([], (result.get("data") or {}).get("bars") or [])

    def test_transport_failure_does_not_fall_back_to_older_days(self):
        def _reset():
            raise ConnectionResetError("reset by peer")

        result = self._query_with_transport(_reset)
        self.assertEqual([], (result.get("data") or {}).get("bars") or [])
        self.assertIsNone(result["_meta"]["data_as_of"])


if __name__ == "__main__":
    unittest.main()
