from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from scripts.audit_data_consumers import (
    DEFAULT_REPOSITORIES,
    MAX_TEXT_BYTES,
    REQUIRED_CONSUMER_FIELDS,
    _agent_tooling_alias_tree_sha256,
    _inventory_agent_tooling_channels,
    audit_exit_code,
    _cached_text_matches,
    _python_internal_transport_matches,
    load_consumer_config,
    scan_repositories,
    validate_consumer_coverage,
    validate_consumer_config,
    write_audit_outputs,
)


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REPOSITORIES = {
    "Market_Watch",
    "live-dashboard",
    "YiMu_IR",
    "YiMu_Quant",
    "YiMu_Intelligence",
    "live-trading",
    "portal",
    "ai-rule-system",
}


class ConsumerInventoryV3Tests(unittest.TestCase):
    def test_internal_transport_only_propagates_through_http_helpers(self):
        text = '''
import os
import requests

def _number(value):
    return value.get("price")

class LocalClient:
    def request(self, method, target):
        return {"method": method, "target": target}

def send(method, target):
    requests.request(method, target)

def send_unknown(method, target):
    requests.request(method, target)

client = LocalClient()
_number({"price": 1})
client.request("GET", "/api/status")
send("POST", "/api/trade")
send_unknown(os.getenv("METHOD"), os.getenv("TARGET"))
requests.get("/api/status")
'''

        matches = _python_internal_transport_matches(text)
        kinds = [match[0] for line in matches.values() for match in line]

        self.assertEqual(kinds.count("known_internal_write_transport"), 1)
        self.assertEqual(kinds.count("known_internal_read_transport"), 1)
        self.assertGreaterEqual(kinds.count("ambiguous_internal_transport"), 1)
        self.assertNotIn(4, matches)
        self.assertNotIn(19, matches)

    def test_jev_shadow_number_helpers_are_not_internal_transport_findings(self):
        source = (ROOT.parent / "Market_Watch" / "scripts" / "jev_shadow_rerank.py").read_text(
            encoding="utf-8"
        )

        matches = _python_internal_transport_matches(source)
        false_positive_lines = {140, 142, 143, 159, 160, 161, 162, 241}

        self.assertFalse(
            any(
                line in false_positive_lines
                and any(match[0] == "ambiguous_internal_transport" for match in line_matches)
                for line, line_matches in matches.items()
            )
        )

    def test_remote_internal_post_helper_stays_production_write_and_unknown_stays_ambiguous(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "fixture-repo"
            repo.mkdir()
            (repo / "transport_helpers.py").write_text(
                'import os\n'
                'import requests\n'
                '\n'
                'def remote_post(method, target):\n'
                '    requests.request(method, target)\n'
                '\n'
                'def unknown_http(method, target):\n'
                '    requests.request(method, target)\n'
                '\n'
                'remote_post("POST", "/api/trade")\n'
                'unknown_http(os.getenv("METHOD"), os.getenv("TARGET"))\n',
                encoding="utf-8",
            )

            report = scan_repositories((("fixture-repo", repo),))

        findings = [item for item in report["findings"] if item["file"] == "transport_helpers.py"]
        self.assertTrue(
            any(
                item["match_kind"] == "known_internal_write_transport"
                and item["classification"] == "production_write_transport"
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["match_kind"] == "ambiguous_internal_transport"
                and item["classification"] == "ambiguous"
                for item in findings
            )
        )

    def test_fixture_scan_covers_languages_categories_and_symlink_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir)
            repo = parent / "fixture-repo"
            outside = parent / "outside.py"
            repo.mkdir()
            (repo / ".gitignore").write_text("src/main.py\n", encoding="utf-8")
            (repo / "src").mkdir()
            (repo / "src" / "main.py").write_text(
                'from ym_stock_data.v2 import resolve\n'
                'import requests\n'
                'requests.get("https://api.tushare.pro/data")\n',
                encoding="utf-8",
            )
            (repo / "web.ts").write_text(
                'const client = fetch("https://eastmoney.example/api");\n',
                encoding="utf-8",
            )
            (repo / "tests").mkdir()
            (repo / "tests" / "test_bypass.py").write_text(
                "import pytdx\n", encoding="utf-8"
            )
            (repo / "fixtures").mkdir()
            (repo / "fixtures" / "fixture.yaml").write_text(
                "provider: akshare\n", encoding="utf-8"
            )
            (repo / "archive").mkdir()
            (repo / "archive" / "old.py").write_text(
                "import easyquotation\n", encoding="utf-8"
            )
            (repo / "generated").mkdir()
            (repo / "generated" / "report.sh").write_text(
                "curl https://pywencai.example/query\n", encoding="utf-8"
            )
            (repo / "pyproject.toml").write_text(
                '[project]\ndependencies = ["httpx", "akshare"]\n', encoding="utf-8"
            )
            (repo / "skills-lock.json").write_text(
                '{"skill": "iwencai", "package": "httpx"}\n', encoding="utf-8"
            )
            (repo / "compiled").mkdir()
            (repo / "compiled" / "rules.v1.json").write_text(
                '{"provider": "pytdx"}\n', encoding="utf-8"
            )
            (repo / ".superpowers" / "brainstorm").mkdir(parents=True)
            (repo / ".superpowers" / "brainstorm" / "draft.html").write_text(
                'fetch("https://eastmoney.com/api");\n', encoding="utf-8"
            )
            (repo / "ui.js").write_text(
                'const label = "iwencai";\nconst data = fetch("/api/quotes");\n',
                encoding="utf-8",
            )
            (repo / "generic.py").write_text(
                "import httpx\nclient = httpx.get(url)\n", encoding="utf-8"
            )
            (repo / "vendor_reference.py").write_text(
                'VENDOR_URL = "https://eastmoney.com/api"\n', encoding="utf-8"
            )
            (repo / "vendor_multiline.py").write_text(
                'requests.get(\n    "https://eastmoney.com/api",\n)\n', encoding="utf-8"
            )
            (repo / "provider_import.py").write_text(
                "from pytdx.hq import TdxHq_API\n", encoding="utf-8"
            )
            (repo / "python_prose.py").write_text(
                'payload = {"error": "000425 daily returned no data from PyTDX"}\n'
                'label = "from pytdx.hq import TdxHq_API"\n'
                'source = "ym_stock_data.v2"\n',
                encoding="utf-8",
            )
            (repo / "python_dynamic.py").write_text(
                'code = """\n'
                'from ym_stock_data.v2.resolve import resolve\n'
                'from pytdx.hq import TdxHq_API\n'
                '"""\n'
                'exec(code)\n',
                encoding="utf-8",
            )
            (repo / "python_dynamic_imports.py").write_text(
                'import importlib\n'
                'import pytdx as provider_alias\n'
                'from ym_stock_data import v2 as data_api\n'
                'from ym_stock_data import query as public_query\n'
                'provider = importlib.import_module("pytdx")\n'
                'private = __import__("ym_stock_data.v2")\n'
                'data_api.resolve()\n'
                'public_query({"intent": "quotes", "params": {}})\n'
                'prose = "importlib.import_module(\\\'pytdx\\\')"\n',
                encoding="utf-8",
            )
            (repo / "vendor_dataflow.py").write_text(
                'import urllib.request\n'
                'VENDOR_URL = "https://api.eastmoney.com/quotes"\n'
                'def fetch(url):\n'
                '    request = urllib.request.Request(url)\n'
                '    return urllib.request.urlopen(request)\n'
                'url = f"{VENDOR_URL}?page=1"\n'
                'response = fetch(url)\n',
                encoding="utf-8",
            )
            (repo / "vendor_dataflow_unknown.py").write_text(
                'import os\n'
                'import urllib.request\n'
                'BASE_URL = os.environ["DATA_URL"]\n'
                'def fetch(url):\n'
                '    request = urllib.request.Request(url)\n'
                '    return urllib.request.urlopen(request)\n'
                'url = f"{BASE_URL}/quotes"\n'
                'response = fetch(url)\n',
                encoding="utf-8",
            )
            (repo / "source_citation_internal.py").write_text(
                'SOURCE_CITATION = "https://eastmoney.com/api/docs"\n'
                'response = requests.get("/api/quotes")\n',
                encoding="utf-8",
            )
            (repo / "src" / "providers").mkdir(parents=True)
            (repo / "src" / "providers" / "direct.py").write_text(
                "import pytdx\nimport akshare\n", encoding="utf-8"
            )
            for directory, provider in (
                ("data", "pytdx"),
                ("runtime", "akshare"),
                ("history", "pytdx"),
                ("legacy", "akshare"),
                ("skills", "pytdx"),
                ("notes", "tushare"),
            ):
                (repo / directory).mkdir()
                suffix = ".sh" if directory == "notes" else ".py"
                (repo / directory / f"direct{suffix}").write_text(
                    f"import {provider}\n" if suffix == ".py" else f"curl https://api.tushare.pro/data\n",
                    encoding="utf-8",
                )
            (repo / "vendor_hosts.py").write_text(
                'requests.get("https://api.eastmoney.com/api")\n'
                'requests.get("https://eastmoney.com.evil/api")\n'
                'requests.get("https://not-eastmoney.com/api")\n',
                encoding="utf-8",
            )
            (repo / "internal_post.py").write_text(
                'requests.post("http://127.0.0.1:8088/api/trade")\n', encoding="utf-8"
            )
            (repo / "internal_post_multiline.py").write_text(
                'requests.request(\n    "POST",\n    "http://127.0.0.1:8088/api/trade",\n)\n',
                encoding="utf-8",
            )
            (repo / "internal_transport_dataflow.py").write_text(
                'import requests\n'
                'METHOD = "PATCH"\n'
                'TARGET = "/api/trade"\n'
                'requests.request(METHOD, TARGET)\n'
                'READ_TARGET = "http://127.0.0.1:8088/api/status"\n'
                'requests.get(READ_TARGET)\n',
                encoding="utf-8",
            )
            (repo / "internal_transport_unknown.py").write_text(
                'import os\n'
                'import requests\n'
                'METHOD = os.getenv("METHOD")\n'
                'TARGET = os.getenv("TARGET")\n'
                'requests.request(METHOD, TARGET)\n',
                encoding="utf-8",
            )
            outside.write_text(
                "from ym_stock_data.sources import quotes\n", encoding="utf-8"
            )
            (repo / "src" / "external.py").symlink_to(outside)

            report = scan_repositories((("fixture-repo", repo),))

        findings = report["findings"]
        self.assertTrue(
            any(
                item["file"] == "src/main.py"
                and item["classification"] == "production_bypass"
                for item in findings
            )
        )
        self.assertTrue(any(item["classification"] == "test" for item in findings))
        self.assertTrue(any(item["classification"] == "fixture" for item in findings))
        self.assertTrue(any(item["classification"] == "archive" for item in findings))
        self.assertTrue(any(item["classification"] == "generated" for item in findings))
        self.assertTrue(
            any(
                item["match_kind"] == "external_symlink"
                and item["classification"] == "unknown"
                for item in findings
            )
        )
        self.assertIn("src/main.py", report["manifest"]["scanned_files"])
        self.assertNotIn("src/main.py", report["manifest"]["ignored_by_gitignore"])
        self.assertTrue(
            any(
                item["file"] == "provider_import.py"
                and item["classification"] == "production_bypass"
                for item in findings
            )
        )
        self.assertFalse(
            any(
                item["file"] == "python_prose.py"
                and item["classification"] == "production_bypass"
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["file"] == "python_dynamic.py"
                and item["match_kind"] == "private_import"
                and item["classification"] == "production_bypass"
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["file"] == "python_dynamic_imports.py"
                and item["match_kind"] == "provider_import"
                and item["classification"] == "production_bypass"
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["file"] == "python_dynamic_imports.py"
                and item["match_kind"] == "private_import"
                and item["classification"] == "production_bypass"
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["file"] == "python_dynamic_imports.py"
                and item["match_kind"] == "private_access"
                and "ym_stock_data.v2.resolve" in item["symbol_or_url"]
                for item in findings
            )
        )
        self.assertFalse(
            any(
                item["file"] == "python_dynamic_imports.py"
                and item["match_kind"] in {"provider_import", "private_import", "private_access"}
                and item["line"] == 9
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["file"] == "python_dynamic.py"
                and item["match_kind"] == "provider_import"
                and item["classification"] == "production_bypass"
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["file"] == "src/providers/direct.py"
                and item["classification"] == "production_bypass"
                for item in findings
            )
        )
        for directory in ("data", "runtime", "history", "legacy", "skills", "notes"):
            self.assertTrue(
                any(
                    item["file"].startswith(f"{directory}/")
                    and item["classification"] == "production_bypass"
                    for item in findings
                ),
                directory,
            )
        self.assertTrue(
            any(
                item["file"] == "vendor_hosts.py"
                and item["match_kind"] == "known_vendor_url"
                and item["symbol_or_url"] == "https://api.eastmoney.com/api"
                for item in findings
            )
        )
        self.assertFalse(
            any(
                item["file"] == "vendor_hosts.py"
                and item["match_kind"] == "known_vendor_url"
                and "evil" in item["symbol_or_url"]
                for item in findings
            )
        )
        self.assertFalse(
            any(
                item["file"] == "vendor_hosts.py"
                and item["match_kind"] == "known_vendor_url"
                and "not-eastmoney" in item["symbol_or_url"]
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["file"] == "vendor_multiline.py"
                and item["match_kind"] == "known_vendor_url"
                and item["classification"] == "production_bypass"
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["file"] == "vendor_reference.py"
                and item["classification"] == "production_reference"
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["file"] == "generic.py"
                and item["classification"] == "production_reference"
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["file"] == "internal_post.py"
                and item["classification"] == "production_write_transport"
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["file"] == "internal_post_multiline.py"
                and item["classification"] == "production_write_transport"
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["file"] == "ui.js"
                and item["classification"] == "approved_internal_transport"
                for item in findings
            )
        )
        self.assertFalse(
            any(item["file"] == "ui.js" and item["classification"] == "production_bypass" for item in findings)
        )
        self.assertTrue(any(item["file"] == "pyproject.toml" and item["classification"] == "dependency_metadata" for item in findings))
        self.assertTrue(any(item["file"] == "skills-lock.json" and item["classification"] == "dependency_metadata" for item in findings))
        self.assertTrue(any(item["file"] == ".superpowers/brainstorm/draft.html" and item["classification"] == "documentation" for item in findings))
        self.assertTrue(any(item["file"] == "compiled/rules.v1.json" and item["classification"] == "generated" for item in findings))
        self.assertTrue(
            any(
                item["file"] == "vendor_dataflow.py"
                and item["match_kind"] == "known_vendor_transport"
                and item["classification"] == "production_bypass"
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["file"] == "vendor_dataflow_unknown.py"
                and item["classification"] == "ambiguous"
                for item in findings
            )
        )
        self.assertFalse(
            any(
                item["file"] == "source_citation_internal.py"
                and item["classification"] == "production_bypass"
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["file"] == "source_citation_internal.py"
                and item["classification"] == "approved_internal_transport"
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["file"] == "internal_transport_dataflow.py"
                and item["match_kind"] == "known_internal_write_transport"
                and item["classification"] == "production_write_transport"
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["file"] == "internal_transport_dataflow.py"
                and item["match_kind"] == "known_internal_read_transport"
                and item["classification"] == "approved_internal_transport"
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item["file"] == "internal_transport_unknown.py"
                and item["match_kind"] == "ambiguous_internal_transport"
                and item["classification"] == "ambiguous"
                for item in findings
            )
        )

    def test_repository_cache_rescans_same_size_mtime_when_content_or_inode_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "fixture-repo"
            repo.mkdir()
            target = repo / "provider.py"
            clean = "import mathx\n"
            dirty = "import pytdx\n"
            self.assertEqual(len(clean), len(dirty))
            target.write_text(clean, encoding="utf-8")
            first_stat = target.stat()
            first = scan_repositories((("fixture-repo", repo),))
            target.write_text(dirty, encoding="utf-8")
            os_utime = __import__("os").utime
            os_utime(target, ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns))
            second = scan_repositories((("fixture-repo", repo),))

        self.assertFalse(any(item["classification"] == "production_bypass" for item in first["findings"]))
        self.assertTrue(
            any(
                item["file"] == "provider.py" and item["classification"] == "production_bypass"
                for item in second["findings"]
            )
        )

    def test_real_local_query_specs_vendor_url_is_reference_not_transport(self):
        path = ROOT.parent / "Market_Watch" / "scripts" / "local_query_specs.py"
        text = path.read_text(encoding="utf-8")
        findings = _cached_text_matches(
            text,
            text.splitlines(),
            "python",
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        kinds = {kind for _line, kind, _symbol, _reason, _context in findings}
        self.assertIn("from ym_stock_data import query", text)
        self.assertIn("known_vendor_url_reference", kinds)
        self.assertNotIn("known_vendor_transport", kinds)

    def test_agent_tooling_channels_inventory_is_scoped_and_redacted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "fixture-repo"
            skill_root = repo / ".agents" / "skills" / "foo"
            (skill_root / "scripts").mkdir(parents=True)
            (skill_root / "SKILL.md").write_text(
                "Route A-share facts through public ym_stock_data.query only.\n",
                encoding="utf-8",
            )
            client = skill_root / "scripts" / "client.js"
            client.write_text(
                'fetch("https://eastmoney.com/api/quotes")\n', encoding="utf-8"
            )
            client.chmod(0o755)
            (repo / ".agents" / "memory").mkdir(parents=True)
            (repo / ".agents" / "memory" / "secret.py").write_text(
                "fetch(SECRET)\n", encoding="utf-8"
            )
            (repo / ".agents" / "download").mkdir(parents=True)
            (repo / ".agents" / "download" / "client.sh").write_text(
                "curl https://eastmoney.com/api\n", encoding="utf-8"
            )
            symlink = skill_root / "scripts" / "linked.sh"
            symlink.symlink_to(repo / "outside.sh")
            report = scan_repositories((("fixture-repo", repo),))

        channels = report["manifest"]["agent_tooling_channels"]
        paths = {item["path"] for item in channels}
        self.assertIn(".agents/skills/foo/SKILL.md", paths)
        self.assertIn(".agents/skills/foo/scripts/client.js", paths)
        self.assertNotIn(".agents/memory/secret.py", paths)
        self.assertNotIn(".agents/download/client.sh", paths)
        self.assertIn(".agents/skills/foo/scripts/linked.sh", paths)
        client_record = next(
            item for item in channels if item["path"].endswith("scripts/client.js")
        )
        self.assertEqual("out_of_band_provider_channel", client_record["classification"])
        self.assertIn("canonical query", client_record["owner_policy"])
        self.assertEqual(
            report["manifest"]["agent_tooling_channels_sha256"],
            report["summary"]["agent_tooling_channels_sha256"],
        )
        self.assertEqual(paths, {item["path"] for item in report["summary"]["agent_tooling_channels"]})
        linked = next(item for item in channels if item["path"].endswith("scripts/linked.sh"))
        self.assertEqual("human_review_required", linked["classification"])
        self.assertEqual("symlink_not_followed", linked["scan_status"])
        excluded = report["manifest"]["agent_tooling_excluded_paths"]
        self.assertEqual(
            {".agents/memory", ".agents/download"},
            {item["path"] for item in excluded},
        )
        self.assertNotEqual(0, report["summary"]["agent_tooling_scan_error_files"])
        self.assertEqual(1, audit_exit_code(report["summary"]))

    def test_agent_symlink_alias_binds_relative_target_and_target_tree_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "fixture-repo"
            target = repo / ".agents" / "skills" / "wind"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("wind channel\n", encoding="utf-8")
            alias = repo / ".claude" / "skills" / "wind-mcp-skill"
            alias.parent.mkdir(parents=True)
            alias.symlink_to("../../.agents/skills/wind")
            binding = {
                "repository": "fixture-repo",
                "path": ".claude/skills/wind-mcp-skill",
                "expected_relative_target": "../../.agents/skills/wind",
                "target_tree_sha256": _agent_tooling_alias_tree_sha256(target),
            }
            aliases = {(binding["repository"], binding["path"]): binding}

            records, _excluded = _inventory_agent_tooling_channels(
                "fixture-repo", repo, alias_bindings=aliases
            )
            bound = next(item for item in records if item["path"] == binding["path"])
            self.assertEqual("symlink_alias_bound", bound["scan_status"])
            self.assertEqual(binding["expected_relative_target"], bound["symlink_target"])
            self.assertEqual(binding["target_tree_sha256"], bound["target_tree_sha256"])
            self.assertEqual("out_of_band_provider_channel", bound["classification"])

            scenarios = {
                "target_drift": lambda: (target / "SKILL.md").write_text("drift\n", encoding="utf-8"),
                "external": lambda: alias.unlink() or alias.symlink_to(Path(temp_dir) / "outside"),
                "absolute": lambda: alias.unlink() or alias.symlink_to((repo / ".agents" / "skills" / "wind").resolve()),
                "missing": lambda: alias.unlink() or alias.symlink_to("../../.agents/skills/missing"),
                "cycle": lambda: (alias.unlink(), alias.symlink_to("../../.claude/skills/wind-mcp-skill")),
            }
            for name, mutate in scenarios.items():
                with self.subTest(name=name):
                    if name != "target_drift":
                        (target / "SKILL.md").write_text("wind channel\n", encoding="utf-8")
                    mutate()
                    records, _excluded = _inventory_agent_tooling_channels(
                        "fixture-repo", repo, alias_bindings=aliases
                    )
                    failed = next(item for item in records if item["path"] == binding["path"])
                    self.assertTrue(failed["scan_status"].startswith("symlink_alias_error:"))
                    self.assertEqual("human_review_required", failed["classification"])
                    if name == "target_drift":
                        (target / "SKILL.md").write_text("wind channel\n", encoding="utf-8")
                    alias.unlink()
                    alias.symlink_to("../../.agents/skills/wind")

    def test_real_inventory_binds_portal_non_data_hash_and_internal_wind_alias(self):
        config = load_consumer_config(ROOT / "config" / "data-consumers.v3.json")
        validate_consumer_config(config)
        report = scan_repositories(DEFAULT_REPOSITORIES, consumer_config=config)
        channels = report["manifest"]["agent_tooling_channels"]
        portal = next(
            item
            for item in channels
            if item["repository"] == "portal"
            and item["path"] == ".agents/skills/portal-sync-flow/SKILL.md"
        )
        self.assertEqual("specialized_non_data", portal["classification"])
        self.assertEqual(
            "1562af82ffb100f0be1fc16fa4f557e28d96c7122f16ad6e7c865ba8533bb185",
            portal["sha256"],
        )
        alias = next(
            item
            for item in channels
            if item["repository"] == "YiMu_IR"
            and item["path"] == ".claude/skills/wind-mcp-skill"
        )
        self.assertEqual("symlink_alias_error:target_tree_hash_drift", alias["scan_status"])
        self.assertEqual("human_review_required", alias["classification"])
        self.assertIsNone(alias["target_tree_sha256"])
        self.assertEqual("../../.agents/skills/wind-mcp-skill", alias["symlink_target"])
        self.assertEqual(1, report["summary"]["unknown_findings"])
        self.assertEqual(1, report["summary"]["agent_tooling_human_review_files"])
        self.assertEqual(1, report["summary"]["agent_tooling_scan_error_files"])
        self.assertEqual(1, audit_exit_code(report["summary"]))

    def test_production_file_fail_closed_for_oversized_and_read_errors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "fixture-repo"
            repo.mkdir()
            (repo / "large.py").write_bytes(b"x" * (MAX_TEXT_BYTES + 1))
            (repo / "unreadable.py").write_text("import pytdx\n", encoding="utf-8")
            original_read_bytes = Path.read_bytes

            def read_bytes(path):
                if path.name == "unreadable.py":
                    raise OSError("fixture read failure")
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", read_bytes):
                report = scan_repositories((("fixture-repo", repo),))

        unknown = {
            (item["file"], item["match_kind"])
            for item in report["findings"]
            if item["classification"] == "unknown"
        }
        self.assertIn(("large.py", "unscanned_production_file"), unknown)
        self.assertIn(("unreadable.py", "unreadable_production_file"), unknown)
        self.assertTrue(report["manifest"]["scan_failures"])
        self.assertTrue(any(item["path"] == "unreadable.py" for item in report["manifest"]["scan_failures"]))

    def test_repository_scan_failure_is_manifested_as_unknown_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "fixture-repo"
            repo.mkdir()
            (repo / "safe.py").write_text("x = 1\n", encoding="utf-8")
            original_scandir = __import__("os").scandir

            def broken_scandir(path):
                if Path(path) == repo:
                    raise OSError("fixture scandir failure")
                return original_scandir(path)

            with patch("scripts.audit_data_consumers.os.scandir", side_effect=broken_scandir):
                report = scan_repositories((("fixture-repo", repo),))

        self.assertTrue(any(item["match_kind"] == "repository_scan_error" for item in report["findings"]))
        self.assertTrue(report["manifest"]["scan_failures"])
        self.assertEqual(1, audit_exit_code(report["summary"]))

    def test_compatibility_allowlist_is_repository_and_match_kind_scoped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir)
            live_repo = parent / "live-dashboard"
            other_repo = parent / "other-repo"
            for repo in (live_repo, other_repo):
                (repo / "scripts").mkdir(parents=True)
                (repo / "scripts" / "ym_data_query.py").write_text(
                    "from ym_stock_data.sources.iwencai import query\nimport akshare\n",
                    encoding="utf-8",
                )
            report = scan_repositories((("live-dashboard", live_repo), ("other-repo", other_repo)))

        live = [item for item in report["findings"] if item["repository"] == "live-dashboard"]
        other = [item for item in report["findings"] if item["repository"] == "other-repo"]
        self.assertTrue(any(item["match_kind"] == "private_import" and item["classification"] == "approved_compatibility" for item in live))
        self.assertTrue(any(item["match_kind"] == "provider_import" and item["classification"] == "production_bypass" for item in live))
        self.assertTrue(any(item["match_kind"] == "private_import" and item["classification"] == "production_bypass" for item in other))

    def test_production_bypass_files_have_unique_config_owners(self):
        config = load_consumer_config(ROOT / "config" / "data-consumers.v3.json")
        validate_consumer_config(config)
        report = scan_repositories(DEFAULT_REPOSITORIES, consumer_config=config)
        coverage = validate_consumer_coverage(config, report["findings"])
        migrated_consumers = {
            ("Market_Watch", "scripts/board_history.py"),
            ("YiMu_IR", "scripts/build_20260630_semiconductor_three_stock.py"),
            ("live-dashboard", "scripts/backfill_history.py"),
            ("live-dashboard", "scripts/backfill_intraday.py"),
            ("live-trading", "shadow_trading/data_router.py"),
        }
        records_by_entry = {
            (record["repository"], record["entry_point"]): record
            for record in config["consumers"]
        }
        migration_findings = set(report["summary"]["migration_production_bypass_files"])
        orphan_claims = {
            (item["repository"], item["file"])
            for item in coverage["orphan_owner_claims"]
        }
        for repository, entry_point in migrated_consumers:
            with self.subTest(consumer=f"{repository}:{entry_point}"):
                record = records_by_entry[(repository, entry_point)]
                self.assertEqual("compliant", record["migration_status"])
                self.assertEqual([], record["finding_files"])
                self.assertNotIn(f"{repository}:{entry_point}", migration_findings)
                self.assertNotIn((repository, entry_point), orphan_claims)
                self.assertFalse(
                    any(
                        finding["repository"] == repository
                        and finding["file"] == entry_point
                        and finding["classification"] == "production_bypass"
                        for finding in report["findings"]
                    )
                )
        self.assertEqual([], coverage["unclassified_production_files"])
        self.assertEqual(
            coverage["production_bypass_files"],
            coverage["covered_production_files"],
        )
        self.assertEqual(0, report["summary"]["unclassified_production_files"])
        self.assertEqual([], coverage["orphan_owner_claims"])
        self.assertEqual([], coverage["missing_entry_points"])
        self.assertEqual([], coverage["unprotected_write_transport_files"])
        self.assertEqual(4, len(coverage["protected_write_transport_files"]))
        self.assertEqual(4, report["summary"]["write_transport_files"])
        self.assertEqual(4, report["summary"]["protected_write_transport_files"])
        self.assertEqual(0, report["summary"]["unprotected_write_transport_files"])
        self.assertEqual([], report["summary"]["agent_tooling_config_unmatched"])
        self.assertNotIn(
            "Market_Watch:scripts/local_query_specs.py",
            report["summary"]["migration_production_bypass_files"],
        )
        self.assertEqual(
            report["summary"]["production_bypass_files"],
            len(report["summary"]["migration_production_bypass_files"]),
        )
        self.assertTrue(
            set(report["summary"]["migration_production_bypass_files"]).isdisjoint(
                report["summary"]["protected_write_transport_file_keys"]
            )
        )
        self.assertGreater(report["summary"]["production_bypass_files"], 0)
        for key, owners in coverage["owners"].items():
            self.assertEqual(1, len(owners), key)

        invalid = json.loads(json.dumps(config))
        for record in invalid["consumers"]:
            record["finding_files"] = []
        invalid_report = scan_repositories(DEFAULT_REPOSITORIES, consumer_config=invalid)
        self.assertGreater(invalid_report["summary"]["unclassified_production_files"], 0)

        write_unowned = json.loads(json.dumps(config))
        for record in write_unowned["consumers"]:
            if record["migration_status"] == "protected_write_boundary":
                record["finding_files"] = []
        write_coverage = validate_consumer_coverage(write_unowned, report["findings"])
        self.assertEqual(4, len(write_coverage["unprotected_write_transport_files"]))

        write_wrong_status = json.loads(json.dumps(config))
        for record in write_wrong_status["consumers"]:
            if record["migration_status"] == "protected_write_boundary":
                record["migration_status"] = "pending_inventory"
        wrong_status_coverage = validate_consumer_coverage(write_wrong_status, report["findings"])
        self.assertEqual(4, len(wrong_status_coverage["unprotected_write_transport_files"]))

        orphan = json.loads(json.dumps(config))
        orphan["consumers"][0]["finding_files"].append("does-not-exist.py")
        orphan_coverage = validate_consumer_coverage(orphan, report["findings"])
        self.assertIn(
            {"repository": orphan["consumers"][0]["repository"], "file": "does-not-exist.py"},
            orphan_coverage["orphan_owner_claims"],
        )

        missing_entry = json.loads(json.dumps(config))
        missing_entry["consumers"][0]["entry_point"] = "does-not-exist.py"
        missing_coverage = validate_consumer_coverage(missing_entry, report["findings"])
        self.assertIn(
            {"repository": missing_entry["consumers"][0]["repository"], "entry_point": "does-not-exist.py"},
            missing_coverage["missing_entry_points"],
        )

    def test_config_rejects_duplicate_finding_file_owners(self):
        config = load_consumer_config(ROOT / "config" / "data-consumers.v3.json")
        records = [record for record in config["consumers"] if record["finding_files"]]
        self.assertGreaterEqual(len(records), 2)
        invalid = json.loads(json.dumps(config))
        owners_by_repository = {}
        for index, record in enumerate(invalid["consumers"]):
            if record["finding_files"]:
                owners_by_repository.setdefault(record["repository"], []).append(index)
        owner_indexes = next(
            indexes for indexes in owners_by_repository.values() if len(indexes) >= 2
        )
        invalid["consumers"][owner_indexes[1]]["finding_files"] = list(
            invalid["consumers"][owner_indexes[0]]["finding_files"]
        )
        with self.assertRaises(ValueError):
            validate_consumer_config(invalid)

    def test_cli_fail_closed_for_unowned_or_write_transport_findings(self):
        base = {
            "unknown_findings": 0,
            "unclassified_consumers": 0,
            "unclassified_production_files": 0,
            "production_write_transport": 3,
            "write_transport_files": 3,
            "protected_write_transport_files": 3,
            "unprotected_write_transport_files": 0,
            "ambiguous_findings": 0,
            "unprotected_write_transport_files": 0,
            "orphan_owner_claims": 0,
            "missing_entry_points": 0,
            "agent_tooling_scan_error_files": 0,
            "agent_tooling_human_review_files": 0,
        }
        self.assertEqual(0, audit_exit_code(base))
        for field in (
            "unknown_findings",
            "unclassified_consumers",
            "unclassified_production_files",
            "ambiguous_findings",
            "unprotected_write_transport_files",
            "orphan_owner_claims",
            "missing_entry_points",
            "agent_tooling_scan_error_files",
            "agent_tooling_human_review_files",
        ):
            invalid = dict(base)
            invalid[field] = 1
            self.assertEqual(1, audit_exit_code(invalid), field)

    def test_real_inventory_has_eight_repositories_and_binds_agent_aliases(self):
        config = load_consumer_config(ROOT / "config" / "data-consumers.v3.json")
        validate_consumer_config(config)
        self.assertEqual(EXPECTED_REPOSITORIES, set(config["repositories"]))
        self.assertEqual(EXPECTED_REPOSITORIES, {name for name, _ in DEFAULT_REPOSITORIES})

        report = scan_repositories(DEFAULT_REPOSITORIES, consumer_config=config)
        self.assertEqual(EXPECTED_REPOSITORIES, set(report["repositories"]))
        self.assertEqual(1, report["summary"]["unknown_findings"])
        self.assertEqual(1, report["summary"]["agent_tooling_human_review_files"])
        self.assertEqual(1, report["summary"]["agent_tooling_scan_error_files"])
        self.assertEqual(1, audit_exit_code(report["summary"]))
        self.assertEqual(0, report["summary"]["unclassified_consumers"])
        self.assertEqual(0, report["summary"]["unclassified_production_files"])
        self.assertIn("scanner_source_sha256", report["manifest"])
        self.assertIn("consumer_config_sha256", report["manifest"])
        self.assertIn("test_sources_sha256", report["manifest"])
        self.assertEqual("scripts/audit_data_consumers.py", report["manifest"]["source_bindings"]["scanner_source"])
        self.assertEqual(
            hashlib.sha256((ROOT / "scripts/audit_data_consumers.py").read_bytes()).hexdigest(),
            report["manifest"]["scanner_source_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(
                json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            report["manifest"]["consumer_config_sha256"],
        )
        self.assertEqual(
            report["summary"]["production_bypass_files"],
            report["summary"]["covered_production_files"],
        )
        for finding in report["findings"]:
            for field in (
                "repository",
                "realpath",
                "file",
                "line",
                "language",
                "symbol_or_url",
                "classification",
                "deterministic_reason",
                "business_entry_clue",
                "excerpt_sha256",
            ):
                self.assertIn(field, finding)
            self.assertNotIn("source_excerpt", finding)

    def test_consumer_config_requires_complete_non_unknown_records(self):
        config = load_consumer_config(ROOT / "config" / "data-consumers.v3.json")
        records = config["consumers"]
        self.assertGreaterEqual(len(records), len(EXPECTED_REPOSITORIES))
        for record in records:
            with self.subTest(record=record):
                self.assertEqual(set(REQUIRED_CONSUMER_FIELDS), set(record))
                self.assertIn(record["repository"], EXPECTED_REPOSITORIES)
                self.assertNotEqual("unknown", record["migration_status"])
                self.assertNotEqual("unclassified", record["migration_status"])
                self.assertTrue(record["entry_point"])
                self.assertTrue(record["business_shape"])
                self.assertTrue(record["canonical_intent"])
                self.assertTrue(record["rollback_switch"])
                self.assertIsInstance(record["finding_files"], list)

        invalid = json.loads(json.dumps(config))
        invalid["consumers"][0]["migration_status"] = "unknown"
        with self.assertRaises(ValueError):
            validate_consumer_config(invalid)

    def test_audit_outputs_are_machine_verifiable_and_redacted(self):
        config = load_consumer_config(ROOT / "config" / "data-consumers.v3.json")
        validate_consumer_config(config)
        report = scan_repositories(DEFAULT_REPOSITORIES, consumer_config=config)
        output_root = ROOT / "outputs" / "consumer-audit"
        output_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output_root) as temp_dir:
            output_dir = Path(temp_dir) / "consumer-audit" / "20260923T000000+0800"
            write_audit_outputs(report, output_dir)
            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
            findings = json.loads((output_dir / "findings.json").read_text(encoding="utf-8"))
            summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))

        self.assertEqual(report["manifest"]["file_set_sha256"], manifest["file_set_sha256"])
        self.assertEqual(report["summary"], summary)
        serialized = json.dumps(
            {"manifest": manifest, "findings": findings, "summary": summary},
            ensure_ascii=False,
        ).lower()
        self.assertNotIn("source_excerpt", serialized)
        self.assertNotIn("api_key=", serialized)
        self.assertNotIn("access_token=", serialized)
        self.assertNotIn(str(ROOT).lower(), serialized)
        self.assertNotIn("/users/", serialized)
        self.assertEqual("repository://Market_Watch", findings[0]["realpath"])

    def test_audit_output_path_is_repo_scoped_and_rejects_symlinks(self):
        report = {"manifest": {}, "findings": [], "summary": {}}
        with tempfile.TemporaryDirectory() as temp_dir:
            outside = Path(temp_dir) / "outside"
            with self.assertRaises(ValueError):
                write_audit_outputs(report, outside)

            output_root = ROOT / "outputs" / "consumer-audit"
            output_root.mkdir(parents=True, exist_ok=True)
            link_target = Path(temp_dir) / "target"
            link_target.mkdir()
            link = output_root / "fixture-output-link"
            link.symlink_to(link_target, target_is_directory=True)
            try:
                with self.assertRaises(ValueError):
                    write_audit_outputs(report, link)
            finally:
                link.unlink()

            real_parent = output_root / "fixture-real-parent"
            real_parent.mkdir(exist_ok=True)
            intermediate = output_root / "fixture-intermediate-link"
            intermediate.symlink_to(real_parent, target_is_directory=True)
            try:
                with self.assertRaises(ValueError):
                    write_audit_outputs(report, intermediate / "child")
            finally:
                intermediate.unlink()
                real_parent.rmdir()


if __name__ == "__main__":
    unittest.main()
