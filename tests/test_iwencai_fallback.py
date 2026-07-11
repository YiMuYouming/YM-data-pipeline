"""问财降级熔断测试。"""

import os
import sys
import time
import unittest
import urllib.error
import http.client
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ym_stock_data.sources import iwencai


IWENCAI_TEST_STATE = {
    "_API_KEY": "dummy",
    "_OPENAPI_DOWN_AT": 0,
    "_PYWENCAI_DOWN_AT": 0,
    "_OPENAPI_BREAKER_AT": 0,
    "_OPENAPI_BREAKER_SECONDS": 300,
    "_OPENAPI_FAILURE_TYPE": "rate_limit",
    "_OPENAPI_LAST_ERROR": None,
    "_PYWENCAI_LAST_ERROR": None,
}


class JsonResponse:
    def __init__(self, payload):
        import json

        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


class RawResponse(JsonResponse):
    def __init__(self, payload):
        self.payload = payload


def reset_breaker_state():
    for name, value in IWENCAI_TEST_STATE.items():
        if name != "_API_KEY":
            setattr(iwencai, name, value)


class IwencaiFallbackTests(unittest.TestCase):
    def fallback_meta(self, result):
        self.assertIn("_meta", result)
        return result["_meta"]

    def setUp(self):
        self.iwencai_state = patch.multiple(iwencai, **IWENCAI_TEST_STATE)
        self.iwencai_state.start()
        self.addCleanup(self.iwencai_state.stop)

    def test_pywencai_failure_is_cached_when_openapi_already_down(self):
        iwencai._OPENAPI_DOWN_AT = time.time()
        calls = 0

        def fake_pywencai(query_str, limit=50):
            nonlocal calls
            calls += 1
            return {"error": "anti bot", "query": query_str, "_source": "pywencai"}

        with patch.object(iwencai, "_pywencai_query", side_effect=fake_pywencai):
            first = iwencai.query("昨日涨停 今日涨跌幅 非st")
            second = iwencai.query("今日连板 股票简称 连板数 非st")

        self.assertEqual(calls, 1)
        self.assertTrue(iwencai._PYWENCAI_DOWN_AT)
        self.assertEqual(first["error_type"], "all_paths_dead")
        self.assertEqual(first["_source"], "none")
        self.assertEqual(first["pywencai"], "anti bot")
        self.assertEqual(second["error_type"], "all_paths_dead")
        self.assertEqual(second["_source"], "none")

    def test_http_500_falls_back_for_60_seconds_and_limits_rows(self):
        failure = urllib.error.HTTPError(
            iwencai.IWENCAI_BASE,
            500,
            "server error",
            hdrs=None,
            fp=None,
        )
        self.addCleanup(failure.close)
        fallback = {
            "datas": [{"股票代码": "600000"}, {"股票代码": "600001"}, {"股票代码": "600002"}],
            "row_count": 3,
            "_source": "pywencai",
        }

        with patch("time.time", return_value=1_000.0), \
             patch.object(iwencai.urllib.request, "urlopen", side_effect=failure), \
             patch.object(iwencai, "_pywencai_query", return_value=fallback) as pywencai_query:
            result = iwencai.query("银行股", limit=2)

        pywencai_query.assert_called_once_with("银行股", 2)
        self.assertEqual("pywencai", result.get("_source"))
        self.assertEqual(2, result["row_count"])
        self.assertEqual(2, len(result["datas"]))
        meta = self.fallback_meta(result)
        self.assertEqual("openapi", meta["fallback_from"])
        self.assertEqual("pywencai", meta["fallback_to"])
        self.assertEqual("http_5xx", meta["failure_type"])
        self.assertIn("provider", meta)
        self.assertEqual("pywencai", meta["provider"])
        datetime.fromisoformat(meta["query_time"])
        self.assertEqual("http_5xx", meta["fallback_reason"])
        self.assertEqual({
            "requested_count": 2,
            "returned_count": 2,
            "ratio": 1.0,
        }, meta["coverage"])

        openapi_result = {"datas": [], "row_count": 0}
        with patch("time.time", return_value=1_061.0), \
             patch.object(iwencai.urllib.request, "urlopen", return_value=JsonResponse(openapi_result)) as urlopen, \
             patch.object(iwencai, "_pywencai_query", side_effect=AssertionError("60s breaker must expire")):
            recovered = iwencai.query("银行股", limit=2)

        urlopen.assert_called_once()
        self.assertEqual("openapi", recovered["_source"])

    def test_direct_timeout_falls_back_to_pywencai(self):
        fallback = {"datas": [{"股票代码": "600000"}], "row_count": 1, "_source": "pywencai"}

        with patch.object(iwencai.urllib.request, "urlopen", side_effect=TimeoutError("timed out")), \
             patch.object(iwencai, "_pywencai_query", return_value=fallback):
            result = iwencai.query("银行股", limit=1)

        self.assertEqual("pywencai", result.get("_source"))
        meta = self.fallback_meta(result)
        self.assertEqual("timeout", meta["failure_type"])
        self.assertEqual("openapi", meta["fallback_from"])
        self.assertEqual("pywencai", meta["fallback_to"])

    def test_url_error_falls_back_to_pywencai(self):
        fallback = {"datas": [{"股票代码": "600000"}], "row_count": 1, "_source": "pywencai"}

        with patch.object(
            iwencai.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("connection reset"),
        ), patch.object(iwencai, "_pywencai_query", return_value=fallback):
            result = iwencai.query("银行股", limit=1)

        self.assertEqual("pywencai", result.get("_source"))
        self.assertEqual("network", self.fallback_meta(result)["failure_type"])

    def test_rate_limit_breaker_remains_300_seconds(self):
        failure = urllib.error.HTTPError(
            iwencai.IWENCAI_BASE,
            429,
            "too many requests",
            hdrs=None,
            fp=None,
        )
        self.addCleanup(failure.close)
        fallback = {"datas": [{"股票代码": "600000"}], "row_count": 1, "_source": "pywencai"}

        with patch("time.time", return_value=1_000.0), \
             patch.object(iwencai.urllib.request, "urlopen", side_effect=failure), \
             patch.object(iwencai, "_pywencai_query", return_value=fallback):
            first = iwencai.query("银行股", limit=1)

        self.assertEqual("rate_limit", self.fallback_meta(first)["failure_type"])

        with patch("time.time", return_value=1_061.0), \
             patch.object(iwencai.urllib.request, "urlopen") as urlopen, \
             patch.object(iwencai, "_pywencai_query", return_value=fallback):
            second = iwencai.query("银行股", limit=1)

        urlopen.assert_not_called()
        self.assertEqual("pywencai", second["_source"])
        self.assertEqual("rate_limit", self.fallback_meta(second)["failure_type"])

    def test_failed_server_fallback_reports_all_paths_dead_without_quota_claim(self):
        failure = urllib.error.HTTPError(
            iwencai.IWENCAI_BASE,
            503,
            "unavailable",
            hdrs=None,
            fp=None,
        )
        self.addCleanup(failure.close)
        fallback = {"error": "anti bot", "query": "银行股", "_source": "pywencai"}

        with patch.object(iwencai.urllib.request, "urlopen", side_effect=failure), \
             patch.object(iwencai, "_pywencai_query", return_value=fallback):
            result = iwencai.query("银行股", limit=1)

        self.assertEqual("all_paths_dead", result.get("error_type"))
        self.assertEqual("none", result.get("_source"))
        self.assertNotIn("额度", result["error"])
        meta = self.fallback_meta(result)
        self.assertEqual("http_5xx", meta["failure_type"])
        self.assertEqual("openapi", meta["fallback_from"])
        self.assertEqual("pywencai", meta["fallback_to"])

    def test_fresh_503_honors_recent_pywencai_failure_cache(self):
        first_failure = urllib.error.HTTPError(
            iwencai.IWENCAI_BASE,
            503,
            "unavailable",
            hdrs=None,
            fp=None,
        )
        second_failure = urllib.error.HTTPError(
            iwencai.IWENCAI_BASE,
            503,
            "unavailable again",
            hdrs=None,
            fp=None,
        )
        self.addCleanup(first_failure.close)
        self.addCleanup(second_failure.close)
        fallback_failure = {"error": "anti bot", "query": "银行股", "_source": "pywencai"}

        with patch("time.time", return_value=1_000.0), \
             patch.object(iwencai.urllib.request, "urlopen", side_effect=first_failure), \
             patch.object(iwencai, "_pywencai_query", return_value=fallback_failure):
            first = iwencai.query("银行股", limit=1)
        self.assertEqual("all_paths_dead", first["error_type"])

        with patch("time.time", return_value=1_061.0), \
             patch.object(
                 iwencai.urllib.request,
                 "urlopen",
                 return_value=JsonResponse({"datas": [], "row_count": 0}),
             ):
            recovered = iwencai.query("银行股", limit=1)
        self.assertEqual("openapi", recovered["_source"])

        with patch("time.time", return_value=1_062.0), \
             patch.object(iwencai.urllib.request, "urlopen", side_effect=second_failure), \
             patch.object(iwencai, "_pywencai_query", return_value={"datas": [], "row_count": 0}) as pywencai_query:
            second = iwencai.query("银行股", limit=1)

        pywencai_query.assert_not_called()
        self.assertEqual("all_paths_dead", second["error_type"])
        self.assertEqual("anti bot", second["pywencai"])
        self.assertEqual("http_5xx", second["_meta"]["failure_type"])

    def test_incomplete_read_falls_back_as_network_failure(self):
        response = RawResponse(b"")
        response.read = lambda: (_ for _ in ()).throw(http.client.IncompleteRead(b"partial", 10))
        fallback = {"datas": [{"股票代码": "600000"}], "row_count": 1, "_source": "pywencai"}

        with patch.object(iwencai.urllib.request, "urlopen", return_value=response), \
             patch.object(iwencai, "_pywencai_query", return_value=fallback):
            result = iwencai.query("银行股", limit=1)

        self.assertEqual("pywencai", result.get("_source"))
        self.assertEqual("network", self.fallback_meta(result)["failure_type"])

    def test_malformed_openapi_response_falls_back_as_invalid_response(self):
        fallback = {"datas": [{"股票代码": "600000"}], "row_count": 1, "_source": "pywencai"}

        with patch.object(iwencai.urllib.request, "urlopen", return_value=RawResponse(b"not-json")), \
             patch.object(iwencai, "_pywencai_query", return_value=fallback):
            result = iwencai.query("银行股", limit=1)

        self.assertEqual("pywencai", result.get("_source"))
        self.assertEqual("invalid_response", self.fallback_meta(result)["failure_type"])

    def test_valid_json_with_invalid_top_level_shape_falls_back(self):
        fallback = {"datas": [{"股票代码": "600000"}], "row_count": 1, "_source": "pywencai"}

        for body in (b"[]", b"null", b"{}"):
            with self.subTest(body=body):
                reset_breaker_state()
                with patch.object(iwencai.urllib.request, "urlopen", return_value=RawResponse(body)), \
                     patch.object(iwencai, "_pywencai_query", return_value=fallback) as pywencai_query:
                    result = iwencai.query("银行股", limit=2)

                pywencai_query.assert_called_once_with("银行股", 2)
                self.assertEqual("pywencai", result.get("_source"))
                meta = self.fallback_meta(result)
                self.assertEqual("invalid_response", meta["failure_type"])
                self.assertEqual("invalid_response", meta["fallback_reason"])
                self.assertEqual(60, iwencai._OPENAPI_BREAKER_SECONDS)

    def test_message_only_openapi_error_object_falls_back(self):
        fallback = {"datas": [{"股票代码": "600000"}], "row_count": 1, "_source": "pywencai"}
        body = b'{"message":"temporary upstream failure"}'

        with patch.object(iwencai.urllib.request, "urlopen", return_value=RawResponse(body)), \
             patch.object(iwencai, "_pywencai_query", return_value=fallback) as pywencai_query:
            result = iwencai.query("银行股", limit=2)

        pywencai_query.assert_called_once_with("银行股", 2)
        self.assertEqual("pywencai", result.get("_source"))
        meta = self.fallback_meta(result)
        self.assertEqual("invalid_response", meta["failure_type"])
        self.assertEqual("invalid_response", meta["fallback_reason"])


if __name__ == "__main__":
    unittest.main()
