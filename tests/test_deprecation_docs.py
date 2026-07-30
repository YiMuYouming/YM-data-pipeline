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
    def test_root_claude_recommends_only_the_canonical_channel(self):
        text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

        for current in (
            "from ym_stock_data import query",
            "./ym-data doctor --json",
            '`result["_meta"]["attempts"]`',
            '`result["_meta"]["provider_used"]`',
            "WenCai OpenAPI",
            "portable pywencai",
            "TDX owned OAuth",
            "official Wind CLI",
            "zero-auth PyTDX",
            "./ym-data auth login-tdx",
            "./ym-data auth status-tdx",
            "compatibility wrapper",
        ):
            with self.subTest(current=current):
                self.assertIn(current, text)

        for stale in (
            "from ym_stock_data import fetch",
            "from ym_stock_data.v2.resolve import resolve",
            "tdx-finance",
            "WorkBuddy",
            "tdx_lookup_stock",
        ):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, text)

    def test_readme_and_agents_recommend_only_public_query(self):
        for relative in ("README.md", "AGENTS.md"):
            with self.subTest(file=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("from ym_stock_data import query", text)
                self.assertNotIn("from ym_stock_data.v2.resolve import resolve", text)
                self.assertNotIn("from ym_stock_data import fetch", text)
                self.assertIn("./ym-data doctor --json", text)

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        review_row = next(
            line
            for line in readme.splitlines()
            if line.startswith("| `review_sentiment` |")
        )
        params = {
            value.strip().strip("`")
            for value in review_row.split("|")[3].split(", ")
        }
        self.assertEqual(
            {
                "query",
                "limit",
                "expected_row_shape",
                "expected_count",
                "date",
                "lang",
                "version",
            },
            params,
        )
        self.assertNotIn("page", review_row)

        install = (ROOT / "docs/INSTALL.md").read_text(encoding="utf-8")
        for stale in ("Task 8 只提供", "Task 9 尚未就绪", "等待 Task 13"):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, install)
        for current in (
            "./ym-data auth login-tdx",
            "./ym-data auth status-tdx",
            "macOS Keychain",
            "显式 `--store file`",
            "目录 `0700`",
            "文件和锁 `0600`",
            "不会读取或导入其它应用的凭据",
            "PKCE S256",
            "`mcp.read`",
            "`auth_missing`",
            "`tools/list`",
            "只读小调用",
            "不称为在线",
            "永久兼容边界",
            "不推荐新代码",
            "不承诺迁移时间",
        ):
            with self.subTest(current=current):
                self.assertIn(current, install)

    def test_provider_ownership_table_covers_all_governed_classes(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for marker in (
            "零鉴权",
            "API key",
            "可移植 runtime",
            "owned OAuth",
            "official CLI",
            "./ym-data setup pywencai",
            "./ym-data auth login-tdx",
            "./ym-data auth status-tdx",
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
        self.assertIn("所有排在其前的语义兼容源失败或合法空集后", ownership)
        self.assertNotIn("仅在零鉴权兼容源失败后", ownership)

        pywencai_row = rows["pywencai"]
        for state in ("configured_unverified", "dependency_missing", "unavailable"):
            with self.subTest(state=state):
                self.assertIn(state, pywencai_row)
        self.assertNotIn("`ready`", pywencai_row)
        self.assertIn("runtime installed", readme)
        self.assertIn("不证明在线", readme)
        for relative in (
            "README.md",
            "docs/INSTALL.md",
            "AGENTS.md",
            "docs/TDX-MCP-备用源验证清单.md",
        ):
            with self.subTest(no_external_credential_import=relative):
                text = (ROOT / relative).read_text(encoding="utf-8").lower()
                self.assertNotIn("workbuddy", text)
                self.assertNotIn("import-tdx", text)

    def test_current_docs_lock_wind_screener_and_restarted_acceptance_scope(self):
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        provider_boundary = agents.split("## Provider 边界", 1)[1].split(
            "## 下游与回滚", 1
        )[0]
        self.assertIn("`wind_enrichment`", provider_boundary)
        self.assertIn("`filings`", provider_boundary)
        self.assertIn("`wind_screener`", provider_boundary)
        self.assertIn("`stock_data.search_stocks`", provider_boundary)

        plan = (
            ROOT / "docs/superpowers/plans/2026-07-29-unified-a-share-data-channel.md"
        ).read_text(encoding="utf-8")
        explicit_review_row = next(
            line
            for line in plan.splitlines()
            if line.startswith("| `review_sentiment` with explicit query |")
        )
        self.assertIn(
            "`iwencai_openapi` → `pywencai` → `tdx_screener` → `wind_screener`",
            explicit_review_row,
        )
        stock_event_row = next(
            line
            for line in plan.splitlines()
            if line.startswith("| `stock_event` |")
        )
        self.assertNotIn("wind_mcp", stock_event_row)
        self.assertIn("新五源范围", plan)
        self.assertIn("五类受管来源", plan)
        for source in (
            "WenCai OpenAPI",
            "portable pywencai",
            "TDX owned OAuth",
            "official Wind CLI",
            "zero-auth PyTDX",
        ):
            with self.subTest(source=source):
                self.assertIn(source, plan)
        self.assertIn("验收窗口必须重新开始", plan)

        routing_example = plan.split(
            "### Task 3: Add the provider protocol and canonical route registry", 1
        )[1].split("### Task 4:", 1)[0]
        routing_code = routing_example.split("```python", 1)[1].split("```", 1)[0]
        for providers in (
            ("iwencai_openapi", "pywencai", "tdx_screener", "wind_screener"),
            ("pytdx_breadth", "eastmoney_breadth", "eastmoney_limit_pool"),
        ):
            with self.subTest(providers=providers):
                for name in providers:
                    self.assertIn(f'"{name}"', routing_code)
                positions = [routing_code.index(f'"{name}"') for name in providers]
                self.assertEqual(sorted(positions), positions)

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
