"""PyTDX quote fallback tests."""
import json
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

from ym_stock_data.sources import pytdx


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class EmptyApi:
    def get_security_quotes(self, _codes):
        return []

    def get_security_bars(self, *_args):
        return []


class PytdxFallbackQuotesTest(unittest.TestCase):
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

        with patch("pytdx.hq.TdxHq_API", side_effect=[dead, dead]) as constructor, \
             patch.object(pytdx, "PYTDX_SERVERS", [("dead", 7709)]):
            self.assertIsNone(pytdx._get_api())
            self.assertIsNone(pytdx._get_api())

        self.assertEqual(constructor.call_count, 1)

    def test_get_api_skips_connected_server_without_business_data(self):
        dead = Mock()
        dead.connect.return_value = True
        dead.get_security_quotes.return_value = []
        healthy = Mock()
        healthy.connect.return_value = True
        healthy.get_security_quotes.return_value = [{"code": "600000", "price": 10.0}]

        pytdx._api = None
        pytdx._connected_at = 0
        with patch("pytdx.hq.TdxHq_API", side_effect=[dead, healthy]), \
             patch.object(pytdx, "PYTDX_SERVERS", [("dead", 7709), ("healthy", 7709)]):
            result = pytdx._get_api()

        self.assertIs(result, healthy)
        dead.disconnect.assert_called_once()

    def test_fallback_quotes_uses_tencent_shape_for_dashboard(self):
        tencent_payload = {
            "002436": {
                "price": 37.02,
                "change_pct": -1.23,
                "turnover_pct": 2.34,
                "vol_ratio": 1.56,
            }
        }

        with patch("ym_stock_data.sources.tencent.fetch_quotes", return_value=tencent_payload):
            result = pytdx._fallback_quotes(["002436"])

        self.assertEqual(result["002436"]["最新价"], 37.02)
        self.assertEqual(result["002436"]["涨幅"], "-1.23%")
        self.assertEqual(result["002436"]["换手"], "2.34")
        self.assertEqual(result["002436"]["量比"], "1.56")
        self.assertEqual(result["002436"]["_source"], "tencent_fallback")

    def test_easyquotation_quote_fallback_marks_sina_provenance(self):
        eq = Mock()
        eq.stocks.return_value = {
            "002436": {"now": 37.02, "涨跌(%)": -1.23, "量比": 1.56, "换手(%)": 2.34}
        }

        fake_module = types.SimpleNamespace(use=Mock(return_value=eq))
        with patch("ym_stock_data.sources.tencent.fetch_quotes", side_effect=OSError("down")), \
             patch.dict(sys.modules, {"easyquotation": fake_module}):
            result = pytdx._fallback_quotes(["002436"])

        self.assertIn("_source", result["002436"])
        self.assertEqual(result["002436"]["_source"], "sina_fallback")

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

    def test_fetch_quotes_falls_back_when_pytdx_package_missing(self):
        tencent_payload = {
            "002436": {
                "price": 37.02,
                "change_pct": -1.23,
                "turnover_pct": 2.34,
                "vol_ratio": 1.56,
            }
        }

        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("pytdx"):
                raise ModuleNotFoundError("No module named 'pytdx'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import), \
             patch("ym_stock_data.sources.tencent.fetch_quotes", return_value=tencent_payload):
            result = pytdx.fetch_quotes(["002436"])

        self.assertEqual(result["002436"]["最新价"], 37.02)

    def test_fetch_quotes_falls_back_when_pytdx_disabled(self):
        tencent_payload = {
            "002436": {
                "price": 37.02,
                "change_pct": -1.23,
                "turnover_pct": 2.34,
                "vol_ratio": 1.56,
            }
        }

        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("pytdx"):
                raise AssertionError("pytdx should not be imported when disabled")
            return real_import(name, *args, **kwargs)

        with patch.dict("os.environ", {"YIMU_DISABLE_PYTDX": "1"}), \
             patch("builtins.__import__", side_effect=fake_import), \
             patch("ym_stock_data.sources.tencent.fetch_quotes", return_value=tencent_payload):
            result = pytdx.fetch_quotes(["002436"])

        self.assertEqual(result["002436"]["最新价"], 37.02)
        self.assertEqual(result["002436"]["_source"], "tencent_fallback")

    def test_fetch_index_falls_back_to_eastmoney_when_pytdx_disabled(self):
        payload = {
            "data": {
                "diff": [
                    {"f12": "000001", "f14": "上证指数", "f2": 4079.39, "f3": -0.35, "f6": 597243329048.4, "f15": 4093.0, "f16": 4070.25, "f18": 4093.73, "f104": 830, "f105": 1450},
                    {"f12": "399001", "f14": "深证成指", "f2": 15575.62, "f3": -1.02, "f6": 695029456727.1, "f15": 15696.68, "f16": 15504.79, "f18": 15736.47, "f104": 1068, "f105": 1772},
                    {"f12": "399006", "f14": "创业板指", "f2": 3998.71, "f3": -1.16, "f6": 318347611231.2, "f15": 4042.74, "f16": 3971.72, "f18": 4045.77, "f104": 449, "f105": 918},
                ]
            }
        }

        with patch.dict("os.environ", {"YIMU_DISABLE_PYTDX": "1"}), \
             patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
            result = pytdx.fetch_index()

        self.assertEqual(result["上证指数"], 4079.39)
        self.assertEqual(result["上证指数涨幅"], "-0.35%")
        self.assertEqual(result["深证指数"], 15575.62)
        self.assertEqual(result["创业指数"], 3998.71)
        self.assertEqual(result["上涨家数"], 1898)
        self.assertEqual(result["下跌家数"], 3222)
        self.assertEqual(result["_source"], "eastmoney_fallback")

    def test_fetch_index_falls_back_when_connected_server_returns_empty(self):
        fallback = {"上证指数": 3913.79, "_source": "eastmoney_fallback"}

        with patch.object(pytdx, "_get_api", return_value=EmptyApi()), \
             patch.object(pytdx, "_fallback_index", return_value=fallback) as fetch_fallback:
            result = pytdx.fetch_index()

        self.assertEqual(result, fallback)
        fetch_fallback.assert_called_once_with()

    def test_index_fallback_uses_tencent_when_eastmoney_is_empty(self):
        lines = []
        for symbol, name, price, pct, amount_wan in [
            ("sh000001", "上证指数", "3913.79", "-2.06", "133487193"),
            ("sz399001", "深证成指", "14522.85", "-3.48", "148288915"),
            ("sz399006", "创业板指", "3723.52", "-3.10", "69297056"),
        ]:
            values = [""] * 88
            values[1] = name
            values[3] = price
            values[32] = pct
            values[33] = price
            values[34] = price
            values[37] = amount_wan
            lines.append(f'v_{symbol}="' + "~".join(values) + '";')

        class TencentResponse:
            def read(self):
                return "".join(lines).encode("gbk")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with patch.object(pytdx, "_eastmoney_json", return_value={}), \
             patch("urllib.request.urlopen", return_value=TencentResponse()):
            result = pytdx._fallback_index()

        self.assertIn("上证指数", result)
        self.assertEqual(result["上证指数"], 3913.79)
        self.assertEqual(result["深证指数"], 14522.85)
        self.assertEqual(result["创业指数"], 3723.52)
        self.assertEqual(result["_source"], "tencent_index_fallback")

    def test_fetch_daily_kline_falls_back_to_tencent_when_pytdx_returns_empty(self):
        payload = {
            "code": 0,
            "data": {
                "sh603290": {
                    "qfqday": [
                        ["2026-07-10", "130.00", "123.80", "131.00", "122.00", "100000"],
                        ["2026-07-13", "120.00", "116.99", "121.00", "115.00", "120000"],
                    ]
                }
            },
        }

        with patch.object(pytdx, "_get_api", return_value=EmptyApi()), \
             patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
            result = pytdx.fetch_kline("603290", period="daily")

        self.assertIn("total_bars", result)
        self.assertEqual(result["total_bars"], 2)
        self.assertEqual(result["bars"][-1]["close"], 116.99)
        self.assertIsNone(result["bars"][-1]["amount"])
        self.assertEqual(result["_source"], "tencent_fallback")
        self.assertEqual(result["_meta"]["fallback_to"], "tencent")

    def test_fetch_intraday_kline_falls_back_to_sina_when_pytdx_returns_empty(self):
        rows = [{
            "day": "2026-07-13 14:45:00",
            "open": "118.00",
            "high": "119.00",
            "low": "116.50",
            "close": "116.99",
            "volume": "120000",
            "amount": "14000000.0",
        }]

        class JsonpResponse(FakeResponse):
            def read(self):
                return ("var _k=(" + json.dumps(rows) + ");").encode("utf-8")

        with patch.object(pytdx, "_get_api", return_value=EmptyApi()), \
             patch("urllib.request.urlopen", return_value=JsonpResponse({})):
            result = pytdx.fetch_kline("603290", period="15m")

        self.assertIn("total_bars", result)
        self.assertEqual(result["total_bars"], 1)
        self.assertEqual(result["bars"][0]["time"], "2026-07-13 14:45:00")
        self.assertEqual(result["_source"], "sina_fallback")
        self.assertEqual(result["_meta"]["fallback_to"], "sina")

    def test_fetch_breadth_falls_back_to_eastmoney_when_pytdx_disabled(self):
        payload = {
            "data": {
                "diff": [
                    {"f12": "000001", "f104": 830, "f105": 1450, "f106": 68},
                    {"f12": "399001", "f104": 1068, "f105": 1772, "f106": 81},
                ]
            }
        }

        with patch.dict("os.environ", {"YIMU_DISABLE_PYTDX": "1"}), \
             patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
            result = pytdx.fetch_breadth()

        self.assertEqual(result["0~3%"], 1898)
        self.assertEqual(result["-0~-3%"], 3222)
        self.assertEqual(result["_flat"], 149)
        self.assertEqual(result["_total"], 5269)
        self.assertEqual(result["_source"], "eastmoney_index_fallback")

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
