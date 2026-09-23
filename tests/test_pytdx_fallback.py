"""PyTDX direct-provider contract tests."""
import threading
import unittest
from unittest.mock import Mock, patch

from ym_stock_data.sources import pytdx


class EmptyApi:
    def get_security_quotes(self, _codes):
        return []

    def get_security_bars(self, *_args):
        return []


class PytdxDirectProviderTests(unittest.TestCase):
    def setUp(self):
        pytdx._api = None
        pytdx._connected_at = 0
        pytdx._all_servers_down_at = 0
        pytdx._all_codes_cache = None

    def tearDown(self):
        pytdx.disconnect()
        pytdx._all_servers_down_at = 0

    def test_get_api_caches_all_servers_down_for_short_cooldown(self):
        dead = Mock()
        dead.connect.return_value = True
        dead.get_security_quotes.return_value = []

        constructor = Mock(side_effect=[dead, dead])
        with patch.object(pytdx, "_load_tdx_hq_api", return_value=constructor), \
             patch.object(pytdx, "PYTDX_SERVERS", [("dead", 7709)]):
            self.assertIsNone(pytdx._get_api())
            self.assertIsNone(pytdx._get_api())

        self.assertEqual(constructor.call_count, 1)
        dead.disconnect.assert_called_once()

    def test_get_api_skips_connected_server_without_business_data(self):
        dead = Mock()
        dead.connect.return_value = True
        dead.get_security_quotes.return_value = []
        healthy = Mock()
        healthy.connect.return_value = True
        healthy.get_security_quotes.return_value = [{"code": "600000", "price": 10.0}]

        pytdx._api = None
        pytdx._connected_at = 0
        constructor = Mock(side_effect=[dead, healthy])
        with patch.object(pytdx, "_load_tdx_hq_api", return_value=constructor), \
             patch.object(pytdx, "PYTDX_SERVERS", [("dead", 7709), ("healthy", 7709)]):
            result = pytdx._get_api()

        self.assertIs(result, healthy)
        dead.disconnect.assert_called_once()

    def test_disconnect_waits_for_inflight_pytdx_read(self):
        started = threading.Event()
        release = threading.Event()
        disconnected = threading.Event()

        class BlockingApi:
            def get_security_quotes(self, _codes):
                started.set()
                release.wait(timeout=1)
                return [{"code": "000001", "price": 3913.79, "last_close": 3996.0}]

            def get_index_bars(self, *_args):
                return []

            def disconnect(self):
                disconnected.set()

        api = BlockingApi()
        pytdx._api = api
        pytdx._connected_at = 1
        with patch.object(pytdx, "_get_api", return_value=api):
            reader = threading.Thread(target=pytdx.fetch_index)
            reader.start()
            self.assertTrue(started.wait(timeout=1))
            closer = threading.Thread(target=pytdx.disconnect)
            closer.start()
            self.assertFalse(disconnected.wait(timeout=0.05))
            release.set()
            reader.join(timeout=1)
            closer.join(timeout=1)

        self.assertTrue(disconnected.is_set())

    def test_fetch_quotes_reports_direct_unavailable_when_pytdx_package_is_missing(self):
        with patch.object(pytdx, "_load_tdx_hq_api", side_effect=ImportError("pytdx missing")), \
             patch("ym_stock_data.sources.tencent.fetch_quotes") as fetch_tencent:
            result = pytdx.fetch_quotes(["002436"])

        self.assertEqual("PYTDX_DIRECT_UNAVAILABLE", result["error_type"])
        fetch_tencent.assert_not_called()

    def test_fetch_quotes_reports_direct_unavailable_when_disabled_without_fallback(self):
        with patch.dict("os.environ", {"YIMU_DISABLE_PYTDX": "1"}), \
             patch.object(pytdx, "_load_tdx_hq_api") as load_api, \
             patch("ym_stock_data.sources.tencent.fetch_quotes") as fetch_tencent:
            result = pytdx.fetch_quotes(["002436"])

        self.assertEqual("PYTDX_DIRECT_UNAVAILABLE", result["error_type"])
        load_api.assert_not_called()
        fetch_tencent.assert_not_called()

    def test_fast_quotes_skip_per_symbol_history_requests(self):
        api = Mock()
        api.get_security_quotes.return_value = [{
            "code": "600519", "price": 1250.0, "last_close": 1240.0,
            "vol": 100, "servertime": "13:10:00",
        }]
        with patch.object(pytdx, "_get_api", return_value=api):
            result = pytdx.fetch_quotes(["600519"], fast=True)
        self.assertEqual(1250.0, result["600519"]["price"])
        api.get_security_bars.assert_not_called()

    def test_fetch_index_reports_direct_unavailable_when_disabled_without_fallback(self):
        with patch.dict("os.environ", {"YIMU_DISABLE_PYTDX": "1"}), \
             patch.object(pytdx, "_fallback_index") as fallback_index, \
             patch.object(pytdx, "_fallback_index_tencent") as fallback_tencent:
            result = pytdx.fetch_index()

        self.assertEqual("PYTDX_DIRECT_UNAVAILABLE", result["error_type"])
        fallback_index.assert_not_called()
        fallback_tencent.assert_not_called()

    def test_fetch_index_reports_direct_unavailable_when_business_quote_is_empty(self):
        with patch.object(pytdx, "_get_api", return_value=EmptyApi()), \
             patch.object(pytdx, "_fallback_index") as fallback_index, \
             patch.object(pytdx, "_fallback_index_tencent") as fallback_tencent:
            result = pytdx.fetch_index()

        self.assertEqual("PYTDX_DIRECT_UNAVAILABLE", result["error_type"])
        fallback_index.assert_not_called()
        fallback_tencent.assert_not_called()

    def test_fetch_daily_kline_reports_direct_unavailable_without_tencent_fallback(self):
        with patch.object(pytdx, "_get_api", return_value=EmptyApi()), \
             patch.object(pytdx, "_fetch_tencent_kline") as fetch_tencent:
            result = pytdx.fetch_kline("603290", period="daily")

        self.assertEqual("PYTDX_DIRECT_UNAVAILABLE", result["error_type"])
        fetch_tencent.assert_not_called()

    def test_fetch_intraday_kline_reports_direct_unavailable_without_sina_fallback(self):
        with patch.object(pytdx, "_get_api", return_value=EmptyApi()), \
             patch.object(pytdx, "_fetch_sina_kline") as fetch_sina:
            result = pytdx.fetch_kline("603290", period="15m")

        self.assertEqual("PYTDX_DIRECT_UNAVAILABLE", result["error_type"])
        fetch_sina.assert_not_called()

    def test_all_share_codes_uses_tdx_directory_instead_of_guessed_ranges(self):
        class DirectoryApi:
            def get_security_count(self, _market):
                return 1000

            def get_security_list(self, market, _start):
                if market == 0:
                    return [
                        {"code": "000001", "name": "平安银行"},
                        {"code": "300001", "name": "特锐德"},
                        {"code": "399001", "name": "深证成指"},
                        {"code": "159001", "name": "货币ETF"},
                    ]
                return [
                    {"code": "600000", "name": "浦发银行"},
                    {"code": "688001", "name": "华兴源创"},
                    {"code": "000001", "name": "上证指数"},
                    {"code": "510050", "name": "50ETF"},
                ]

        result = pytdx._all_share_codes(DirectoryApi())

        self.assertEqual(result, [
            (0, "000001"),
            (0, "300001"),
            (1, "600000"),
            (1, "688001"),
        ])


if __name__ == "__main__":
    unittest.main()
