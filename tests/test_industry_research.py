import unittest
from unittest.mock import Mock, patch

from ym_stock_data import fetch
from ym_stock_data.sources import research


def _response(items):
    response = Mock()
    response.status_code = 200
    response.json.return_value = {"data": items, "TotalPage": 1}
    return response


class IndustryResearchTests(unittest.TestCase):
    @patch("ym_stock_data.sources.research.CLIENT.get")
    def test_industry_name_resolver_reads_official_filter_codes(self, get):
        response = Mock(status_code=200)
        response.text = (
            '<span class="item" data-bkval="1036">半导体</span>'
        )
        get.return_value = response
        research._resolve_industry_code.cache_clear()

        code = research._resolve_industry_code("半导体")

        self.assertEqual("1036", code)

    @patch("ym_stock_data.sources.research.CLIENT.get")
    @patch(
        "ym_stock_data.sources.research._resolve_industry_code",
        return_value="1036",
    )
    def test_industry_name_query_uses_qtype_one(self, resolve_code, get):
        get.return_value = _response(
            [{"title": "半导体行业研报", "industryName": "半导体"}]
        )

        result = research.fetch_industry_reports(industry="半导体")

        params = get.call_args.kwargs["params"]
        self.assertEqual(1, params["qType"])
        self.assertEqual("1036", params["industryCode"])
        self.assertEqual("*", params["industry"])
        self.assertEqual("", params["code"])
        self.assertEqual(1, result["total"])

    @patch("ym_stock_data.sources.research.CLIENT.get")
    @patch(
        "ym_stock_data.sources.research._resolve_stock_industry_code",
        return_value="1277",
    )
    def test_stock_code_query_uses_qtype_one(self, resolve_code, get):
        get.return_value = _response(
            [{"title": "白酒行业研报", "industryName": "食品饮料"}]
        )

        result = research.fetch_industry_reports(code="600519")

        params = get.call_args.kwargs["params"]
        self.assertEqual(1, params["qType"])
        self.assertEqual("1277", params["industryCode"])
        self.assertEqual("", params["code"])
        self.assertEqual("*", params["industry"])
        self.assertEqual("stock_code", result["query_type"])

    @patch("ym_stock_data.sources.research.CLIENT.get")
    def test_stock_code_resolver_uses_individual_report_industry(self, get):
        get.return_value = _response(
            [{"stockCode": "600519", "indvInduCode": "1277"}]
        )

        code = research._resolve_stock_industry_code(
            "600519", "2025-07-14", "2026-07-14"
        )

        self.assertEqual("1277", code)
        self.assertEqual(0, get.call_args.kwargs["params"]["qType"])

    @patch("ym_stock_data.sources.research.CLIENT.get")
    @patch(
        "ym_stock_data.sources.research._resolve_industry_code",
        return_value="1036",
    )
    def test_empty_result_is_explicit_success(self, resolve_code, get):
        get.return_value = _response([])

        result = research.fetch_industry_reports(industry="不存在的行业")

        self.assertEqual(0, result["total"])
        self.assertEqual([], result["reports"])
        self.assertNotIn("error", result)

    @patch("ym_stock_data.sources.research.CLIENT.get")
    @patch(
        "ym_stock_data.sources.research._resolve_industry_code",
        return_value="1036",
    )
    def test_timeout_is_exposed_with_source(self, resolve_code, get):
        get.side_effect = TimeoutError("timed out")

        result = research.fetch_industry_reports(industry="半导体")

        self.assertEqual("timed out", result["error"])
        self.assertEqual("TimeoutError", result["error_type"])
        self.assertEqual("eastmoney_industry_reportapi", result["source"])

    def test_query_requires_industry_or_code(self):
        with self.assertRaises(ValueError):
            research.fetch_industry_reports()

    @patch("ym_stock_data.sources.research.CLIENT.get")
    @patch(
        "ym_stock_data.sources.research._resolve_industry_code",
        return_value="1036",
    )
    def test_v1_route_adds_meta(self, resolve_code, get):
        get.return_value = _response([])

        result = fetch("industry_research", industry="半导体")

        self.assertEqual("industry_research", result["_meta"]["data_type"])


if __name__ == "__main__":
    unittest.main()
