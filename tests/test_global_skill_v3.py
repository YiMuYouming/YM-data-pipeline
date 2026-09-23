from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import tomllib
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import ym_stock_data
from ym_stock_data.routing import all_route_specs


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_SKILL = ROOT / "skills" / "ym-a-stock-pipeline"
DOCS = (
    ROOT / "README.md",
    ROOT / "docs" / "agent-contract.md",
    ROOT / "docs" / "INSTALL.md",
    ROOT / "docs" / "STOCKTODAY.md",
    ROOT / "docs" / "ACCEPTANCE_RUNBOOK.md",
)
ACTIVE_DOCS_RELATIVE = tuple(path.relative_to(ROOT).as_posix() for path in DOCS)
HISTORICAL_DOC_PREFIXES = (
    "docs/superpowers/",
    "docs/audit/",
    "docs/YM-data-pipeline-2.0-",
)
AGENT_SKILLS = (
    Path("/Users/yimu/.codex/skills/ym-a-stock-pipeline"),
    Path("/Users/yimu/.agents/skills/ym-a-stock-pipeline"),
    Path("/Users/yimu/.claude/skills/ym-a-stock-pipeline"),
    Path("/Users/yimu/.workbuddy/skills/ym-a-stock-pipeline"),
)
BACKUP_PARENT = Path("/Users/yimu/.ym-stock-data/backups/skills")
EXACT_INSTALL_TARGETS = (
    "/Users/yimu/.codex/skills/ym-a-stock-pipeline",
    "/Users/yimu/.agents/skills/ym-a-stock-pipeline",
    "/Users/yimu/.claude/skills/ym-a-stock-pipeline",
    "/Users/yimu/.workbuddy/skills/ym-a-stock-pipeline",
)


def _discover_backup_root() -> Path:
    candidates: list[tuple[datetime, Path]] = []
    if BACKUP_PARENT.is_symlink() or not BACKUP_PARENT.is_dir():
        raise AssertionError(f"backup parent is not a real directory: {BACKUP_PARENT}")

    batch_name = re.compile(r"^\d{8}T\d{6}[+-]\d{4}$")
    for candidate in BACKUP_PARENT.iterdir():
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        if batch_name.fullmatch(candidate.name) is None:
            continue

        manifest_path = candidate / "manifest.md"
        inventory_path = candidate / "inventory.json"
        if (
            manifest_path.is_symlink()
            or inventory_path.is_symlink()
            or not manifest_path.is_file()
            or not inventory_path.is_file()
        ):
            continue

        try:
            directory_time = datetime.strptime(candidate.name, "%Y%m%dT%H%M%S%z")
            manifest = manifest_path.read_text(encoding="utf-8")
            created_match = re.search(r"(?m)^Created:\s*(\S+)\s*$", manifest)
            canonical_match = re.search(
                r"(?ms)^Canonical replacement:\s*\n`([^`]+)`", manifest
            )
            if created_match is None or canonical_match is None:
                continue
            created = datetime.fromisoformat(created_match.group(1))
            if created.tzinfo is None or created.microsecond:
                continue
            if created.strftime("%Y%m%dT%H%M%S%z") != candidate.name:
                continue
            if created != directory_time:
                continue
            if canonical_match.group(1) != str(CANONICAL_SKILL):
                continue

            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            if not isinstance(inventory, dict):
                continue
            if inventory.get("canonical_replacement") != str(CANONICAL_SKILL):
                continue
            if inventory.get("scope") != "four_exact_install_entries":
                continue
            if inventory.get("exact_install_targets") != list(EXACT_INSTALL_TARGETS):
                continue
        except (OSError, TypeError, ValueError, UnicodeError):
            continue

        candidates.append((created, candidate))
    if not candidates:
        raise AssertionError(
            f"no valid backup batch is bound to canonical Skill {CANONICAL_SKILL}"
        )
    return max(candidates, key=lambda item: (item[0], item[1].name))[1]


