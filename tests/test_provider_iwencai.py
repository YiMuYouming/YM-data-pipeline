import importlib.util as importlib_util
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock

from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers import iwencai as iwencai_provider
from ym_stock_data.providers.iwencai import (
    IWenCaiOpenAPIProvider,
    PyWenCaiProvider,
    PyWenCaiRuntime,
)


class IWenCaiProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_path = Path(self.temp_dir.name) / "providers.sqlite3"

    def test_openapi_401_is_auth_error_and_opens_shared_breaker(self):
        failure = urllib.error.HTTPError(
            "https://openapi.iwencai.com/v1/query2data",
            401,
            "unauthorized",
            hdrs=None,
            fp=None,
        )
        self.addCleanup(failure.close)
        transport = Mock(side_effect=failure)
        first_state = ProviderState(self.state_path)
        provider = IWenCaiOpenAPIProvider(state=first_state, transport=transport)

        outcome = provider.call(
            "review_sentiment",
            {"query": "今日涨停 非ST", "limit": 20},
        )

        self.assertEqual("auth_error", outcome.status)
        self.assertEqual("HTTP_401", outcome.error_code)
        breaker = ProviderState(self.state_path).active_breaker("iwencai_openapi")
        self.assertEqual("HTTP_401", breaker["error_code"])
        self.assertGreaterEqual(breaker["expires_at"] - breaker["opened_at"], 300)

    def test_active_breaker_avoids_another_http_call(self):
        state = ProviderState(self.state_path)
        state.record_failure(
            provider="iwencai_openapi",
            failure_type="auth_error",
            error_code="HTTP_401",
            breaker_seconds=300,
        )
        transport = Mock(side_effect=AssertionError("HTTP must not be called"))
        provider = IWenCaiOpenAPIProvider(state=state, transport=transport)

        outcome = provider.call(
            "review_sentiment",
            {"query": "今日涨停 非ST", "limit": 20},
        )

        self.assertEqual("breaker_open", outcome.status)
        self.assertEqual("HTTP_401", outcome.error_code)
        transport.assert_not_called()

    def test_missing_pywencai_is_dependency_missing(self):
        provider = PyWenCaiProvider(runtime_resolver=lambda: None)

        outcome = provider.call(
            "review_sentiment",
            {"query": "今日涨停 非ST", "limit": 20},
        )

        self.assertEqual("dependency_missing", outcome.status)
        self.assertEqual("PYWENCAI_RUNTIME_MISSING", outcome.error_code)
        self.assertEqual("ym-data setup pywencai", outcome.detail)

    def test_discover_rejects_current_runtime_when_import_probe_fails(self):
        with unittest.mock.patch.dict(
            os.environ,
            {"YM_PYWENCAI_PYTHON": ""},
            clear=False,
        ), unittest.mock.patch.object(
            iwencai_provider,
            "_is_executable",
            return_value=False,
        ), unittest.mock.patch.object(
            importlib_util,
            "find_spec",
            return_value=object(),
        ), unittest.mock.patch.object(
            iwencai_provider.importlib,
            "import_module",
            side_effect=ImportError("binary initialization failed"),
        ) as import_module:
            runtime = iwencai_provider.discover_pywencai_runtime()

        self.assertIsNone(runtime)
        import_module.assert_called_once_with("pywencai")

    def test_pywencai_nonetype_get_is_sanitized_provider_error(self):
        def fail_runner(_python, _query, _limit):
            raise AttributeError("'NoneType' object has no attribute 'get'")

        provider = PyWenCaiProvider(
            runtime_resolver=lambda: PyWenCaiRuntime(
                python=Path("/managed/bin/python"),
                source="managed",
            ),
            runner=fail_runner,
        )

        outcome = provider.call(
            "review_sentiment",
            {"query": "今日涨停 非ST", "limit": 20},
        )

        self.assertEqual("provider_error", outcome.status)
        self.assertEqual("AttributeError", outcome.error_code)
        self.assertNotIn("NoneType", repr(outcome))
        self.assertNotIn("今日涨停", repr(outcome))

    def test_pywencai_error_payload_detail_is_propagated_for_diagnosis(self):
        provider = PyWenCaiProvider(
            runtime_resolver=lambda: PyWenCaiRuntime(
                python=Path("/managed/bin/python"),
                source="managed",
            ),
            runner=lambda _python, _query, _limit: {
                "error": "pywencai execution failed",
                "error_type": "AttributeError",
                "detail": (
                    "Traceback (most recent call last):\n"
                    "  File wencai.py line 185, in get\n"
                    "    data = params.get('data')\n"
                ),
                "_source": "pywencai",
            },
        )

        outcome = provider.call(
            "review_sentiment",
            {"query": "今日涨停 非ST", "limit": 20},
        )

        self.assertEqual("provider_error", outcome.status)
        self.assertEqual("AttributeError", outcome.error_code)
        self.assertIsNotNone(outcome.detail)
        self.assertIn("wencai.py line 185", outcome.detail)

    def test_pywencai_runner_patches_referer_header_for_wencai_antiscrape(self):
        from ym_stock_data.sources import iwencai as source_iwencai

        runner_source = source_iwencai._PYWENCAI_RUNNER

        self.assertIn("Referer", runner_source)
        self.assertIn("www.iwencai.com", runner_source)
        self.assertIn("ph.headers", runner_source)
        self.assertIn("pw.wencai.headers", runner_source)

    def test_pywencai_success_limits_rows_and_names_actual_provider(self):
        rows = [{"股票代码": str(code)} for code in range(5)]
        provider = PyWenCaiProvider(
            runtime_resolver=lambda: PyWenCaiRuntime(
                python=Path("/managed/bin/python"),
                source="managed",
            ),
            runner=lambda _python, _query, _limit: {
                "datas": rows,
                "row_count": len(rows),
            },
        )

        outcome = provider.call(
            "review_sentiment",
            {"query": "今日涨停 非ST", "limit": 2},
        )

        self.assertEqual("success", outcome.status)
        self.assertEqual("pywencai", outcome.provider)
        self.assertEqual(2, outcome.data["row_count"])
        self.assertEqual(2, len(outcome.data["datas"]))

    def test_provider_code_has_no_workbuddy_runtime_discovery(self):
        root = Path(__file__).resolve().parents[1]
        contents = "\n".join(
            (root / relative).read_text(encoding="utf-8")
            for relative in (
                "ym_stock_data/providers/iwencai.py",
                "ym_stock_data/sources/iwencai.py",
                "ym_stock_data/config.py",
            )
        )
        self.assertNotIn("WorkBuddy/Tools/data-venv", contents)
        self.assertNotIn(".workbuddy/binaries", contents)


if __name__ == "__main__":
    unittest.main()
