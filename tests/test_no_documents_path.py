#!/usr/bin/env python3
"""守护测试：禁止写死 Documents/YM_Capital（审计回复 42 第五节）。

旧路径已废弃，仓库根一律按同级 checkout 推导。任何代码在 ym_stock_data/ 下
（排除 _archive）写死 Documents/YM_Capital 都判失败。
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = "Documents/YM_Capital"


class NoDocumentsYMCapitalTest(unittest.TestCase):
    def test_no_hardcoded_documents_path_in_ym_stock_data(self):
        offenders: list[str] = []
        for path in (ROOT / "ym_stock_data").rglob("*.py"):
            if "_archive" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if FORBIDDEN in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual([], offenders, f"禁止写死 {FORBIDDEN} 路径")


if __name__ == "__main__":
    unittest.main()