def _write_selector_candidate(
    parent: Path,
    name: str,
    *,
    created: str,
    manifest_canonical: str = str(CANONICAL_SKILL),
    inventory_canonical: str | None = None,
    scope: str = "four_exact_install_entries",
    exact_install_targets: tuple[str, ...] = EXACT_INSTALL_TARGETS,
) -> Path:
    candidate = parent / name
    candidate.mkdir()
    (candidate / "manifest.md").write_text(
        "\n".join(
            (
                "# test backup",
                f"Created: {created}",
                "Canonical replacement:",
                f"`{manifest_canonical}`",
                "",
            )
        ),
        encoding="utf-8",
    )
    (candidate / "inventory.json").write_text(
        json.dumps(
            {
                "canonical_replacement": (
                    inventory_canonical
                    if inventory_canonical is not None
                    else str(CANONICAL_SKILL)
                ),
                "scope": scope,
                "exact_install_targets": list(exact_install_targets),
            }
        ),
        encoding="utf-8",
    )
    return candidate


BACKUP_ROOT = _discover_backup_root()
BACKUP_INVENTORY = BACKUP_ROOT / "inventory.json"

FORBIDDEN_RUNTIME_PATTERNS = (
    re.compile(r"ym_stock_data\s*\.\s*(?:sources|v2|providers|fetch)\b", re.IGNORECASE),
    re.compile(r"\b(?:fetch|resolve|get_client)\s*\(", re.IGNORECASE),
    re.compile(
        r"\b(?:importlib|import_module|__import__|exec|eval|compile)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b__builtins__\b", re.IGNORECASE),
    re.compile(r"\b(?:tushare|pytdx|akshare|pywencai|easyquotation)\b", re.IGNORECASE),
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\b(?:npx|clawhub)\b", re.IGNORECASE),
    re.compile(r"--(?:token|api-key)\b", re.IGNORECASE),
    re.compile(r"\b(?:api_key|access_token|auth_token|tushare_token)\s*=", re.IGNORECASE),
)

FROM_YM_STOCK_DATA_IMPORT = re.compile(
    r"(?ix)\bfrom[ \t]+ym_stock_data"
    r"(?P<submodule>(?:[ \t]*\.[ \t]*[A-Za-z_]\w*)*)"
    r"[ \t]+import\b"
)
IMPORT_YM_STOCK_DATA = re.compile(
    r"(?ix)\bimport[ \t]+ym_stock_data(?:[ \t]*\.[ \t]*[A-Za-z_]\w*)?"
)
CONTINUED_YM_STOCK_DATA_IMPORT = re.compile(
    r"(?is)\bfrom[ \t]+ym_stock_data.{0,240}\\[ \t]*\r?\n"
)


def _is_exact_public_query_import(markdown: str, match: re.Match[str]) -> bool:
    if match.group("submodule").strip():
        return False

    tail = markdown[match.end() :]
    if re.match(r"[ \t]*\(", tail):
        return False
    query = re.match(r"[ \t]+query\b", tail, re.IGNORECASE)
    if query is None:
        return False

    remainder = tail[query.end() :]
    if re.match(r"[ \t]*(?:\r?\n|$)", remainder):
        return True
    return remainder.startswith("`")


def _forbidden_runtime_matches(markdown: str) -> list[str]:
    runtime_surface = markdown
    matches = []
    matches.extend(
        match.group(0)
        for match in CONTINUED_YM_STOCK_DATA_IMPORT.finditer(runtime_surface)
    )
    for match in FROM_YM_STOCK_DATA_IMPORT.finditer(runtime_surface):
        if not _is_exact_public_query_import(runtime_surface, match):
            matches.append(match.group(0))
    matches.extend(match.group(0) for match in IMPORT_YM_STOCK_DATA.finditer(runtime_surface))
    matches.extend(
        match.group(0)
        for pattern in FORBIDDEN_RUNTIME_PATTERNS
        if (match := pattern.search(runtime_surface)) is not None
    )
    return matches


