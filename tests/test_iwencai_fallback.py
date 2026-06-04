"""问财降级熔断测试。"""

import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ym_stock_data.sources import iwencai


class IwencaiFallbackTests(unittest.TestCase):
    def setUp(self):
        iwencai._API_KEY = "dummy"
        iwencai._OPENAPI_DOWN_AT = 0
        iwencai._PYWENCAI_DOWN_AT = 0

    def tearDown(self):
        iwencai._OPENAPI_DOWN_AT = 0
        iwencai._PYWENCAI_DOWN_AT = 0

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


if __name__ == "__main__":
    unittest.main()
