import unittest
from unittest.mock import Mock, patch

from ym_stock_data.sources import news, research


class NewsContractTests(unittest.TestCase):
    @patch("ym_stock_data.sources.news.requests.get")
    def test_fetch_news_uses_signed_v1_endpoint_and_keeps_shape(self, get):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "errno": 0,
            "data": {
                "roll_data": [
                    {
                        "id": 7,
                        "ctime": 1783990800,
                        "title": "测试标题",
                        "content": "测试正文",
                    }
                ]
            },
        }
        get.return_value = response

        result = news.fetch_news(limit=1)

        url = get.call_args.args[0]
        self.assertIn("/v1/roll/get_roll_list?", url)
        self.assertIn("sign=", url)
        self.assertEqual(1, result["total"])
        self.assertEqual("测试标题", result["items"][0]["title"])
        self.assertEqual("cls_telegraph", result["source"])


class ResearchContractTests(unittest.TestCase):
    @patch("ym_stock_data.sources.research.requests.get")
    def test_fetch_reports_sends_code_to_server(self, get):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "data": [
                {
                    "stockCode": "600519",
                    "title": "公司研报",
                    "publishDate": "2026-07-14",
                    "orgSName": "机构",
                    "emRatingName": "增持",
                    "predictThisYearEps": 10,
                    "predictNextYearEps": 11,
                    "predictNextTwoYearEps": 12,
                    "infoCode": "ABC",
                }
            ],
            "TotalPage": 1,
        }
        get.return_value = response

        result = research.fetch_reports("600519", days=90, max_pages=1)

        self.assertEqual("600519", get.call_args.kwargs["params"].get("code"))
        self.assertEqual(1, result["total"])
        self.assertEqual("eastmoney_reportapi", result["source"])


if __name__ == "__main__":
    unittest.main()
