"""PyTDX quote fallback tests."""
import unittest
from unittest.mock import patch

from ym_stock_data.sources import pytdx


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


if __name__ == "__main__":
    unittest.main()
