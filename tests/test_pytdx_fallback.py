"""PyTDX quote fallback tests."""
import json
import unittest
from unittest.mock import patch

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


class PytdxFallbackQuotesTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