def _recompute_backup_entries(root: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []

    def visit(path: Path) -> None:
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "target": os.readlink(path),
                }
            )
            return
        if path.is_dir():
            entries.append({"path": relative, "kind": "directory"})
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                visit(child)
            return
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append({"path": relative, "kind": "file", "sha256": digest})
            return
        raise AssertionError(f"unsupported backup entry: {path}")

    for name in ("codex", "agents", "claude", "workbuddy"):
        visit(root / name)
    return entries


def _inventory_entries_sha256(entries: list[dict[str, str]]) -> str:
    payload = json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class GlobalSkillV3Tests(unittest.TestCase):
    def test_repository_skill_is_a_real_progressive_disclosure_entrypoint(self):
        skill_path = CANONICAL_SKILL / "SKILL.md"
        self.assertTrue(skill_path.is_file(), skill_path)
        text = skill_path.read_text(encoding="utf-8")

        self.assertRegex(text, r"(?s)^---.*?\bname:\s*ym-a-stock-pipeline\b.*?---")
        self.assertRegex(text, r"(?m)^description:\s*\S+")
        self.assertIn("from ym_stock_data import query", text)
        self.assertIn("./ym-data", text)
        self.assertIn("stocktoday_data", text)
        self.assertIn("catalog", text.lower())
        self.assertRegex(text.lower(), r"primary|promotion|accepted")
        self.assertIn("source_gap", text)
        self.assertIn("pipeline_version", text)
        self.assertIn("route_policy_version", text)
        self.assertIn("source_tier", text)
        self.assertIn("policy_evidence_sha256", text)
        self.assertIn("查前复权日K", text)
        self.assertIn("adjustment=qfq", text)

        self.assertEqual([], _forbidden_runtime_matches(text))

    def test_markdown_variants_cannot_hide_forbidden_runtime_symbols(self):
        mutations = {
            "backtick_fence": "```python\nfrom ym_stock_data.sources import quotes\n```",
            "tilde_fence": "~~~python\nhttps://vendor.invalid/api\n~~~",
            "indented_block": "    result = fetch(\"quotes\")",
            "inline_code": "Use `ym_stock_data.v2.resolve(\"quotes\")` here.",
            "mixed_root_import": "from ym_stock_data import query, fetch",
            "spaced_dotted_import": "from ym_stock_data . v2 import resolve",
            "aliased_public_import": "from ym_stock_data import query as public_query",
            "parenthesized_public_import": "from ym_stock_data import (query)",
            "multiline_parenthesized_import": (
                "from ym_stock_data import (\n    query,\n)"
            ),
            "semicolon_after_public_import": (
                "from ym_stock_data import query; print(\"side statement\")"
            ),
            "continued_public_import": (
                "from ym_stock_data import query, \\\n    fetch"
            ),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                self.assertNotEqual([], _forbidden_runtime_matches(mutation))

    def test_dynamic_import_and_execution_primitives_cannot_hide_runtime_bypasses(self):
        # This is a conservative static gate for Agent-facing Skill text, not an
        # arbitrary adversarial-obfuscation detector.
        mutations = {
            "split_importlib": (
                'import importlib; importlib.import_module('
                '"ym_stock_data" + ".v2")'
            ),
            "split_dunder_import": (
                '__import__("ym_stock_data" + ".sources")'
            ),
            "dynamic_exec": (
                'exec("__import__(\'ym_stock_data\' + \'.v2\')")'
            ),
            "dynamic_eval": (
                'eval("__import__(\'ym_stock_data\' + \'.v2\')")'
            ),
            "dynamic_compile": (
                'compile("__import__(\'ym_stock_data\' + \'.v2\')", '
                '"<skill>", "exec")'
            ),
            "dynamic_builtins": (
                '__builtins__["__import__"]("ym_stock_data" + ".v2")'
            ),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                self.assertNotEqual([], _forbidden_runtime_matches(mutation))

    def test_skill_covers_every_runtime_route_intent_without_a_hand_copied_list(self):
        text = (CANONICAL_SKILL / "SKILL.md").read_text(encoding="utf-8")
        expected = sorted({spec.intent for spec in all_route_specs()})
        for intent in expected:
            with self.subTest(intent=intent):
                self.assertIn(f"`{intent}`", text)

    def test_public_contract_carries_v3_provenance_and_quality_semantics(self):
        required = (
            "_meta",
            "contract_version",
            "pipeline_version",
            "route_policy_version",
            "source_tier",
            "policy_evidence_sha256",
            "provider_used",
            "attempts",
            "quality",
            "fetched_at",
            "source_gap",
        )
        combined = "\n".join(path.read_text(encoding="utf-8") for path in DOCS)
        for field in required:
            with self.subTest(field=field):
                self.assertIn(field, combined)
        self.assertIn("contract 1.0", combined)
        self.assertRegex(combined.lower(), r"catalog.*(?:not|不).*?(?:primary|promotion|晋级)")
        self.assertRegex(combined.lower(), r"http\s*200.*(?:not|不).*?(?:primary|promotion|晋级)")
        self.assertRegex(combined.lower(), r"non-empty|非空")

    def test_required_docs_use_the_projects_checkout_and_preserve_compatibility_boundary(self):
        canonical = str(ROOT)
        legacy_checkout = "/Users/yimu/Documents/YM_Capital/YM-data-pipeline"
        for path in DOCS:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertIn(canonical, text)
                self.assertNotIn(legacy_checkout, text)
                self.assertIn("compat", text.lower())
        self.assertIn("contract 1.0", (ROOT / "README.md").read_text(encoding="utf-8"))

    def test_active_doc_allowlist_excludes_historical_material(self):
        self.assertEqual(
            (
                "README.md",
                "docs/agent-contract.md",
                "docs/INSTALL.md",
                "docs/STOCKTODAY.md",
                "docs/ACCEPTANCE_RUNBOOK.md",
            ),
            ACTIVE_DOCS_RELATIVE,
        )
        historical = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "docs").rglob("*")
            if path.is_file()
            and any(
                path.relative_to(ROOT).as_posix().startswith(prefix)
                for prefix in HISTORICAL_DOC_PREFIXES
            )
        }
        self.assertTrue(historical)
        self.assertTrue(historical.isdisjoint(ACTIVE_DOCS_RELATIVE))

    def test_official_skill_validator_has_a_reproducible_repo_dev_dependency(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        dev_dependencies = pyproject.get("dependency-groups", {}).get("dev", [])
        self.assertIn("pyyaml==6.0.3", dev_dependencies)
        install_doc = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("quick_validate.py", install_doc)
        self.assertIn("uv run --offline", install_doc)
        self.assertNotIn("PYTHONPATH=", install_doc)

    def test_stocktoday_doc_exposes_only_the_canonical_agent_boundary(self):
        text = (ROOT / "docs" / "STOCKTODAY.md").read_text(encoding="utf-8")
        forbidden = (
            "clawhub",
            "npx",
            "get_client",
            "keychain",
            "service=",
            "account=",
            "tushare_token",
            "stocktoday_token",
            "stocktoday-skill-runtime",
            "fetch()",
            "v2.resolve",
        )
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value.lower(), text.lower())
        self.assertNotRegex(text, r"https?://")
        self.assertIn("./ym-data auth set-stocktoday --stdin", text)
        self.assertIn("./ym-data auth status-stocktoday", text)
        self.assertIn("shared/audits/2026-09-22-stocktoday-native/", text)
        self.assertRegex(text, r"禁止.*Agent.*runtime")

    def test_project_version_is_v3_without_changing_contract_version(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(pyproject, r'(?m)^version\s*=\s*["\']3\.0\.0["\']\s*$')
        self.assertEqual("3.0.0", ym_stock_data.__version__)
        self.assertIn('"contract_version": "1.0"', (ROOT / "README.md").read_text(encoding="utf-8"))

    def test_all_local_agent_entries_resolve_to_the_repository_skill(self):
        for path in AGENT_SKILLS:
            with self.subTest(path=path):
                self.assertTrue(path.is_symlink(), f"not a symlink: {path}")
                self.assertEqual(CANONICAL_SKILL.resolve(), path.resolve())

    def test_workbuddy_backup_is_self_contained_and_restore_safe(self):
        workbuddy = BACKUP_ROOT / "workbuddy"
        agents = BACKUP_ROOT / "agents"
        self.assertTrue(workbuddy.is_symlink())
        self.assertEqual("agents", os.readlink(workbuddy))
        self.assertEqual(agents.resolve(), workbuddy.resolve())
        self.assertTrue((workbuddy / "SKILL.md").is_file())
        manifest = (BACKUP_ROOT / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("Original symlink target", manifest)
        self.assertIn("Normalized restore target", manifest)
        self.assertIn("/Users/yimu/.agents/skills/ym-a-stock-pipeline", manifest)
        self.assertIn("`backup/agents`", manifest)

    def test_backup_inventory_recomputes_the_four_exact_install_entries(self):
        self.assertTrue(BACKUP_INVENTORY.is_file(), BACKUP_INVENTORY)
        inventory = json.loads(BACKUP_INVENTORY.read_text(encoding="utf-8"))

        self.assertEqual(1, inventory["schema_version"])
        self.assertEqual("four_exact_install_entries", inventory["scope"])
        self.assertEqual(str(CANONICAL_SKILL), inventory["canonical_replacement"])
        self.assertEqual(
            {
                "agents",
                "claude",
                "codex",
                "workbuddy",
                "manifest.md",
                "inventory.json",
            },
            {path.name for path in BACKUP_ROOT.iterdir()},
        )
        self.assertEqual(
            [
                "/Users/yimu/.codex/skills/ym-a-stock-pipeline",
                "/Users/yimu/.agents/skills/ym-a-stock-pipeline",
                "/Users/yimu/.claude/skills/ym-a-stock-pipeline",
                "/Users/yimu/.workbuddy/skills/ym-a-stock-pipeline",
            ],
            inventory["exact_install_targets"],
        )
        self.assertIn("does not prove", inventory["scope_note"].lower())
        self.assertEqual(
            {
                "manifest.md": {
                    "included_in_entries": False,
                    "reason": "excluded to avoid metadata hash cycle",
                },
                "inventory.json": {
                    "included_in_entries": False,
                    "reason": "excluded to avoid self-hash cycle",
                },
            },
            inventory["metadata_files"],
        )

        expected = _recompute_backup_entries(BACKUP_ROOT)
        self.assertEqual(expected, inventory["entries"])
        self.assertEqual(_inventory_entries_sha256(expected), inventory["entries_sha256"])

    def test_backup_selector_rejects_invalid_candidates_and_chooses_latest_aware_time(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir)
            older_valid = _write_selector_candidate(
                parent,
                "20260923T010000+0800",
                created="2026-09-23T01:00:00+08:00",
            )
            newer_valid = _write_selector_candidate(
                parent,
                "20260922T200000+0000",
                created="2026-09-22T20:00:00+00:00",
            )
            _write_selector_candidate(
                parent,
                "20260924T010000+0800",
                created="2026-09-23T01:00:00+08:00",
            )
            _write_selector_candidate(
                parent,
                "20260925T010000+0800",
                created="2026-09-25T01:00:00+08:00",
                manifest_canonical="/wrong/canonical/skill",
            )
            _write_selector_candidate(
                parent,
                "20260926T010000+0800",
                created="2026-09-26T01:00:00+08:00",
                scope="wrong_scope",
            )
            (parent / "not-a-batch").mkdir()
            os.symlink(older_valid, parent / "20260927T010000+0800")

            with mock.patch(__name__ + ".BACKUP_PARENT", parent):
                selected = _discover_backup_root()

        self.assertEqual(newer_valid.name, selected.name)
        self.assertNotEqual(older_valid.name, selected.name)


if __name__ == "__main__":
    unittest.main()
