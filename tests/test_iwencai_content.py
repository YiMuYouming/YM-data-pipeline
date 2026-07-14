import unittest
from unittest.mock import Mock, patch

from ym_stock_data import fetch
from ym_stock_data.sources import iwencai_content


class IwencaiContentTests(unittest.TestCase):
    @patch.dict("os.environ", {"IWENCAI_API_KEY": "token"}, clear=False)
    @patch("ym_stock_data.sources.iwencai_content.requests.post")
    def test_search_content_uses_report_search_contract(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {
            "status_code": 0,
            "data": [{"uid": "1", "title": "机器人"}],
        }
        post.return_value = response

        result = iwencai_content.search_content(
            "机器人", channel="report", limit=10
        )

        headers = post.call_args.kwargs["headers"]
        payload = post.call_args.kwargs["json"]
        self.assertEqual("Bearer token", headers["Authorization"])
        self.assertEqual("report-search", headers["X-Claw-Skill-Id"])
        self.assertEqual(["report"], payload["channels"])
        self.assertEqual("iwencai_content", result["source"])

    @patch.dict("os.environ", {"IWENCAI_API_KEY": "token"}, clear=False)
    @patch("ym_stock_data.sources.iwencai_content.requests.post")
    def test_duplicate_uid_keeps_highest_score(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {
            "status_code": 0,
            "data": [
                {"uid": "1", "score": 1, "publish_date": "2026-07-14"},
                {"uid": "1", "score": 2, "publish_date": "2026-07-13"},
            ],
        }
        post.return_value = response

        result = iwencai_content.search_content("机器人")

        self.assertEqual(1, result["total"])
        self.assertEqual(2, result["items"][0]["score"])

    @patch.dict("os.environ", {"IWENCAI_API_KEY": "token"}, clear=False)
    @patch("ym_stock_data.sources.iwencai_content.requests.post")
    def test_empty_result_is_explicit_success(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {"status_code": 0, "data": []}
        post.return_value = response

        result = iwencai_content.search_content("不存在的主题")

        self.assertEqual(0, result["total"])
        self.assertEqual([], result["items"])
        self.assertNotIn("error", result)

    @patch.dict("os.environ", {"IWENCAI_API_KEY": "token"}, clear=False)
    @patch("ym_stock_data.sources.iwencai_content.requests.post")
    def test_timeout_is_exposed_with_source(self, post):
        post.side_effect = TimeoutError("timed out")

        result = iwencai_content.search_content("机器人")

        self.assertEqual("timed out", result["error"])
        self.assertEqual("TimeoutError", result["error_type"])
        self.assertEqual("iwencai_content", result["source"])

    def test_search_content_rejects_unknown_channel(self):
        with self.assertRaises(ValueError):
            iwencai_content.search_content("机器人", channel="social")

    @patch.dict("os.environ", {"IWENCAI_API_KEY": "token"}, clear=False)
    @patch("ym_stock_data.sources.iwencai_content.requests.post")
    def test_v1_route_adds_meta(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {"status_code": 0, "data": []}
        post.return_value = response

        result = fetch("iwencai_content", query="机器人", channel="news")

        self.assertEqual("iwencai_content", result["_meta"]["data_type"])


if __name__ == "__main__":
    unittest.main()
