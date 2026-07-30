from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = PIPELINE_ROOT / "docs/ACCEPTANCE_RUNBOOK.md"
MARKET_WATCH = Path("/Users/yimu/Documents/YM_Capital/Market_Watch")
LIVE_DASHBOARD = Path("/Users/yimu/Documents/YM_Capital/live-dashboard")


class AcceptanceRunbookTests(unittest.TestCase):
    def read_runbook(self) -> str:
        self.assertTrue(RUNBOOK.is_file(), "docs/ACCEPTANCE_RUNBOOK.md must exist")
        return RUNBOOK.read_text(encoding="utf-8")

    def test_runbook_is_single_complete_fresh_agent_entry(self) -> None:
        text = self.read_runbook()
        required = (
            "umask 077",
            "chmod 700",
            "同日去重",
            "Shanghai Stock Exchange",
            "https://www.sse.com.cn/",
            "./ym-data doctor --json",
            "./ym-data smoke --live",
            "./ym-data acceptance template --date",
            "./ym-data acceptance build",
            "./ym-data acceptance validate",
            "summarize_query_result",
            "_default_resolver",
            "extract_rows",
            "_effective_meta",
            "compat_iwencai_query",
            'mode="unified"',
            "empty_legacy",
            "zero_secret_scan",
            "shasum -a 256",
            "check-ignore",
            "acceptance 1.2",
            "smoke schema 2",
            "five-source-structured-v1",
            "11 个固定 case",
            "canonical registry",
            "旧 10-case",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, text)

    def test_runbook_code_blocks_use_existing_functions_and_no_forbidden_routes(self) -> None:
        text = self.read_runbook()
        bash_blocks = re.findall(r"```bash\n(.*?)```", text, re.DOTALL)
        snippets = "\n".join(bash_blocks)
        self.assertTrue(bash_blocks)
        for index, block in enumerate(bash_blocks):
            with self.subTest(language="bash", index=index):
                completed = subprocess.run(
                    ["bash", "-n"],
                    input=block,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
        python_blocks = re.findall(r"<<'PY'\n(.*?)\nPY", snippets, re.DOTALL)
        self.assertTrue(python_blocks)
        for index, block in enumerate(python_blocks):
            with self.subTest(language="python", index=index):
                compile(block, f"runbook-python-{index}", "exec")
        for forbidden in (
            "--save",
            "8088",
            "ym_stock_data.sources",
            "ym_stock_data.v2",
            "tdx-finance",
            "tdx_mcp",
            "wind_mcp",
            "mcp__",
            "from ym_stock_data.experimental",
            "fetch_wind_enrichment",
            "wind-mcp-skill",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, snippets)

        owners = (
            (PIPELINE_ROOT / "ym_stock_data/smoke.py", "def summarize_query_result"),
            (MARKET_WATCH / "scripts/run_c15_scan.py", "def _default_resolver"),
            (MARKET_WATCH / "scripts/c15_contract.py", "def extract_rows"),
            (MARKET_WATCH / "scripts/c15_contract.py", "def _effective_meta"),
            (LIVE_DASHBOARD / "scripts/ym_data_query.py", "def compat_iwencai_query"),
            (LIVE_DASHBOARD / "scripts/ym_data_query.py", "def compare_review_results"),
        )
        for path, definition in owners:
            with self.subTest(path=path, definition=definition):
                self.assertTrue(path.is_file())
                self.assertIn(definition, path.read_text(encoding="utf-8"))

    def test_probe_parameters_and_timezone_are_locked_to_reviewed_semantics(self) -> None:
        text = self.read_runbook()
        snippets = "\n".join(re.findall(r"```bash\n(.*?)```", text, re.DOTALL))
        self.assertIn('TZ=Asia/Shanghai date +%F', snippets)
        self.assertIn('TZ=Asia/Shanghai date +%H%M', snippets)
        self.assertIn(
            'query("review_sentiment", query="A股 非ST 涨停", limit=3)',
            snippets,
        )
        self.assertIn(
            'result = _default_resolver("review_sentiment")',
            snippets,
        )
        self.assertNotRegex(
            snippets,
            r'_default_resolver\("review_sentiment",\s*query=',
        )
        dashboard_call = re.search(
            r'result = compat_iwencai_query\((.*?)\n\)',
            snippets,
            re.DOTALL,
        )
        self.assertIsNotNone(dashboard_call)
        assert dashboard_call is not None
        self.assertIn('"A股 非ST 涨停"', dashboard_call.group(1))
        self.assertIn("limit=3", dashboard_call.group(1))
        self.assertNotIn("脱敏验收样例", snippets)
        self.assertRegex(
            snippets,
            r'rg -n -i .*?"\$acceptance_tmp" "\$smoke_receipt"',
        )

        dashboard_block = re.search(
            r"## 7\. live-dashboard unified no-save 探针.*?```bash\n(.*?)```",
            text,
            re.DOTALL,
        )
        self.assertIsNotNone(dashboard_block)
        assert dashboard_block is not None
        dashboard = dashboard_block.group(1)
        self.assertEqual(
            1,
            dashboard.count('query("review_sentiment", query="A股 非ST 涨停", limit=3)'),
        )
        for required in (
            "compare_review_results",
            "legacy_review_query",
            'if default_mode == "legacy":',
            'comparison_status = "unified_default_observed"',
            "canonical_fn=lambda",
            "legacy_fn=legacy_call",
        ):
            with self.subTest(required=required):
                self.assertIn(required, dashboard)
        self.assertNotIn('"query":', dashboard)
        self.assertNotIn('"rows":', dashboard)
        self.assertNotIn("sha256", dashboard.lower())

    def test_other_entry_docs_link_runbook_without_copying_input_schema(self) -> None:
        documents = {
            "AGENTS.md": (PIPELINE_ROOT / "AGENTS.md").read_text(encoding="utf-8"),
            "README.md": (PIPELINE_ROOT / "README.md").read_text(encoding="utf-8"),
            "Task14": (PIPELINE_ROOT / "docs/superpowers/plans/2026-07-29-unified-a-share-data-channel.md").read_text(encoding="utf-8"),
        }
        for name, text in documents.items():
            with self.subTest(name=name):
                self.assertIn("docs/ACCEPTANCE_RUNBOOK.md", text)
        self.assertNotIn("--downstream", documents["AGENTS.md"])
        readme_section = documents["README.md"].split("## 五日验收记录", 1)[1].split("\n## ", 1)[0]
        task_section = documents["Task14"].split("### Task 14:", 1)[1].split("\n---", 1)[0]
        self.assertNotIn("--downstream", readme_section)
        self.assertNotIn("--downstream", task_section)


if __name__ == "__main__":
    unittest.main()
