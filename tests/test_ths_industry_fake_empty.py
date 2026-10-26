"""B2（2026-10-26 批次4）：同花顺 881 行业摘要的"空页静默截断"缺陷。

根因证据（evidence/BATCH4/b2-pagination-probe.json）：
同花顺行业摘要分页为 page1=50 行、page2=40 行、page3=0（共 90 个 881 行业）；
"电机"在 page2。原 `_fetch_all_industry_rows` 遇到解析为空的页直接 break，
一旦 page2 偶发空页/解析失败就静默只返回 page1 的 50 个行业，让"电机"等
后页行业被当成"上游没有该板块"（09:12 生产实测：电机 empty，同日稍后
同参数成功，说明是瞬时截断而不是长期缺源）。

修复点：
1. page>1 解析为空时先重试该页一次，仍空才视为分页结束；
2. 显式请求的 code/name 仍缺失时，整表重读一次再判定 missing；
   仍缺失才是合法 empty，并在返回中留下 retry 证据。
"""

import unittest
from unittest.mock import patch

from ym_stock_data.sources import ths_industry


def _row(code, name):
    return {"code": code, "name": name, "change_pct": 1.0}


PAGE1 = [_row(f"881{i:03d}", f"行业{i}") for i in range(50)]
PAGE2 = [_row("881277", "电机"), _row("881121", "半导体")]


class _Response:
    def __init__(self, text="<html></html>", status_code=200):
        self.text = text
        self.status_code = status_code
        self.encoding = "gbk"


class IndustryPaginationRetryTests(unittest.TestCase):
    def test_empty_page_after_first_is_retried_not_silently_truncated(self):
        responses = [_Response(), _Response(), _Response(), _Response()]
        parse_results = [list(PAGE1), [], list(PAGE2), []]
        with patch.object(ths_industry.requests, "get", side_effect=responses) as get, \
                patch.object(ths_industry, "_parse_industry_rows", side_effect=parse_results):
            rows = ths_industry._fetch_all_industry_rows()
        self.assertIsInstance(rows, list)
        names = {row["name"] for row in rows}
        self.assertIn("电机", names)
        self.assertEqual(52, len(rows))
        # page1 + page2(空一次) + page2(重试) + page3(结束) = 4 次请求
        self.assertEqual(4, get.call_count)

    def test_requested_name_missing_triggers_one_full_reread(self):
        first = list(PAGE1)
        second = list(PAGE1) + list(PAGE2)
        with patch.object(ths_industry, "_fetch_all_industry_rows", side_effect=[first, second]) as fetch:
            result = ths_industry.fetch_sector_index(names=["电机"])
        self.assertEqual(["电机"], [row["name"] for row in result["items"]])
        self.assertEqual(2, fetch.call_count)
        self.assertEqual(["电机"], result.get("recovered_missing"))
        self.assertEqual(2, result.get("retry_attempts"))

    def test_still_missing_after_reread_is_legal_empty(self):
        once = list(PAGE1)
        with patch.object(ths_industry, "_fetch_all_industry_rows", side_effect=[once, list(PAGE1)]) as fetch:
            result = ths_industry.fetch_sector_index(names=["电机"])
        self.assertEqual([], result["items"])
        self.assertEqual(["电机"], result["missing"])
        self.assertEqual(2, fetch.call_count)
        self.assertEqual([], result.get("recovered_missing") or [])


if __name__ == "__main__":
    unittest.main()
