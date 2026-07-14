import unittest
from unittest.mock import Mock, patch

from ym_stock_data.sources import filings


def _json_response(payload, status_code=200):
    response = Mock(status_code=status_code)
    response.json.return_value = payload
    return response


class FilingsOrgIdTests(unittest.TestCase):
    def setUp(self):
        filings._ORG_ID_CACHE.clear()

    @patch(
        "ym_stock_data.sources.filings.requests.get",
        side_effect=AssertionError("orgId lookup must use POST"),
    )
    @patch("ym_stock_data.sources.filings.requests.post")
    def test_resolver_selects_exact_code_and_caches_success(self, post, get):
        post.return_value = _json_response(
            [
                {"code": "600000", "orgId": "gssh0600000"},
                {"code": "600519", "orgId": "gssh0600519"},
            ]
        )

        first = filings._resolve_org_id("600519")
        second = filings._resolve_org_id("600519")

        self.assertEqual("gssh0600519", first)
        self.assertEqual(first, second)
        post.assert_called_once()

    @patch(
        "ym_stock_data.sources.filings.requests.get",
        side_effect=AssertionError("orgId lookup must use POST"),
    )
    @patch("ym_stock_data.sources.filings.requests.post")
    def test_failed_resolution_is_not_cached_or_reused(self, post, get):
        post.side_effect = [
            _json_response([]),
            _json_response([{"code": "600519", "orgId": "correct"}]),
        ]

        with self.assertRaises(LookupError):
            filings._resolve_org_id("600519")
        resolved = filings._resolve_org_id("600519")

        self.assertEqual("correct", resolved)
        self.assertEqual(2, post.call_count)

    @patch("ym_stock_data.sources.filings.requests.post")
    @patch(
        "ym_stock_data.sources.filings._resolve_org_id",
        return_value="gssh0600519",
    )
    def test_fetch_filings_posts_dynamic_org_id(self, resolve, post):
        post.return_value = _json_response(
            {
                "announcements": [
                    {
                        "announcementTime": 1783958400000,
                        "announcementTitle": "测试公告",
                        "secCode": "600519",
                    }
                ],
                "hasMore": False,
            }
        )

        result = filings.fetch_filings("600519", max_pages=1)

        form = post.call_args.kwargs["data"]
        self.assertEqual("600519,gssh0600519", form["stock"])
        self.assertEqual("", form["searchkey"])
        self.assertEqual(1, result["total"])

    @patch("ym_stock_data.sources.filings.requests.post")
    @patch(
        "ym_stock_data.sources.filings._resolve_org_id",
        return_value="gssh0600519",
    )
    def test_empty_result_is_explicit_success(self, resolve, post):
        post.return_value = _json_response(
            {"announcements": [], "hasMore": False}
        )

        result = filings.fetch_filings("600519", max_pages=1)

        self.assertEqual(0, result["total"])
        self.assertEqual([], result["filings"])
        self.assertNotIn("error", result)

    @patch("ym_stock_data.sources.filings._resolve_org_id")
    def test_org_id_failure_is_explicit(self, resolve):
        resolve.side_effect = LookupError("orgId not found")

        result = filings.fetch_filings("600519")

        self.assertEqual("orgId not found", result["error"])
        self.assertEqual("orgid_unresolved", result["error_type"])
        self.assertEqual("cninfo", result["source"])

    @patch("ym_stock_data.sources.filings.requests.post")
    @patch(
        "ym_stock_data.sources.filings._resolve_org_id",
        return_value="gssh0600519",
    )
    def test_post_timeout_is_explicit(self, resolve, post):
        post.side_effect = TimeoutError("timed out")

        result = filings.fetch_filings("600519", max_pages=1)

        self.assertEqual("timed out", result["error"])
        self.assertEqual("TimeoutError", result["error_type"])


if __name__ == "__main__":
    unittest.main()
