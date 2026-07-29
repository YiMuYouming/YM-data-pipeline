from __future__ import annotations

import importlib
import warnings
import unittest
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ym_stock_data import fetch
from ym_stock_data.api import PROVIDER_REGISTRY
from ym_stock_data.routing import all_route_specs
from ym_stock_data.v2.resolve import resolve


ROOT = Path(__file__).resolve().parents[1]


class DeprecationAndDocumentationTests(unittest.TestCase):
    def test_readme_and_agents_recommend_only_public_query(self):
        for relative in ("README.md", "AGENTS.md"):
            with self.subTest(file=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("from ym_stock_data import query", text)
                self.assertNotIn("from ym_stock_data.v2.resolve import resolve", text)
                self.assertNotIn("from ym_stock_data import fetch", text)
                self.assertIn("./ym-data doctor --json", text)

    def test_provider_ownership_table_covers_all_governed_classes(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for marker in (
            "零鉴权",
            "API key",
            "可移植 runtime",
            "owned OAuth",
            "official CLI",
            "./ym-data setup pywencai",
            "./ym-data auth import-tdx --from-workbuddy",
            "wind_enrichment",
            "automatic fallback",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, readme)

        ownership = readme.split("## Provider ownership 与路由边界", 1)[1].split(
            "## Wind 显式研究增强", 1
        )[0]
        rows = {}
        for line in ownership.splitlines():
            if not line.startswith("| `"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            rows[cells[0].strip("`")] = line

        self.assertEqual(set(PROVIDER_REGISTRY), set(rows))
        self.assertNotIn("等本地/HTTP provider", ownership)

        routed_intents = defaultdict(set)
        for spec in all_route_specs():
            for provider in spec.providers:
                routed_intents[provider].add(spec.intent)
        for provider, intents in routed_intents.items():
            with self.subTest(provider=provider):
                for intent in intents:
                    self.assertIn(f"`{intent}`", rows[provider])

        self.assertIn("诊断聚合，无 RouteSpec", rows["tdx_mcp"])
        self.assertIn("所有排在其前的语义兼容源失败后", ownership)
        self.assertNotIn("仅在零鉴权兼容源失败后", ownership)

    def test_v2_design_documents_are_explicitly_historical(self):
        for relative in (
            "docs/YM-data-pipeline-2.0-数据源治理方案.md",
            "docs/YM-data-pipeline-v2.0-MVP-试运行记录.md",
        ):
            with self.subTest(file=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("2026-07-29", text)
                self.assertIn("from ym_stock_data import query", text)
                self.assertIn("兼容", text)

    def test_legacy_fetch_and_v2_resolve_survive_warning_as_error(self):
        canonical = {
            "data": {"quotes": [{"code": "000001"}]},
            "_meta": {
                "status": "success",
                "provider_used": "pytdx",
                "attempts": [
                    {
                        "provider": "pytdx",
                        "status": "success",
                        "error_code": None,
                        "latency_ms": 1,
                    }
                ],
                "fetched_at": "2026-07-29T15:00:00+08:00",
                "quality": {"status": "normal", "returned_count": 1},
            },
        }
        fetch_module = importlib.import_module("ym_stock_data.fetch")
        resolve_module = importlib.import_module("ym_stock_data.v2.resolve")
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            with patch.object(fetch_module, "canonical_query", return_value=canonical):
                fetched = fetch("quotes", codes=["000001"])
            with patch.object(resolve_module.public_api, "query", return_value=canonical):
                resolved = resolve(
                    "stock_snapshot",
                    codes=["000001"],
                    _now=datetime.fromisoformat("2026-07-29T15:00:01+08:00"),
                )

        self.assertEqual("canonical", fetched["_meta"]["compatibility_route"])
        self.assertEqual("pytdx", fetched["_meta"]["provider_used"])
        self.assertEqual(canonical["data"], resolved["data"])
        self.assertEqual("pytdx", resolved["_meta"]["provider_used"])


if __name__ == "__main__":
    unittest.main()
