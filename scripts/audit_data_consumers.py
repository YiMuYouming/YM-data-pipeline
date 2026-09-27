#!/usr/bin/env python3
"""Deterministic, read-only inventory of YM-data consumer repositories.

The scanner intentionally does not execute consumer code, consult ``.gitignore``,
use the network, or resolve provider credentials.  It inventories source and
configuration text so that migration decisions can be made from a reproducible
machine-readable report.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent

DEFAULT_REPOSITORIES: tuple[tuple[str, Path], ...] = tuple(
    (name, PROJECT_ROOT / name)
    for name in (
        "Market_Watch",
        "live-dashboard",
        "YiMu_IR",
        "YiMu_Quant",
        "YiMu_Intelligence",
        "live-trading",
        "portal",
        "ai-rule-system",
    )
)

REQUIRED_CONSUMER_FIELDS = (
    "repository",
    "entry_point",
    "business_shape",
    "canonical_intent",
    "freshness_requirement",
    "current_source",
    "target_policy",
    "rollback_switch",
    "migration_status",
    "evidence_refs",
    "finding_files",
)
REQUIRED_AGENT_TOOLING_FIELDS = ("repository", "path", "channel", "classification", "owner_policy")
REQUIRED_AGENT_ALIAS_FIELDS = (
    "repository",
    "path",
    "expected_relative_target",
    "target_tree_sha256",
)
REQUIRED_AGENT_CONTENT_BINDING_FIELDS = (
    "repository",
    "path",
    "content_sha256",
    "classification",
    "owner_policy",
)

SCHEMA_VERSION = "ym-data-pipeline.consumer-inventory.v3"
SCANNER_VERSION = "consumer-audit-v3.5"
MAX_TEXT_BYTES = 2_000_000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

AGENT_TOOLING_CHANNEL_ROOTS = (".agents/skills", ".claude/skills", ".workbuddy/skills")
AGENT_TOOLING_IGNORED_DIRECTORIES = frozenset({"memory", "cache", "download", "downloads"})
AGENT_SCRIPT_SUFFIXES = frozenset(
    {".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".sh", ".bash", ".zsh", ".fish"}
)
_PYTHON_ANALYSIS_CACHE: dict[str, dict[int, list[tuple[str, str, str]]]] = {}
_PYTHON_ANALYSIS_CACHE_LIMIT = 4096
_TEXT_MATCH_CACHE: dict[tuple[str, str], tuple[tuple[int, str, str, str, str], ...]] = {}
_TEXT_MATCH_CACHE_LIMIT = 8192
_REPOSITORY_SCAN_CACHE: dict[str, tuple[str, list[dict[str, Any]], dict[str, Any]]] = {}

# These are tooling/dependency trees.  They are recorded in the manifest, but
# are not silently used as a way to hide source files under an application
# directory.  Generated, data, archive, and report paths remain scannable and
# are classified below.
EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".playwright-cli",
        ".claude",
        ".agents",
        ".workbuddy",
    }
)

SUPPORTED_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".yaml",
        ".yml",
        ".json",
        ".jsonl",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".env.example",
        ".html",
        ".htm",
        ".jinja",
        ".j2",
        ".md",
        ".txt",
        ".sql",
        ".xml",
    }
)

LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".fish": "shell",
    ".yaml": "config",
    ".yml": "config",
    ".json": "config",
    ".jsonl": "config",
    ".toml": "config",
    ".ini": "config",
    ".cfg": "config",
    ".conf": "config",
    ".env.example": "config",
    ".html": "template",
    ".htm": "template",
    ".jinja": "template",
    ".j2": "template",
    ".md": "document",
    ".txt": "document",
    ".sql": "sql",
    ".xml": "markup",
}

GENERATED_MARKERS = frozenset(
    {
        "generated",
        "out",
        "output",
        "outputs",
        "artifact",
        "artifacts",
        "report",
        "reports",
        "deliverable",
        "deliverables",
        "tmp",
        "compiled",
        "dist",
        "build",
    }
)
ARCHIVE_MARKERS = frozenset(
    {"archive", "_archive", "deleted", "待删除", "backup"}
)
WEAK_RUNTIME_MARKERS = frozenset({"data", "runtime", "history", "legacy", "skills", "notes"})
EXECUTABLE_SOURCE_SUFFIXES = frozenset(
    {".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".sh", ".bash", ".zsh", ".fish"}
)
TEST_MARKERS = frozenset({"test", "tests"})
FIXTURE_MARKERS = frozenset({"fixture", "fixtures"})
PROVIDER_INTERNAL_MARKERS = frozenset(
    {"provider", "providers", "source", "sources", "adapter", "adapters", "vendor", "vendors"}
)
DOCUMENTATION_MARKERS = frozenset(
    {
        "doc",
        "docs",
        "documentation",
        "plans",
        "plan",
        "audits",
        "audit",
        "superpowers",
        "brainstorm",
    }
)

DEPENDENCY_FILENAMES = frozenset(
    {
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "requirements.txt",
        "requirements-dev.txt",
        "uv.lock",
        "poetry.lock",
        "pipfile",
        "pipfile.lock",
        "skills-lock.json",
    }
)

KNOWN_PROVIDER_TOKENS = (
    "tushare",
    "pytdx",
    "akshare",
    "pywencai",
    "easyquotation",
    "iwencai",
    "wind",
    "stocktoday",
    "eastmoney",
    "10jqka",
    "tonghuashun",
    "sina",
    "gtimg",
    "westock",
    "workbuddy",
    "tencent",
)

KNOWN_VENDOR_DOMAINS = (
    "tushare.pro",
    "eastmoney.com",
    "10jqka.com.cn",
    "iwencai.com",
    "stocktoday.cn",
    "sina.com.cn",
    "gtimg.cn",
    "qq.com",
    "wind.com.cn",
)

PROVIDER_PACKAGE_NAMES = frozenset(
    {
        "tushare",
        "pytdx",
        "akshare",
        "pywencai",
        "easyquotation",
        "stocktoday",
        "westock",
        "workbuddy",
        "mcp",
        "wind",
        "windpy",
    }
)

URL_RE = re.compile(r"https?://[^\s\"'<>`]+", re.IGNORECASE)
PRIVATE_IMPORT_RE = re.compile(
    r"\b(?:from\s+ym_stock_data\s*(?:\.\s*[A-Za-z_]\w*)+\s+import|"
    r"import\s+ym_stock_data\s*(?:\.\s*[A-Za-z_]\w*)+)",
    re.IGNORECASE,
)
ROOT_IMPORT_RE = re.compile(
    r"\bfrom\s+ym_stock_data\s+import\s+([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)",
    re.IGNORECASE,
)
PRIVATE_ACCESS_RE = re.compile(
    r"\bym_stock_data\s*\.\s*(?:v2|sources|providers|fetch|resolve)\b", re.IGNORECASE
)
PROVIDER_IMPORT_RE = re.compile(
    r"(?:\b(?:from|import)\s+(?P<python_package>"
    + "|".join(re.escape(package) for package in sorted(PROVIDER_PACKAGE_NAMES))
    + r")\b|\b(?:require|import)\s*\(\s*[\"'](?P<js_package>"
    + "|".join(re.escape(package) for package in sorted(PROVIDER_PACKAGE_NAMES))
    + r")[.\"'])",
    re.IGNORECASE,
)
PROVIDER_TOKEN_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(token) for token in KNOWN_PROVIDER_TOKENS) + r")\b",
    re.IGNORECASE,
)
HTTP_CLIENT_RE = re.compile(
    r"(?:\brequests?\s*\.\s*(?:get|post|put|patch|request|Session)\b|"
    r"\bhttpx\b|\baiohttp\b|\burllib(?:\.[A-Za-z_]\w*)*\.request\b|"
    r"\burlopen\s*\(|\baxios\b|\bfetch\s*\(|(?:^|\s)(?:curl|wget)\s+)",
    re.IGNORECASE,
)
HTTP_CALL_RE = re.compile(
    r"(?:\brequests?\s*\.\s*(?:get|post|put|patch|delete|request)\s*\(|"
    r"\bhttpx\s*\.\s*(?:get|post|put|patch|delete|request)\s*\(|"
    r"\baiohttp\b[^\n]{0,120}\.(?:get|post|put|patch|delete)\s*\(|"
    r"\burlopen\s*\(|\baxios\s*\.\s*(?:get|post|put|patch|delete|request)\s*\(|"
    r"\bfetch\s*\(|(?:^|\s)(?:curl|wget)\s+)",
    re.IGNORECASE,
)
HTTP_WRITE_RE = re.compile(
    r"(?:\brequests?\s*\.\s*(?:post|put|patch|delete)\s*\(|"
    r"\bhttpx\s*\.\s*(?:post|put|patch|delete)\s*\(|"
    r"\b(?:requests?|httpx)\s*\.\s*request\s*\(\s*[\"']\s*(?:POST|PUT|PATCH|DELETE)\b|"
    r"\baxios\s*\.\s*(?:post|put|patch|delete)\s*\(|"
    r"\bmethod\s*[:=]\s*[\"']\s*(?:POST|PUT|PATCH|DELETE)\b|"
    r"\b-X\s*(?:POST|PUT|PATCH|DELETE)\b|"
    r"\b--request\s+(?:POST|PUT|PATCH|DELETE)\b|"
    r"\b(?:POST|PUT|PATCH|DELETE)\b[^\n]{0,30}\b(?:urlopen|fetch|curl)\b)",
    re.IGNORECASE,
)
INTERNAL_API_RE = re.compile(
    r"(?:['\"`]\s*/api(?:/|['\"`])|\b(?:127\.0\.0\.1|localhost)(?::\d+)?\b|\b8088\b)",
    re.IGNORECASE,
)
FALLBACK_RE = re.compile(
    r"(?:\b(?:fallback|failover|provider_order|provider_chain|retry|backoff)\b\s*[:=(]|"
    r"\b(?:fallback|failover|provider_order|provider_chain|retry|backoff)_(?:from|to|providers?)\b\s*[:=])",
    re.IGNORECASE,
)
DIRECT_MCP_RE = re.compile(
    r"(?:\b(?:from|import)\s+(?:workbuddy|westock|mcp)\b|"
    r"\b(?:mcp_call|workbuddy\s*\.|westock\s*\.|mcp\s*\.|"
    r"tdx_(?:quotes|screener|kline)\s*\(|TdxHq_API\s*\(|"
    r"tencent\s*\.\s*(?:quote|quotes|kline|screener|fetch|query|call|request|api|market|stock)[A-Za-z_]*\s*\())",
    re.IGNORECASE,
)
QUICK_SCAN_MARKER_RE = re.compile(
    r"(?i)(?:ym_stock_data|requests?|httpx|aiohttp|urllib|urlopen|axios|fetch|curl|wget|"
    + "|".join(re.escape(token) for token in KNOWN_PROVIDER_TOKENS)
    + r"|tdx|mcp|fallback|failover|provider_order|provider_chain|retry|backoff|https?://)"
)

APPROVED_COMPATIBILITY_RULES = {
    "live-dashboard": {
        "scripts/ym_data_query.py": {
            "match_kinds": frozenset({"private_import", "private_access"}),
            "purpose": "legacy YM-data compatibility wrapper",
        },
        "config/sources.yaml": {
            "match_kinds": frozenset({"private_import", "private_access"}),
            "purpose": "legacy runtime source declaration",
        },
    }
}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return _sha256_bytes(encoded)


def _language(path: Path) -> str | None:
    name = path.name.lower()
    if name.endswith(".env.example"):
        return "config"
    return LANGUAGE_BY_SUFFIX.get(path.suffix.lower())


def _is_supported_text(path: Path, raw: bytes) -> bool:
    if _language(path) is not None:
        return True
    if len(raw) > 4096 or b"\x00" in raw:
        return False
    first_line = raw.splitlines()[0] if raw.splitlines() else b""
    return first_line.startswith(b"#!")


def _path_classification(relative_path: str) -> str:
    parts = [part.lower() for part in Path(relative_path).parts]
    name = parts[-1] if parts else ""
    stem = Path(name).stem.lower()
    if any(part in TEST_MARKERS for part in parts) or name.startswith("test_") or name.endswith("_test.py"):
        return "test"
    if any(part in FIXTURE_MARKERS for part in parts):
        return "fixture"
    if any(part in ARCHIVE_MARKERS for part in parts) or any(
        stem.startswith(marker)
        for marker in ("archive_", "backup_")
    ):
        return "archive"
    if any(part in GENERATED_MARKERS for part in parts) or any(
        marker in name for marker in ("generated", "snapshot", "manifest")
    ):
        return "generated"
    if (
        name in DEPENDENCY_FILENAMES
        or name.endswith(".lock")
        or "skills-lock" in name
        or ("package" in parts and Path(relative_path).suffix.lower() in {".json", ".yaml", ".yml", ".toml"})
    ):
        return "dependency_metadata"
    suffix = Path(relative_path).suffix.lower()
    if any(part in WEAK_RUNTIME_MARKERS for part in parts) and suffix not in EXECUTABLE_SOURCE_SUFFIXES:
        if suffix in {".md", ".txt"}:
            return "documentation"
        return "generated"
    if (
        any(part in DOCUMENTATION_MARKERS for part in parts)
        or name in {"readme.md", "agents.md", "changelog.md", "license.md"}
        or (suffix in {".md", ".txt"} and name not in {"template.md"})
        or (suffix == ".html" and any(part in DOCUMENTATION_MARKERS for part in parts))
    ):
        return "documentation"
    return "production"


def _relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _sanitize_url(value: str) -> str:
    value = value.rstrip(".,;:)]}>")
    try:
        parsed = urlsplit(value)
    except ValueError:
        malformed = re.match(r"(?i)^(https?)://([^/\\s?#<>`]+)", value)
        if not malformed:
            return "redacted://invalid-url"
        authority = malformed.group(2).rsplit("@", 1)[-1]
        host_match = re.match(r"[A-Za-z0-9._-]+", authority.strip("[]"))
        host = host_match.group(0) if host_match else "invalid-host"
        return f"{malformed.group(1).lower()}://{host}"
    if not parsed.scheme or not parsed.netloc:
        return value
    # Deliberately drop query/fragment and credentials.  The report needs a
    # stable endpoint clue, never a token-bearing URL.
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        # A prose/document URL may be followed by non-URL text without a
        # whitespace boundary.  Keep only the safe hostname in that case.
        port = None
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), host, parsed.path, "", ""))


def _business_entry_clue(relative_path: str, text: str, line_number: int) -> str:
    lines = text.splitlines()
    start = max(0, line_number - 4)
    end = min(len(lines), line_number + 3)
    window = "\n".join(lines[start:end])
    clues = [f"module:{relative_path}"]
    functions = re.findall(r"\b(?:def|async\s+def|function)\s+([A-Za-z_]\w*)", window)
    classes = re.findall(r"\bclass\s+([A-Za-z_]\w*)", window)
    entries = re.findall(r"\b(?:entry_point|business_entry)\s*[:=]\s*['\"]([A-Za-z0-9_.:/-]{1,80})['\"]", window)
    if functions:
        clues.append(f"function:{_safe_identifier(functions[-1])}")
    if classes:
        clues.append(f"class:{_safe_identifier(classes[-1])}")
    if entries:
        clues.append(f"entry:{_safe_identifier(entries[-1])}")
    if "__main__" in window:
        clues.append("entry:__main__")
    return ";".join(clues)


def _safe_identifier(value: str) -> str:
    if re.search(r"(?:api[_-]?key|access[_-]?token|secret|password|authorization|bearer|credential)", value, re.IGNORECASE):
        return "redacted_identifier"
    return value[:80]


def _excerpt_hash(text: str, line_number: int) -> str:
    lines = text.splitlines()
    start = max(0, line_number - 2)
    end = min(len(lines), line_number + 1)
    return _sha256_bytes("\n".join(lines[start:end]).encode("utf-8", errors="replace"))


def _finding(
    *,
    repository: str,
    repository_realpath: str,
    file: str,
    file_realpath: str,
    line: int,
    language: str,
    symbol_or_url: str,
    match_kind: str,
    classification: str,
    deterministic_reason: str,
    business_entry_clue: str,
    excerpt_sha256: str,
    excerpt_line_window: str,
) -> dict[str, Any]:
    return {
        "repository": repository,
        "realpath": repository_realpath,
        "file": file,
        "file_realpath": file_realpath,
        "line": line,
        "language": language,
        "symbol_or_url": symbol_or_url,
        "match_kind": match_kind,
        "classification": classification,
        "deterministic_reason": deterministic_reason,
        "business_entry_clue": business_entry_clue,
        "excerpt_sha256": excerpt_sha256,
        "excerpt_line_window": excerpt_line_window,
    }


def _compatibility_rule(repository: str, relative_path: str, match_kind: str) -> Mapping[str, Any] | None:
    rule = APPROVED_COMPATIBILITY_RULES.get(repository, {}).get(relative_path)
    if rule and match_kind in rule["match_kinds"]:
        return rule
    return None


def _compatibility_rules_manifest() -> dict[str, dict[str, dict[str, Any]]]:
    return {
        repository: {
            relative_path: {
                "match_kinds": sorted(rule["match_kinds"]),
                "purpose": rule["purpose"],
            }
            for relative_path, rule in rules.items()
        }
        for repository, rules in APPROVED_COMPATIBILITY_RULES.items()
    }


def _known_vendor_host(value: str) -> str:
    try:
        host = (urlsplit(value).hostname or "").lower()
    except ValueError:
        return ""
    return host


def _is_known_vendor_host(host: str) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in KNOWN_VENDOR_DOMAINS)


def _context_has_known_vendor_url(context: str) -> bool:
    for match in URL_RE.finditer(context):
        if _is_known_vendor_host(_known_vendor_host(_sanitize_url(match.group(0)))):
            return True
    return False


def _context_is_internal(context: str) -> bool:
    return bool(INTERNAL_API_RE.search(context))


def _context_is_write(context: str) -> bool:
    return bool(HTTP_WRITE_RE.search(context))


def _strip_inline_comments(lines: Sequence[str]) -> str:
    cleaned: list[str] = []
    for line in lines:
        line = re.sub(r"(?<!:)//.*$", "", line)
        line = re.sub(r"\s+#.*$", "", line)
        cleaned.append(line)
    return "\n".join(cleaned)


def _ast_dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _ast_dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _python_match(
    matches: dict[int, list[tuple[str, str, str]]],
    line_number: int,
    match_kind: str,
    symbol: str,
    reason: str,
) -> None:
    key = (match_kind, symbol, reason)
    bucket = matches.setdefault(line_number, [])
    if key not in bucket:
        bucket.append(key)


def _python_string_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # Preserve literal portions of an f-string.  Dynamic substitutions do
        # not need to be evaluated for import detection; the executable string
        # boundary is established separately by the exec/subprocess analysis.
        return "".join(
            part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


def _python_static_matches(text: str) -> tuple[dict[int, list[tuple[str, str, str]]], ast.AST | None]:
    matches: dict[int, list[tuple[str, str, str]]] = {}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return matches, None

    string_constants: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = _python_string_value(node.value)
            if value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        string_constants[target.id] = value
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.name.lower().startswith("ym_stock_data"):
                    aliases[alias.asname] = alias.name.lower()
        elif isinstance(node, ast.ImportFrom) and (node.module or "").lower().startswith("ym_stock_data"):
            module = (node.module or "").lower()
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                aliases[bound] = f"{module}.{alias.name}" if module else alias.name

    for node in ast.walk(tree):
        line_number = int(getattr(node, "lineno", 1))
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name.lower()
                if module.startswith("ym_stock_data."):
                    _python_match(
                        matches,
                        line_number,
                        "private_import",
                        alias.name,
                        "private ym_stock_data submodule import",
                    )
                elif module.split(".", 1)[0] in PROVIDER_PACKAGE_NAMES:
                    _python_match(
                        matches,
                        line_number,
                        "provider_import",
                        module.split(".", 1)[0],
                        "direct provider package import or SDK reference",
                    )
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").lower()
            if module == "ym_stock_data" or module.startswith("ym_stock_data."):
                imported_names = [alias.name for alias in node.names]
                noncanonical = [name for name in imported_names if name != "query"]
                if module != "ym_stock_data" or noncanonical:
                    symbol = f"from {node.module} import {','.join(sorted(noncanonical or imported_names))}"
                    _python_match(
                        matches,
                        line_number,
                        "private_import",
                        symbol,
                        "private ym_stock_data submodule import"
                        if module != "ym_stock_data"
                        else "non-canonical root ym_stock_data import; only public query is allowed",
                    )
            elif module.split(".", 1)[0] in PROVIDER_PACKAGE_NAMES:
                _python_match(
                    matches,
                    line_number,
                    "provider_import",
                    module.split(".", 1)[0],
                    "direct provider package import or SDK reference",
                )
        elif isinstance(node, ast.Attribute):
            dotted = (_ast_dotted_name(node) or "").lower()
            resolved = dotted
            for alias, module in aliases.items():
                if dotted == alias or dotted.startswith(alias + "."):
                    resolved = module + dotted[len(alias) :]
                    break
            if re.match(r"^ym_stock_data\.(?:v2|sources|providers|fetch|resolve)(?:\.|$)", resolved):
                _python_match(
                    matches,
                    line_number,
                    "private_access",
                    resolved,
                    "private ym_stock_data submodule/access",
                )
        elif isinstance(node, ast.Call):
            dotted = (_ast_dotted_name(node.func) or "").lower()
            if dotted.rsplit(".", 1)[-1] in {"tdxhq_api", "mcp_call"} or dotted.startswith(
                ("tencent.", "workbuddy.", "westock.", "mcp.")
            ):
                _python_match(
                    matches,
                    line_number,
                    "direct_mcp_or_workbuddy",
                    dotted,
                    "direct WorkBuddy/Tencent/TDX/MCP runtime call",
                )
            if dotted.rsplit(".", 1)[-1] in {"import_module", "__import__"}:
                target: str | None = None
                if node.args:
                    target = _python_string_value(node.args[0])
                    if target is None and isinstance(node.args[0], ast.Name):
                        target = string_constants.get(node.args[0].id)
                if target is None:
                    _python_match(
                        matches,
                        line_number,
                        "ambiguous_dynamic_import",
                        dotted,
                        "executable dynamic import has no statically known module; human review required",
                    )
                else:
                    module = target.lower().strip()
                    root = module.split(".", 1)[0]
                    if module == "ym_stock_data" or module.startswith("ym_stock_data."):
                        if module != "ym_stock_data":
                            _python_match(
                                matches,
                                line_number,
                                "private_import",
                                module,
                                "private ym_stock_data submodule loaded through executable dynamic import",
                            )
                    elif root in PROVIDER_PACKAGE_NAMES:
                        _python_match(
                            matches,
                            line_number,
                            "provider_import",
                            root,
                            "provider package loaded through executable dynamic import",
                        )
    return matches, tree


def _python_dynamic_string_matches(
    text: str,
    tree: ast.AST | None,
) -> dict[int, list[tuple[str, str, str]]]:
    matches: dict[int, list[tuple[str, str, str]]] = {}
    if tree is None:
        return matches

    assignments: dict[str, tuple[ast.AST, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        content = _python_string_value(value)
        if content is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                assignments[target.id] = (value, content)

    executed_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = (_ast_dotted_name(node.func) or "").lower()
        if dotted.rsplit(".", 1)[-1] in {"exec", "eval"} and node.args:
            if isinstance(node.args[0], ast.Name):
                executed_names.add(node.args[0].id)
        if dotted in {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "subprocess.popen"}:
            literals = {
                value.value
                for value in ast.walk(node)
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            }
            if "-c" in literals:
                for name in assignments:
                    if any(isinstance(value, ast.Name) and value.id == name for value in ast.walk(node)):
                        executed_names.add(name)

    lines = text.splitlines()
    for name in executed_names:
        if name not in assignments:
            continue
        value_node, _content = assignments[name]
        start = int(getattr(value_node, "lineno", 1))
        end = int(getattr(value_node, "end_lineno", start))
        for line_number in range(start, min(end, len(lines)) + 1):
            line = lines[line_number - 1]
            for match in PRIVATE_IMPORT_RE.finditer(line):
                _python_match(
                    matches,
                    line_number,
                    "private_import",
                    match.group(0).strip(),
                    "private ym_stock_data import in an executable Python string",
                )
            root_import = ROOT_IMPORT_RE.search(line)
            if root_import:
                imported_names = [name.strip() for name in root_import.group(1).split(",")]
                noncanonical = [name for name in imported_names if name != "query"]
                if noncanonical:
                    _python_match(
                        matches,
                        line_number,
                        "private_import",
                        "from ym_stock_data import " + ",".join(sorted(noncanonical)),
                        "non-canonical root ym_stock_data import in an executable Python string",
                    )
            for match in PROVIDER_IMPORT_RE.finditer(line):
                package = match.group("python_package") or match.group("js_package") or "provider"
                _python_match(
                    matches,
                    line_number,
                    "provider_import",
                    package.lower(),
                    "direct provider package import in an executable Python string",
                )
    return matches


def _empty_url_state() -> dict[str, Any]:
    return {"known": set(), "unknown": False, "internal": False}


def _merge_url_state(*states: Mapping[str, Any]) -> dict[str, Any]:
    merged = _empty_url_state()
    for state in states:
        merged["known"].update(state.get("known", set()))
        merged["unknown"] = bool(merged["unknown"] or state.get("unknown"))
        merged["internal"] = bool(merged["internal"] or state.get("internal"))
    return merged


def _string_url_state(value: str) -> dict[str, Any]:
    state = _empty_url_state()
    for match in URL_RE.finditer(value):
        url = _sanitize_url(match.group(0))
        if _is_known_vendor_host(_known_vendor_host(url)):
            state["known"].add(url)
    if re.match(r"(?i)^\s*/api(?:/|$)", value) or _context_is_internal(value):
        state["internal"] = True
    if not state["known"] and not state["internal"] and re.search(
        r"(?i)(?:https?://|/api(?:/|$)|\b(?:url|uri|endpoint)\b)", value
    ):
        state["unknown"] = True
    return state


def _load_names(node: ast.AST) -> set[str]:
    return {
        value.id
        for value in ast.walk(node)
        if isinstance(value, ast.Name) and isinstance(value.ctx, ast.Load)
    }


def _eval_url_state(node: ast.AST, env: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _string_url_state(node.value)
    if isinstance(node, ast.Name):
        return dict(env.get(node.id, _empty_url_state()))
    if isinstance(node, ast.JoinedStr):
        states = []
        literal = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                literal.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                states.append(_eval_url_state(value.value, env))
        states.append(_string_url_state("".join(literal)))
        return _merge_url_state(*states)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _merge_url_state(_eval_url_state(node.left, env), _eval_url_state(node.right, env))
    if isinstance(node, ast.BoolOp):
        return _merge_url_state(*(_eval_url_state(value, env) for value in node.values))
    if isinstance(node, ast.IfExp):
        return _merge_url_state(_eval_url_state(node.body, env), _eval_url_state(node.orelse, env))
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return _merge_url_state(*(_eval_url_state(value, env) for value in node.elts))
    if isinstance(node, ast.Subscript):
        state = _eval_url_state(node.value, env)
        if not state["known"] and not state["internal"]:
            dotted = (_ast_dotted_name(node.value) or "").lower()
            if re.search(r"(?:url|uri|endpoint)", dotted):
                state["unknown"] = True
        return state
    if isinstance(node, ast.Call):
        state = _merge_url_state(
            *[_eval_url_state(argument, env) for argument in node.args],
            *[_eval_url_state(keyword.value, env) for keyword in node.keywords],
        )
        if not state["known"] and not state["internal"] and not state["unknown"]:
            dotted = (_ast_dotted_name(node.func) or "").lower()
            if re.search(r"(?:url|uri|endpoint)", dotted) and dotted.rsplit(".", 1)[-1] not in {
                "urlopen",
                "request",
            }:
                state["unknown"] = True
        return state
    return _empty_url_state()


def _is_http_transport_call(node: ast.Call) -> bool:
    raw_dotted = _ast_dotted_name(node.func) or ""
    dotted = raw_dotted.lower()
    last = dotted.rsplit(".", 1)[-1]
    raw_last = raw_dotted.rsplit(".", 1)[-1]
    if raw_dotted.startswith("urllib.") and raw_last == "Request":
        # ``urllib.request.Request`` is a request-object constructor rather
        # than an arbitrary ``obj.request`` method.  It carries the target
        # and, when supplied, the HTTP method/data used by the later
        # ``urlopen`` call.
        return True
    if raw_last == "request":
        # A generic ``obj.request(...)`` is not enough evidence of an HTTP
        # transport: ordinary SDK/domain objects use that method name too.
        # Keep the bare imported-function form and known HTTP client/module
        # namespaces, while excluding constructors such as
        # ``urllib.request.Request(...)``.
        return dotted == "request" or dotted.startswith(
            (
                "requests.",
                "httpx.",
                "aiohttp.",
                "urllib3.",
                "httplib2.",
                "http.client.",
            )
        )
    if raw_last == "urlopen":
        return dotted == "urlopen" or dotted.startswith("urllib.")
    if last == "fetch":
        return True
    if last in {"get", "post", "put", "patch", "delete"}:
        return any(token in dotted for token in ("requests.", "httpx.", "aiohttp.", "axios."))
    return False


def _function_transport_params(function: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    parameters = [argument.arg for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)]
    dependencies: dict[str, set[str]] = {name: {name} for name in parameters}
    transported: set[str] = set()
    all_nodes = list(ast.walk(function))
    load_names_by_id: dict[int, set[str]] = {}
    for node in reversed(all_nodes):
        names: set[str] = set()
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.add(node.id)
        for child in ast.iter_child_nodes(node):
            names.update(load_names_by_id.get(id(child), set()))
        load_names_by_id[id(node)] = names
    nodes = sorted(
        (
            node
            for node in all_nodes
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        ),
        key=lambda node: (int(getattr(node, "lineno", 0)), int(getattr(node, "col_offset", 0))),
    )
    for node in nodes:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            source_names = load_names_by_id.get(id(node.value), set())
            sources = set().union(*(dependencies.get(name, set()) for name in source_names))
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    dependencies[target.id] = set(sources)
        elif isinstance(node, ast.Call) and _is_http_transport_call(node):
            for argument in (*node.args, *(keyword.value for keyword in node.keywords)):
                sources = set().union(
                    *(dependencies.get(name, set()) for name in load_names_by_id.get(id(argument), set()))
                )
                transported.update(sources.intersection(parameters))
    return tuple(name for name in parameters if name in transported)


def _python_vendor_transport_matches(text: str) -> dict[int, list[tuple[str, str, str]]]:
    matches: dict[int, list[tuple[str, str, str]]] = {}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return matches

    helpers: dict[str, tuple[str, ...]] = {}
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = _function_transport_params(node)
            if params:
                helpers[node.name] = params
            functions[node.name] = node

    def target_names(target: ast.AST) -> list[str]:
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, (ast.Tuple, ast.List)):
            names: list[str] = []
            for item in target.elts:
                names.extend(target_names(item))
            return names
        return []

    def add_transport_result(line_number: int, state: Mapping[str, Any], reason: str) -> None:
        if state.get("known"):
            symbol = sorted(state["known"])[0]
            _python_match(matches, line_number, "known_vendor_transport", symbol, reason)
        elif state.get("unknown") and not state.get("internal"):
            _python_match(
                matches,
                line_number,
                "ambiguous_vendor_transport",
                "unresolved_same_file_url",
                "same-file URL expression reaches an HTTP helper but its vendor identity is unresolved; human review required",
            )

    def process_scope(body: Sequence[ast.stmt], initial_env: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        env: dict[str, Mapping[str, Any]] = {name: dict(state) for name, state in (initial_env or {}).items()}
        aliases: dict[str, set[str]] = {}
        nodes = sorted(
            (
                node
                for root in body
                for node in ast.walk(root)
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            ),
            key=lambda node: (int(getattr(node, "lineno", 0)), int(getattr(node, "col_offset", 0))),
        )
        for node in nodes:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                state = _eval_url_state(node.value, env)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in target_names(target):
                        adjusted = dict(state)
                        if not adjusted["known"] and not adjusted["internal"] and not adjusted["unknown"] and re.search(
                            r"(?i)(?:url|uri|endpoint)", name
                        ):
                            adjusted["unknown"] = True
                        env[name] = adjusted
                if isinstance(node.value, ast.BoolOp) and isinstance(node.value.op, ast.Or):
                    candidates = [
                        _ast_dotted_name(value)
                        for value in node.value.values
                        if isinstance(value, (ast.Name, ast.Attribute))
                    ]
                    for target in targets:
                        for name in target_names(target):
                            aliases[name] = {candidate for candidate in candidates if candidate}
            elif isinstance(node, ast.Call):
                if _is_http_transport_call(node):
                    for argument in (*node.args, *(keyword.value for keyword in node.keywords)):
                        add_transport_result(
                            int(getattr(node, "lineno", 1)),
                            _eval_url_state(argument, env),
                            "known vendor URL reaches an HTTP transport through same-file Python dataflow",
                        )
                target = _ast_dotted_name(node.func)
                candidates = set(aliases.get(target or "", set()))
                if target:
                    candidates.add(target)
                for candidate in candidates:
                    parameters = helpers.get(candidate)
                    if not parameters:
                        continue
                    for index, parameter in enumerate(parameters):
                        argument: ast.AST | None = None
                        if index < len(node.args):
                            argument = node.args[index]
                        for keyword in node.keywords:
                            if keyword.arg == parameter:
                                argument = keyword.value
                        if argument is not None:
                            add_transport_result(
                                int(getattr(node, "lineno", 1)),
                                _eval_url_state(argument, env),
                                "known vendor URL reaches a same-file HTTP helper through Python dataflow",
                            )

    # Top-level assignments and calls are enough for the common producer shape.
    process_scope(tree.body)
    # Also inspect each function as a caller.  Seed it with the module-level
    # constants so a function can use a file-local vendor base without a broad
    # file-level bypass exemption.
    module_env: dict[str, Mapping[str, Any]] = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            state = _eval_url_state(node.value, module_env)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in target_names(target):
                    module_env[name] = state
    for function in functions.values():
        process_scope(function.body, module_env)
    return matches


_HTTP_METHOD_NAMES = frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"})
_HTTP_WRITE_METHOD_NAMES = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _empty_method_state() -> dict[str, Any]:
    return {"known": set(), "unknown": False}


def _merge_method_state(*states: Mapping[str, Any]) -> dict[str, Any]:
    merged = _empty_method_state()
    for state in states:
        merged["known"].update(state.get("known", set()))
        merged["unknown"] = bool(merged["unknown"] or state.get("unknown"))
    return merged


def _string_method_state(value: str) -> dict[str, Any]:
    method = value.strip().upper()
    if method in _HTTP_METHOD_NAMES:
        return {"known": {method}, "unknown": False}
    return {"known": set(), "unknown": True}


def _eval_method_state(node: ast.AST, env: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _string_method_state(node.value)
    if isinstance(node, ast.Name):
        return dict(env.get(node.id, {"known": set(), "unknown": True}))
    if isinstance(node, ast.JoinedStr):
        states = []
        literal: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                literal.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                states.append(_eval_method_state(value.value, env))
        states.append(_string_method_state("".join(literal)))
        return _merge_method_state(*states)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _merge_method_state(_eval_method_state(node.left, env), _eval_method_state(node.right, env))
    if isinstance(node, ast.BoolOp):
        return _merge_method_state(*(_eval_method_state(value, env) for value in node.values))
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return _merge_method_state(*(_eval_method_state(value, env) for value in node.elts))
    if isinstance(node, ast.IfExp):
        return _merge_method_state(_eval_method_state(node.body, env), _eval_method_state(node.orelse, env))
    return {"known": set(), "unknown": True}


def _target_state(node: ast.AST, env: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    state = _eval_url_state(node, env)
    if not state["known"] and not state["internal"] and not state["unknown"]:
        dotted = (_ast_dotted_name(node) or "")
        if re.search(r"(?i)(?:url|uri|endpoint|target|path)", dotted):
            state["unknown"] = True
    return state


def _transport_arguments(
    node: ast.Call,
    url_env: Mapping[str, Mapping[str, Any]],
    method_env: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_dotted = _ast_dotted_name(node.func) or ""
    dotted = raw_dotted.lower()
    last = dotted.rsplit(".", 1)[-1]
    keyword_values = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
    if raw_dotted.startswith("urllib.") and raw_dotted.rsplit(".", 1)[-1] == "Request":
        method_node = keyword_values.get("method")
        if method_node is not None:
            method = _eval_method_state(method_node, method_env)
        elif keyword_values.get("data") is not None or len(node.args) > 1:
            method = _string_method_state("POST")
        else:
            method = _string_method_state("GET")
        target_node = keyword_values.get("url") or (node.args[0] if node.args else None)
        target = _target_state(target_node, url_env) if target_node is not None else {"known": set(), "unknown": True, "internal": False}
        return method, target
    if last in {"get", "head", "options", "post", "put", "patch", "delete"}:
        method = _string_method_state(last)
        target_node = keyword_values.get("url") or (node.args[0] if node.args else None)
    elif last in {"request", "urlopen", "fetch"} or dotted.endswith(".request.request"):
        method_node = keyword_values.get("method")
        if method_node is None and last == "request" and node.args:
            method_node = node.args[0]
        method = _eval_method_state(method_node, method_env) if method_node is not None else _empty_method_state()
        if method_node is None and last in {"urlopen", "fetch"}:
            method = _string_method_state("GET")
        target_node = keyword_values.get("url")
        if target_node is None:
            if last == "request" and len(node.args) > 1:
                target_node = node.args[1]
            elif node.args:
                target_node = node.args[0]
    else:
        return _empty_method_state(), _empty_url_state()
    target = _target_state(target_node, url_env) if target_node is not None else {"known": set(), "unknown": True, "internal": False}
    return method, target


def _internal_transport_kind(method: Mapping[str, Any], target: Mapping[str, Any]) -> str | None:
    known_methods = set(method.get("known", set()))
    internal = bool(target.get("internal"))
    target_unknown = bool(target.get("unknown")) or (not internal and not target.get("known"))
    method_unknown = bool(method.get("unknown")) or not known_methods
    if internal and known_methods.intersection(_HTTP_WRITE_METHOD_NAMES):
        return "known_internal_write_transport"
    if internal and not method_unknown:
        return "known_internal_read_transport"
    if internal and method_unknown:
        return "ambiguous_internal_transport"
    if target_unknown and (known_methods.intersection(_HTTP_WRITE_METHOD_NAMES) or method_unknown):
        return "ambiguous_internal_transport"
    return None


def _python_internal_transport_matches(text: str) -> dict[int, list[tuple[str, str, str]]]:
    matches: dict[int, list[tuple[str, str, str]]] = {}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return matches

    def target_names(target: ast.AST) -> list[str]:
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, (ast.Tuple, ast.List)):
            result: list[str] = []
            for item in target.elts:
                result.extend(target_names(item))
            return result
        return []

    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    helper_specs: dict[str, dict[str, Any]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = node
            all_params = [
                argument.arg
                for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            ]
            transported = _function_transport_params(node)
            if not transported:
                # Only helpers whose parameters have been proven to reach an
                # HTTP transport may participate in cross-function
                # propagation.  Ordinary local helpers (for example a
                # ``dict.get`` numeric coercer) must not create an unknown
                # transport finding at their call site.
                continue
            method_param = next(
                (name for name in transported if re.search(r"(?i)(?:method|verb)", name)),
                None,
            )
            target_param = next(
                (
                    name
                    for name in transported
                    if re.search(r"(?i)(?:url|uri|endpoint|target|path)", name)
                ),
                None,
            )
            default_method: str | None = None
            for child in ast.walk(node):
                if not isinstance(child, ast.Call) or not _is_http_transport_call(child):
                    continue
                dotted = (_ast_dotted_name(child.func) or "").lower()
                last = dotted.rsplit(".", 1)[-1]
                if last.upper() in _HTTP_METHOD_NAMES:
                    default_method = last.upper()
                    break
                if last == "request" and child.args:
                    literal = _python_string_value(child.args[0])
                    if literal and literal.strip().upper() in _HTTP_METHOD_NAMES:
                        default_method = literal.strip().upper()
                        break
            helper_specs[node.name] = {
                "params": tuple(all_params),
                "transported": tuple(transported),
                "method_param": method_param,
                "target_param": target_param,
                "default_method": default_method,
            }

    def add_transport(line_number: int, method: Mapping[str, Any], target: Mapping[str, Any]) -> None:
        kind = _internal_transport_kind(method, target)
        if kind is None:
            return
        if kind == "known_internal_write_transport":
            reason = "internal write HTTP transport resolved through file-level method and target dataflow"
            symbol = "internal_write"
        elif kind == "known_internal_read_transport":
            reason = "internal read HTTP transport resolved through file-level method and target dataflow"
            symbol = "internal_read"
        else:
            reason = "internal HTTP transport has unresolved method or target; human review required"
            symbol = "unresolved_internal_transport"
        _python_match(matches, line_number, kind, symbol, reason)

    def process_scope(
        body: Sequence[ast.stmt],
        initial_urls: Mapping[str, Mapping[str, Any]] | None = None,
        initial_methods: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        include_function_bodies: bool = True,
    ) -> None:
        url_env = {name: dict(state) for name, state in (initial_urls or {}).items()}
        method_env = {name: dict(state) for name, state in (initial_methods or {}).items()}
        roots = (
            body
            if include_function_bodies
            else tuple(
                root
                for root in body
                if not isinstance(root, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            )
        )
        nodes = sorted(
            (
                node
                for root in roots
                for node in ast.walk(root)
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            ),
            key=lambda node: (int(getattr(node, "lineno", 0)), int(getattr(node, "col_offset", 0))),
        )
        for node in nodes:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                url_state = _eval_url_state(node.value, url_env)
                method_state = _eval_method_state(node.value, method_env)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in target_names(target):
                        adjusted_url = dict(url_state)
                        adjusted_method = dict(method_state)
                        if not adjusted_url["known"] and not adjusted_url["internal"] and not adjusted_url["unknown"] and re.search(r"(?i)(?:url|uri|endpoint|target|path)", name):
                            adjusted_url["unknown"] = True
                        if not adjusted_method["known"] and not adjusted_method["unknown"] and re.search(r"(?i)(?:method|verb)", name):
                            adjusted_method["unknown"] = True
                        url_env[name] = adjusted_url
                        method_env[name] = adjusted_method
            elif isinstance(node, ast.Call):
                if _is_http_transport_call(node):
                    method, target = _transport_arguments(node, url_env, method_env)
                    add_transport(int(getattr(node, "lineno", 1)), method, target)
                target_name = _ast_dotted_name(node.func)
                helper = helper_specs.get(target_name or "")
                if helper:
                    arguments = list(node.args)
                    keyword_arguments = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}

                    def argument_for(parameter: str | None) -> ast.AST | None:
                        if parameter is None:
                            return None
                        if parameter in keyword_arguments:
                            return keyword_arguments[parameter]
                        try:
                            index = helper["params"].index(parameter)
                        except ValueError:
                            return None
                        return arguments[index] if index < len(arguments) else None

                    method_node = argument_for(helper["method_param"])
                    if method_node is not None:
                        method_state = _eval_method_state(method_node, method_env)
                    elif helper["default_method"]:
                        method_state = _string_method_state(helper["default_method"])
                    else:
                        method_state = {"known": set(), "unknown": True}
                    target_node = argument_for(helper["target_param"])
                    if target_node is None:
                        for parameter in helper["transported"]:
                            candidate = argument_for(parameter)
                            if candidate is None:
                                continue
                            candidate_state = _target_state(candidate, url_env)
                            if candidate_state["internal"] or candidate_state["known"] or candidate_state["unknown"]:
                                target_node = candidate
                                break
                    target_state = _target_state(target_node, url_env) if target_node is not None else {"known": set(), "unknown": True, "internal": False}
                    add_transport(int(getattr(node, "lineno", 1)), method_state, target_state)

    module_urls: dict[str, Mapping[str, Any]] = {}
    module_methods: dict[str, Mapping[str, Any]] = {}
    process_scope(tree.body, include_function_bodies=False)
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            url_state = _eval_url_state(node.value, module_urls)
            method_state = _eval_method_state(node.value, module_methods)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in target_names(target):
                    if not url_state["known"] and not url_state["internal"] and not url_state["unknown"] and re.search(r"(?i)(?:url|uri|endpoint|target|path)", name):
                        url_state = dict(url_state)
                        url_state["unknown"] = True
                    if not method_state["known"] and not method_state["unknown"] and re.search(r"(?i)(?:method|verb)", name):
                        method_state = dict(method_state)
                        method_state["unknown"] = True
                    module_urls[name] = url_state
                    module_methods[name] = method_state
    for function in functions.values():
        process_scope(function.body, module_urls, module_methods)
    return matches


def _python_analysis_matches(
    text: str,
    *,
    cache_key: str | None = None,
) -> dict[int, list[tuple[str, str, str]]]:
    if cache_key is not None and cache_key in _PYTHON_ANALYSIS_CACHE:
        return _PYTHON_ANALYSIS_CACHE[cache_key]
    static_candidate = bool(
        re.search(
            r"(?i)\b(?:ym_stock_data|pytdx|akshare|pywencai|tushare|easyquotation|stocktoday|windpy|workbuddy|mcp|tdx|"
            r"TdxHq_API|importlib|__import__)\b",
            text,
        )
    )
    dataflow_candidate = bool(
        HTTP_CLIENT_RE.search(text)
        and (
            URL_RE.search(text)
            or INTERNAL_API_RE.search(text)
            or re.search(r"(?i)\b(?:url|uri|endpoint|target|method|verb)\b", text)
        )
    )
    if not static_candidate and not dataflow_candidate:
        result: dict[int, list[tuple[str, str, str]]] = {}
        if cache_key is not None:
            _PYTHON_ANALYSIS_CACHE[cache_key] = result
        return result
    static, tree = _python_static_matches(text)
    sources = [_python_dynamic_string_matches(text, tree)] if static_candidate else []
    if dataflow_candidate:
        sources.append(_python_vendor_transport_matches(text))
        sources.append(_python_internal_transport_matches(text))
    for source in sources:
        for line_number, line_matches in source.items():
            for match in line_matches:
                _python_match(static, line_number, *match)
    if cache_key is not None:
        if len(_PYTHON_ANALYSIS_CACHE) >= _PYTHON_ANALYSIS_CACHE_LIMIT:
            _PYTHON_ANALYSIS_CACHE.pop(next(iter(_PYTHON_ANALYSIS_CACHE)))
        _PYTHON_ANALYSIS_CACHE[cache_key] = static
    return static


def _classify_finding(
    repository: str,
    file_classification: str,
    relative_path: str,
    match_kind: str,
    context: str,
) -> str:
    if file_classification != "production":
        return file_classification
    if file_classification == "dependency_metadata":
        return "dependency_metadata"
    if match_kind in {"private_import", "private_access"}:
        if _compatibility_rule(repository, relative_path, match_kind):
            return "approved_compatibility"
        return "production_bypass"
    if match_kind in {
        "provider_import",
        "known_vendor_url",
        "known_vendor_transport",
        "provider_fallback_chain",
        "direct_mcp_or_workbuddy",
    }:
        return "production_bypass"
    if match_kind == "ambiguous_vendor_transport":
        return "ambiguous"
    if match_kind == "known_internal_write_transport":
        return "production_write_transport"
    if match_kind == "known_internal_read_transport":
        return "approved_internal_transport"
    if match_kind == "ambiguous_internal_transport":
        return "ambiguous"
    if match_kind == "known_vendor_url_reference":
        return "production_reference"
    if match_kind == "direct_http_client":
        if _context_is_internal(context):
            if _context_is_write(context):
                return "production_write_transport"
            return "approved_internal_transport"
        if _context_has_known_vendor_url(context):
            return "production_bypass"
        return "production_reference"
    if match_kind == "provider_token":
        return "production_reference"
    return "production_reference"


def _matches_for_context(
    lines: Sequence[str],
    line_index: int,
    *,
    language: str = "unknown",
    python_matches: Mapping[int, Sequence[tuple[str, str, str]]] | None = None,
) -> Iterable[tuple[str, str, str, str]]:
    line = lines[line_index]
    line_number = line_index + 1
    if not QUICK_SCAN_MARKER_RE.search(line) and not (
        language == "python" and python_matches and line_number in python_matches
    ):
        return
    start = max(0, line_index - 3)
    end = min(len(lines), line_index + 4)
    context = "\n".join(lines[start:end])
    code_context = _strip_inline_comments(lines[start:end])
    seen: set[tuple[str, str]] = set()

    def emit(kind: str, symbol: str, reason: str) -> Iterable[tuple[str, str, str, str]]:
        key = (kind, symbol)
        if key not in seen:
            seen.add(key)
            yield kind, symbol, reason, code_context

    if language == "python" and python_matches is not None:
        for kind, symbol, reason in python_matches.get(line_number, ()):
            yield from emit(kind, symbol, reason)
    else:
        for match in PRIVATE_IMPORT_RE.finditer(line):
            yield from emit("private_import", match.group(0).strip(), "private ym_stock_data submodule import")
        root_import = ROOT_IMPORT_RE.search(line)
        if root_import:
            imported_names = [name.strip() for name in root_import.group(1).split(",")]
            noncanonical = [name for name in imported_names if name != "query"]
            if noncanonical:
                yield from emit(
                    "private_import",
                    "from ym_stock_data import " + ",".join(sorted(noncanonical)),
                    "non-canonical root ym_stock_data import; only public query is allowed",
                )
        for match in PRIVATE_ACCESS_RE.finditer(line):
            yield from emit("private_access", match.group(0).strip(), "private ym_stock_data submodule/access")
        for match in PROVIDER_IMPORT_RE.finditer(line):
            package = match.group("python_package") or match.group("js_package") or "provider"
            yield from emit("provider_import", package.lower(), "direct provider package import or SDK reference")
    for match in PROVIDER_TOKEN_RE.finditer(line):
        yield from emit("provider_token", match.group(0).lower(), "provider/package token or UI/config label reference")
    for match in URL_RE.finditer(line):
        url = _sanitize_url(match.group(0))
        host = _known_vendor_host(url)
        if _is_known_vendor_host(host):
            if HTTP_CALL_RE.search(code_context) and not _context_is_internal(code_context):
                yield from emit("known_vendor_url", url, "known vendor URL used within a bounded HTTP call context")
            else:
                yield from emit("known_vendor_url_reference", url, "known vendor URL reference without an observed call")
    specific_transport_kinds = {
        "known_vendor_transport",
        "ambiguous_vendor_transport",
        "known_internal_write_transport",
        "known_internal_read_transport",
        "ambiguous_internal_transport",
    }
    has_specific_transport = bool(
        language == "python"
        and python_matches
        and any(kind in specific_transport_kinds for kind, _symbol, _reason in python_matches.get(line_number, ()))
    )
    if not has_specific_transport:
        for match in HTTP_CLIENT_RE.finditer(line):
            symbol = match.group(0).strip()
            yield from emit("direct_http_client", symbol, "direct HTTP client or shell network transport reference")
    fallback = FALLBACK_RE.search(code_context)
    provider_context = PROVIDER_TOKEN_RE.search(code_context) or re.search(r"\b(?:tdx|mcp)\b", code_context, re.IGNORECASE)
    if fallback and provider_context:
        tokens = sorted({match.lower() for match in PROVIDER_TOKEN_RE.findall(code_context)})
        if re.search(r"\btdx\b", code_context, re.IGNORECASE):
            tokens.append("tdx")
        if re.search(r"\bmcp\b", code_context, re.IGNORECASE):
            tokens.append("mcp")
        # Never copy the matched source line into the report.  A structured
        # token is enough to classify the chain and cannot leak credentials or
        # arbitrary prose from a config/template line.
        symbol = "fallback_chain:" + ",".join(sorted(set(tokens)))
        yield from emit("provider_fallback_chain", symbol, "provider fallback/retry/order chain in runtime context")
    direct_mcp = DIRECT_MCP_RE.search(code_context)
    if direct_mcp:
        yield from emit("direct_mcp_or_workbuddy", direct_mcp.group(0), "direct WorkBuddy/Tencent/TDX/MCP runtime call")


def _cached_text_matches(
    text: str,
    lines: Sequence[str],
    language: str,
    digest: str,
) -> tuple[tuple[int, str, str, str, str], ...]:
    key = (digest, language)
    cached = _TEXT_MATCH_CACHE.get(key)
    if cached is not None:
        return cached
    python_matches = _python_analysis_matches(text, cache_key=digest) if language == "python" else {}
    collected: list[tuple[int, str, str, str, str]] = []
    for line_index, _line in enumerate(lines):
        line_number = line_index + 1
        for match_kind, symbol, reason, context in _matches_for_context(
            lines,
            line_index,
            language=language,
            python_matches=python_matches,
        ):
            collected.append((line_number, match_kind, symbol, reason, context))
    result = tuple(collected)
    if len(_TEXT_MATCH_CACHE) >= _TEXT_MATCH_CACHE_LIMIT:
        _TEXT_MATCH_CACHE.pop(next(iter(_TEXT_MATCH_CACHE)))
    _TEXT_MATCH_CACHE[key] = result
    return result


def _iter_tree(
    root: Path,
) -> tuple[list[tuple[Path, str, str]], list[dict[str, str]], list[dict[str, str]]]:
    files: list[tuple[Path, str, str]] = []
    excluded: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            children = list(os.scandir(current))
            children.sort(key=lambda item: item.name)
        except Exception as exc:
            failures.append(
                {
                    "path": _relative_path(root, current),
                    "operation": "scandir_or_list",
                    "reason": f"scan_error:{type(exc).__name__}",
                }
            )
            continue
        for item in children:
            path = Path(item.path)
            relative = _relative_path(root, path)
            try:
                is_link = item.is_symlink()
                is_dir = item.is_dir(follow_symlinks=False)
                is_file = item.is_file(follow_symlinks=False)
            except Exception as exc:
                failures.append(
                    {
                        "path": relative,
                        "operation": "stat",
                        "reason": f"stat_error:{type(exc).__name__}",
                    }
                )
                continue
            if is_dir and item.name in EXCLUDED_DIRECTORY_NAMES and not is_link:
                excluded.append({"path": relative, "reason": "explicit_tool_or_dependency_exclusion"})
                continue
            if is_link:
                files.append((path, relative, "symlink"))
            elif is_dir:
                pending.append(path)
            elif is_file:
                files.append((path, relative, "file"))
    files.sort(key=lambda value: value[1])
    excluded.sort(key=lambda value: value["path"])
    failures.sort(key=lambda value: (value["path"], value["operation"], value["reason"]))
    return files, excluded, failures


def _is_executable_script(path: Path, raw: bytes | None = None) -> bool | None:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return None
    if not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        return False
    if path.suffix.lower() in AGENT_SCRIPT_SUFFIXES:
        return True
    if raw is None:
        try:
            raw = path.read_bytes()
        except OSError:
            return None
    first_line = raw.splitlines()[0] if raw.splitlines() else b""
    return first_line.startswith(b"#!")


def _agent_tooling_alias_tree_sha256(root: Path) -> str:
    """Hash an alias target's relative tree and file contents without following links."""

    entries: list[dict[str, Any]] = []
    active_directories: set[tuple[int, int]] = set()

    def visit(path: Path, relative: str) -> None:
        try:
            item = path.lstat()
        except OSError as exc:
            raise ValueError(f"target_tree_stat_error:{type(exc).__name__}") from exc
        if stat.S_ISLNK(item.st_mode):
            raise ValueError("target_tree_symlink_or_cycle")
        if stat.S_ISDIR(item.st_mode):
            identity = (item.st_dev, item.st_ino)
            if identity in active_directories:
                raise ValueError("target_tree_cycle")
            active_directories.add(identity)
            entries.append({"path": relative, "kind": "directory"})
            try:
                children = sorted(path.iterdir(), key=lambda child: child.name)
            except OSError as exc:
                raise ValueError(f"target_tree_scan_error:{type(exc).__name__}") from exc
            for child in children:
                child_relative = f"{relative}/{child.name}" if relative != "." else child.name
                visit(child, child_relative)
            active_directories.remove(identity)
            return
        if not stat.S_ISREG(item.st_mode):
            raise ValueError("target_tree_special_file")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"target_tree_read_error:{type(exc).__name__}") from exc
        entries.append(
            {
                "path": relative,
                "kind": "file",
                "size_bytes": len(content),
                "sha256": _sha256_bytes(content),
            }
        )

    visit(Path(root), ".")
    return _sha256_json(entries)


def _safe_symlink_target(target: str) -> str:
    if os.path.isabs(target) or Path(target).is_absolute():
        return "redacted:absolute-path"
    return target


def _agent_tooling_text_classification(
    path: Path,
    raw: bytes,
    *,
    content_binding: Mapping[str, Any] | None = None,
) -> tuple[str, str, str]:
    if content_binding is not None:
        observed = _sha256_bytes(raw)
        if observed != content_binding.get("content_sha256"):
            return (
                "human_review_required",
                "content-hash binding drifted; generic A-share production must use canonical query",
                "pending_human_review",
            )
        return (
            str(content_binding["classification"]),
            str(content_binding["owner_policy"]),
            "not_applicable_non_data",
        )
    if len(raw) > MAX_TEXT_BYTES:
        return (
            "human_review_required",
            "file exceeds audit text limit; generic A-share production must use canonical query",
            "pending_human_review",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return (
            "human_review_required",
            "non-UTF-8 agent tooling file; generic A-share production must use canonical query",
            "pending_human_review",
        )
    language = _language(path) or "unknown"
    direct_provider = False
    ambiguous = False
    if language == "python":
        analysis = _python_analysis_matches(text)
        direct_provider = any(
            kind in {"provider_import", "known_vendor_transport", "direct_mcp_or_workbuddy"}
            for line_matches in analysis.values()
            for kind, _symbol, _reason in line_matches
        )
        ambiguous = any(
            kind == "ambiguous_vendor_transport"
            for line_matches in analysis.values()
            for kind, _symbol, _reason in line_matches
        )
    else:
        lines = text.splitlines()
        for index, line in enumerate(lines):
            context = _strip_inline_comments(lines[max(0, index - 3) : min(len(lines), index + 4)])
            if PROVIDER_IMPORT_RE.search(line) or DIRECT_MCP_RE.search(context):
                direct_provider = True
            if any(
                _is_known_vendor_host(_known_vendor_host(_sanitize_url(match.group(0))))
                for match in URL_RE.finditer(line)
            ) and HTTP_CALL_RE.search(context):
                direct_provider = True
        if HTTP_CLIENT_RE.search(text) and not direct_provider:
            ambiguous = bool(URL_RE.search(text))

    canonical_router = bool(
        re.search(r"(?i)(?:ym_stock_data\s*\.\s*query|from\s+ym_stock_data\s+import\s+query|canonical\s+query|public\s+query)", text)
    )
    provider_label = bool(PROVIDER_TOKEN_RE.search(text))
    if ambiguous:
        return (
            "human_review_required",
            "agent tooling contains an unresolved URL/transport channel; generic A-share production must use canonical query",
            "pending_human_review",
        )
    if direct_provider:
        return (
            "out_of_band_provider_channel",
            "specialized_explicit_only; generic A-share production must use canonical query; Task6 provider verification required",
            "pending_explicit_provider_verification",
        )
    if provider_label and not canonical_router:
        return (
            "specialized_explicit_only",
            "specialized_explicit_only; generic A-share production must use canonical query; Task6 provider verification required",
            "pending_explicit_provider_verification",
        )
    if canonical_router:
        return (
            "compliant",
            "canonical_query_only; generic A-share production must use canonical query",
            "not_applicable_canonical_router",
        )
    return (
        "human_review_required",
        "agent tooling channel has no proven canonical-query-only policy; generic A-share production must use canonical query",
        "pending_human_review",
    )


def _agent_tooling_alias_record(
    repository: str,
    root: Path,
    relative: str,
    channel_root: str,
    target: str,
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    expected_target = binding.get("expected_relative_target")
    expected_tree_hash = binding.get("target_tree_sha256")
    base = {
        "repository": repository,
        "path": relative,
        "channel": channel_root,
        "kind": "symlink_alias",
        "classification": "human_review_required",
        "owner_policy": "symlink alias binding failed; generic A-share production must use canonical query; fail closed",
        "task6_status": "pending_human_review",
        "size_bytes": None,
        "sha256": _sha256_bytes(target.encode("utf-8", errors="replace")),
        "symlink_target": _safe_symlink_target(target),
        "expected_relative_target": expected_target,
        "expected_target_tree_sha256": expected_tree_hash,
        "target_tree_sha256": None,
        "scan_status": "symlink_alias_error:unvalidated",
    }

    def failure(reason: str) -> dict[str, Any]:
        result = dict(base)
        result["scan_status"] = f"symlink_alias_error:{reason}"
        return result

    if not isinstance(expected_target, str) or not expected_target:
        return failure("invalid_expected_target")
    if os.path.isabs(target) or Path(target).is_absolute():
        return failure("absolute_target")
    if target != expected_target:
        return failure("target_drift")
    candidate = root / relative
    target_path = candidate.parent / target
    try:
        if target_path.is_symlink():
            return failure("target_is_symlink_or_cycle")
        resolved = target_path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        return failure(f"resolve_error:{type(exc).__name__}")
    root_resolved = root.resolve(strict=False)
    if not _inside(resolved, root_resolved):
        return failure("external_target")
    if not target_path.exists():
        return failure("missing_target")
    if not target_path.is_dir():
        return failure("target_not_directory")
    try:
        observed_hash = _agent_tooling_alias_tree_sha256(target_path)
    except ValueError as exc:
        return failure(str(exc))
    if not isinstance(expected_tree_hash, str) or observed_hash != expected_tree_hash:
        return failure("target_tree_hash_drift")
    base.update(
        {
            "classification": "out_of_band_provider_channel",
            "owner_policy": "specialized_explicit_only; generic A-share production must use canonical query; Task6 provider verification required",
            "task6_status": "pending_explicit_provider_verification",
            "sha256": observed_hash,
            "target_tree_sha256": observed_hash,
            "scan_status": "symlink_alias_bound",
        }
    )
    return base


def _inventory_agent_tooling_channels(
    repository: str,
    root: Path,
    *,
    alias_bindings: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    content_bindings: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    records: list[dict[str, Any]] = []
    excluded_paths: list[dict[str, str]] = []
    alias_bindings = alias_bindings or {}
    content_bindings = content_bindings or {}
    seen_aliases: set[tuple[str, str]] = set()

    def error_record(
        path: str,
        channel_root: str,
        *,
        kind: str,
        scan_status: str,
        classification: str = "human_review_required",
    ) -> dict[str, Any]:
        return {
            "repository": repository,
            "path": path,
            "channel": channel_root,
            "kind": kind,
            "classification": classification,
            "owner_policy": "filesystem scan error; generic A-share production must use canonical query; fail closed",
            "task6_status": "pending_human_review",
            "size_bytes": None,
            "sha256": _sha256_bytes(scan_status.encode("utf-8")),
            "scan_status": scan_status,
        }

    for channel_root in AGENT_TOOLING_CHANNEL_ROOTS:
        channel_parent = root / channel_root.split("/", 1)[0]
        for ignored_name in sorted(AGENT_TOOLING_IGNORED_DIRECTORIES):
            ignored_path = channel_parent / ignored_name
            try:
                ignored_stat = ignored_path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                records.append(
                    error_record(
                        _relative_path(root, ignored_path),
                        channel_root,
                        kind="stat_error",
                        scan_status=f"stat_error:{type(exc).__name__}",
                    )
                )
                continue
            if stat.S_ISDIR(ignored_stat.st_mode) and not stat.S_ISLNK(ignored_stat.st_mode):
                excluded_paths.append(
                    {
                        "repository": repository,
                        "path": _relative_path(root, ignored_path),
                        "channel": channel_root,
                        "reason": "explicit_agent_non_skill_cache_exclusion",
                        "evidence": "directory is outside the declared skill channel and is not read",
                    }
                )
        base = root / channel_root
        try:
            base_stat = base.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            records.append(
                error_record(
                    channel_root,
                    channel_root,
                    kind="stat_error",
                    scan_status=f"stat_error:{type(exc).__name__}",
                )
            )
            continue
        if stat.S_ISLNK(base_stat.st_mode):
            records.append(
                error_record(channel_root, channel_root, kind="symlink", scan_status="symlink_not_followed")
            )
            continue
        if not stat.S_ISDIR(base_stat.st_mode):
            continue
        pending = [base]
        while pending:
            current = pending.pop()
            try:
                children = list(os.scandir(current))
                children.sort(key=lambda item: item.name)
            except Exception as exc:
                records.append(
                    error_record(
                        _relative_path(root, current),
                        channel_root,
                        kind="scan_error",
                        scan_status=f"scan_error:{type(exc).__name__}",
                    )
                )
                continue
            for item in children:
                path = Path(item.path)
                relative = path.relative_to(root).as_posix()
                try:
                    if item.is_symlink():
                        alias_key = (repository, relative)
                        alias_binding = alias_bindings.get(alias_key)
                        if alias_binding is not None:
                            seen_aliases.add(alias_key)
                            records.append(
                                _agent_tooling_alias_record(
                                    repository,
                                    root,
                                    relative,
                                    channel_root,
                                    os.readlink(path),
                                    alias_binding,
                                )
                            )
                            continue
                        records.append(
                            error_record(
                                relative,
                                channel_root,
                                kind="symlink",
                                scan_status="symlink_not_followed",
                            )
                        )
                        continue
                    if item.is_dir(follow_symlinks=False):
                        if item.name.lower() in AGENT_TOOLING_IGNORED_DIRECTORIES:
                            excluded_paths.append(
                                {
                                    "repository": repository,
                                    "path": relative,
                                    "channel": channel_root,
                                    "reason": "explicit_agent_non_skill_cache_exclusion",
                                    "evidence": "excluded directory is not scanned as a skill or executable channel",
                                }
                            )
                            continue
                        pending.append(path)
                        continue
                    if not item.is_file(follow_symlinks=False):
                        continue
                except Exception as exc:
                    records.append(
                        error_record(
                            relative,
                            channel_root,
                            kind="stat_error",
                            scan_status=f"stat_error:{type(exc).__name__}",
                        )
                    )
                    continue
                is_skill = item.name == "SKILL.md"
                executable = _is_executable_script(path)
                if executable is None:
                    records.append(
                        error_record(
                            relative,
                            channel_root,
                            kind="stat_error",
                            scan_status="stat_error:executable_mode",
                        )
                    )
                    continue
                if not is_skill and not executable:
                    continue
                try:
                    raw = path.read_bytes()
                except OSError as exc:
                    records.append(
                        error_record(
                            relative,
                            channel_root,
                            kind="skill_manifest" if is_skill else "executable_script",
                            scan_status=f"read_error:{type(exc).__name__}",
                        )
                    )
                    continue
                content_binding = content_bindings.get((repository, relative))
                classification, owner_policy, task6_status = _agent_tooling_text_classification(
                    path,
                    raw,
                    content_binding=content_binding,
                )
                records.append(
                    {
                        "repository": repository,
                        "path": relative,
                        "channel": channel_root,
                        "kind": "skill_manifest" if is_skill else "executable_script",
                        "classification": classification,
                        "owner_policy": owner_policy,
                        "task6_status": task6_status,
                        "size_bytes": len(raw),
                        "sha256": _sha256_bytes(raw),
                        "scan_status": "scanned_metadata_only",
                    }
                )
    for alias_key, binding in alias_bindings.items():
        if alias_key[0] != repository or alias_key in seen_aliases:
            continue
        relative = alias_key[1]
        path = root / relative
        target = "missing"
        try:
            if os.path.lexists(path):
                if path.is_symlink():
                    target = os.readlink(path)
                else:
                    target = "not_a_symlink"
        except OSError as exc:
            target = f"read_error:{type(exc).__name__}"
        records.append(
            _agent_tooling_alias_record(
                repository,
                root,
                relative,
                "/".join(Path(relative).parts[:2]),
                target,
                binding,
            )
        )
    records.sort(key=lambda item: (item["repository"], item["path"]))
    excluded_paths = list(
        {
            (item["repository"], item["path"], item["channel"]): item
            for item in excluded_paths
        }.values()
    )
    excluded_paths.sort(key=lambda item: (item["repository"], item["path"], item["channel"]))
    return records, excluded_paths


def _repository_scan_signature(
    tree: Sequence[tuple[Path, str, str]],
    agent_tooling_channels: Sequence[Mapping[str, Any]],
    alias_bindings: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    content_bindings: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> str:
    entries: list[dict[str, Any]] = []
    for path, relative, kind in tree:
        item: dict[str, Any] = {"file": relative, "kind": kind}
        if kind == "symlink":
            try:
                stat_result = path.lstat()
                item["target"] = os.readlink(path)
                item.update(
                    {
                        "inode": stat_result.st_ino,
                        "ctime_ns": stat_result.st_ctime_ns,
                        "mtime_ns": stat_result.st_mtime_ns,
                    }
                )
            except OSError:
                item["target"] = "stat_error"
        else:
            try:
                stat_result = path.lstat()
                try:
                    content_hash = _sha256_bytes(path.read_bytes())
                except OSError as exc:
                    content_hash = f"read_error:{type(exc).__name__}"
                item.update(
                    {
                        "inode": stat_result.st_ino,
                        "ctime_ns": stat_result.st_ctime_ns,
                        "size": stat_result.st_size,
                        "mtime_ns": stat_result.st_mtime_ns,
                        "mode": stat_result.st_mode,
                        "content_sha256": content_hash,
                    }
                )
            except OSError:
                item["stat"] = "stat_error"
        entries.append(item)
    return _sha256_json(
        {
            "tree": entries,
            "agent_tooling_channels": list(agent_tooling_channels),
            "alias_bindings": {
                f"{repository}:{path}": dict(binding)
                for (repository, path), binding in sorted((alias_bindings or {}).items())
            },
            "content_bindings": {
                f"{repository}:{path}": dict(binding)
                for (repository, path), binding in sorted((content_bindings or {}).items())
            },
        }
    )


def _scan_repository(
    repository: str,
    root: Path,
    *,
    alias_bindings: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    content_bindings: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    root_realpath = str(root.resolve(strict=False))
    agent_tooling_channels, agent_tooling_excluded_paths = (
        _inventory_agent_tooling_channels(
            repository,
            root,
            alias_bindings=alias_bindings,
            content_bindings=content_bindings,
        )
        if root.is_dir()
        else ([], [])
    )
    if not root.exists() or not root.is_dir():
        findings.append(
            _finding(
                repository=repository,
                repository_realpath=root_realpath,
                file=".",
                file_realpath=root_realpath,
                line=1,
                language="unknown",
                symbol_or_url=str(root),
                match_kind="missing_repository",
                classification="unknown",
                deterministic_reason="configured consumer repository is missing or not a directory",
                business_entry_clue=f"repository:{repository}",
                excerpt_sha256=_sha256_bytes(str(root).encode()),
                excerpt_line_window="1",
            )
        )
        return findings, {
            "repository": repository,
            "realpath": root_realpath,
            "scanned_files": [],
            "file_entries": [],
            "excluded_paths": [],
            "scan_failures": [],
            "agent_tooling_excluded_paths": agent_tooling_excluded_paths,
            "ignored_by_gitignore": [],
            "gitignore_policy": "gitignore_not_consulted",
            "agent_tooling_channels": agent_tooling_channels,
        }

    tree, excluded, scan_failures = _iter_tree(root)
    scan_signature = _repository_scan_signature(
        tree,
        agent_tooling_channels,
        alias_bindings=alias_bindings,
        content_bindings=content_bindings,
    )
    repository_cache_key = f"{repository}:{root.resolve(strict=False)}"
    cached = _REPOSITORY_SCAN_CACHE.get(repository_cache_key)
    if cached is not None and cached[0] == scan_signature:
        return cached[1], cached[2]
    scanned_files: list[str] = []
    clue_cache: dict[tuple[str, int], str] = {}
    excerpt_cache: dict[tuple[str, int], str] = {}
    for failure in scan_failures:
        failure_path = str(failure.get("path") or ".")
        failure_realpath = str((root / failure_path).resolve(strict=False))
        findings.append(
            _finding(
                repository=repository,
                repository_realpath=root_realpath,
                file=failure_path,
                file_realpath=failure_realpath,
                line=1,
                language="unknown",
                symbol_or_url=str(failure.get("operation") or "scan"),
                match_kind="repository_scan_error",
                classification="unknown",
                deterministic_reason=f"repository {failure.get('operation', 'scan')} failed; fail-closed inventory result ({failure.get('reason', 'unknown')})",
                business_entry_clue=f"module:{failure_path}",
                excerpt_sha256=_sha256_json(failure),
                excerpt_line_window="1",
            )
        )
    for item in agent_tooling_channels:
        scan_status = str(item.get("scan_status") or "")
        if not scan_status.startswith(("scan_error:", "stat_error:", "read_error:", "symlink_alias_error:")) and scan_status != "symlink_not_followed":
            continue
        relative = str(item.get("path") or ".")
        findings.append(
            _finding(
                repository=repository,
                repository_realpath=root_realpath,
                file=relative,
                file_realpath=str((root / relative).resolve(strict=False)),
                line=1,
                language="unknown",
                symbol_or_url="agent_tooling_scan_error",
                match_kind="agent_tooling_scan_error",
                classification="unknown",
                deterministic_reason="agent tooling symlink/stat/read/scan error is not silently accepted; human review required",
                business_entry_clue=f"module:{relative}",
                excerpt_sha256=str(item.get("sha256") or _sha256_bytes(relative.encode())),
                excerpt_line_window="1",
            )
        )

    for path, relative, kind in tree:
        file_classification = _path_classification(relative)
        language = _language(path) or "unknown"
        if kind == "symlink":
            try:
                target = os.readlink(path)
            except OSError as exc:
                failure = {
                    "path": relative,
                    "operation": "readlink",
                    "reason": f"read_error:{type(exc).__name__}",
                }
                scan_failures.append(failure)
                findings.append(
                    _finding(
                        repository=repository,
                        repository_realpath=root_realpath,
                        file=relative,
                        file_realpath=str(path.resolve(strict=False)),
                        line=1,
                        language=language,
                        symbol_or_url="readlink_error",
                        match_kind="repository_scan_error",
                        classification="unknown",
                        deterministic_reason="symlink target could not be read; fail-closed inventory result",
                        business_entry_clue=f"module:{relative}",
                        excerpt_sha256=_sha256_json(failure),
                        excerpt_line_window="1",
                    )
                )
                continue
            resolved = str(path.resolve(strict=False))
            entry = {
                "repository": repository,
                "file": relative,
                "file_realpath": resolved,
                "language": language,
                "classification": file_classification,
                "kind": "symlink",
                "symlink_target": target,
                "scan_status": "not_followed",
            }
            entries.append(entry)
            scanned_files.append(relative)
            if not _inside(Path(resolved), root.resolve(strict=False)) and file_classification == "production":
                findings.append(
                    _finding(
                        repository=repository,
                        repository_realpath=root_realpath,
                        file=relative,
                        file_realpath=resolved,
                        line=1,
                        language=language,
                        symbol_or_url=target,
                        match_kind="external_symlink",
                        classification="unknown",
                        deterministic_reason="production path contains a symlink resolving outside repository; scanner refuses to follow it",
                        business_entry_clue=f"module:{relative}",
                        excerpt_sha256=_sha256_bytes(target.encode("utf-8", errors="replace")),
                        excerpt_line_window="1",
                    )
                )
            continue

        try:
            raw = path.read_bytes()
        except OSError as exc:
            entries.append(
                {
                    "repository": repository,
                    "file": relative,
                    "file_realpath": str(path.resolve(strict=False)),
                    "language": language,
                    "classification": file_classification,
                    "kind": "file",
                    "scan_status": f"read_error:{type(exc).__name__}",
                }
            )
            scan_failures.append(
                {
                    "path": relative,
                    "operation": "read_bytes",
                    "reason": f"read_error:{type(exc).__name__}",
                }
            )
            findings.append(
                _finding(
                    repository=repository,
                    repository_realpath=root_realpath,
                    file=relative,
                    file_realpath=str(path.resolve(strict=False)),
                    line=1,
                    language=language,
                    symbol_or_url="read_error",
                    match_kind="unreadable_production_file" if file_classification == "production" else "unreadable_file",
                    classification="unknown",
                    deterministic_reason="repository file could not be read; fail-closed inventory result",
                    business_entry_clue=f"module:{relative}",
                    excerpt_sha256=_sha256_bytes(str(exc).encode()),
                    excerpt_line_window="1",
                )
            )
            continue

        digest = _sha256_bytes(raw)
        entry = {
            "repository": repository,
            "file": relative,
            "file_realpath": str(path.resolve(strict=False)),
            "language": language,
            "classification": file_classification,
            "kind": "file",
            "size_bytes": len(raw),
            "sha256": digest,
            "scan_status": "scanned" if len(raw) <= MAX_TEXT_BYTES and _is_supported_text(path, raw) else "not_text_or_oversized",
        }
        entries.append(entry)
        scanned_files.append(relative)
        if len(raw) > MAX_TEXT_BYTES and file_classification == "production":
            findings.append(
                _finding(
                    repository=repository,
                    repository_realpath=root_realpath,
                    file=relative,
                    file_realpath=str(path.resolve(strict=False)),
                    line=1,
                    language=language,
                    symbol_or_url="oversized_file",
                    match_kind="unscanned_production_file",
                    classification="unknown",
                    deterministic_reason="production text file exceeds scanner size limit; fail-closed inventory result",
                    business_entry_clue=f"module:{relative}",
                    excerpt_sha256=digest,
                    excerpt_line_window="1",
                )
            )
            continue
        if not _is_supported_text(path, raw):
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            scan_failures.append(
                {
                    "path": relative,
                    "operation": "decode_utf8",
                    "reason": "decode_error:UnicodeDecodeError",
                }
            )
            findings.append(
                _finding(
                    repository=repository,
                    repository_realpath=root_realpath,
                    file=relative,
                    file_realpath=str(path.resolve(strict=False)),
                    line=1,
                    language=language,
                    symbol_or_url="decode_error",
                    match_kind="unreadable_production_file" if file_classification == "production" else "unreadable_file",
                    classification="unknown",
                    deterministic_reason="repository text file is not valid UTF-8; fail-closed inventory result",
                    business_entry_clue=f"module:{relative}",
                    excerpt_sha256=digest,
                    excerpt_line_window="1",
                )
            )
            continue
        lines = text.splitlines()
        for line_number, match_kind, symbol, reason, context in _cached_text_matches(
            text,
            lines,
            language,
            digest,
        ):
            classification = _classify_finding(
                repository,
                file_classification,
                relative,
                match_kind,
                context,
            )
            clue_cache_key = (relative, line_number)
            if clue_cache_key not in clue_cache:
                clue_cache[clue_cache_key] = _business_entry_clue(relative, text, line_number)
                excerpt_cache[clue_cache_key] = _excerpt_hash(text, line_number)
            findings.append(
                _finding(
                    repository=repository,
                    repository_realpath=root_realpath,
                    file=relative,
                    file_realpath=str(path.resolve(strict=False)),
                    line=line_number,
                    language=language,
                    symbol_or_url=_sanitize_url(symbol) if symbol.lower().startswith(("http://", "https://")) else symbol,
                    match_kind=match_kind,
                    classification=classification,
                    deterministic_reason=reason,
                    business_entry_clue=clue_cache[clue_cache_key],
                    excerpt_sha256=excerpt_cache[clue_cache_key],
                    excerpt_line_window=f"{max(1, line_number - 1)}-{min(len(lines), line_number + 1)}",
                )
            )

    findings.sort(key=lambda item: (item["file"], item["line"], item["match_kind"], item["symbol_or_url"]))
    entries.sort(key=lambda item: item["file"])
    manifest = {
        "repository": repository,
        "realpath": root_realpath,
        "scanned_files": sorted(scanned_files),
        "file_entries": entries,
        "excluded_paths": excluded,
        "scan_failures": scan_failures,
        "agent_tooling_excluded_paths": agent_tooling_excluded_paths,
        "ignored_by_gitignore": [],
        "gitignore_policy": "gitignore_not_consulted",
        "agent_tooling_channels": agent_tooling_channels,
    }
    _REPOSITORY_SCAN_CACHE[repository_cache_key] = (scan_signature, findings, manifest)
    return findings, manifest


def _consumer_file_owners(config: Mapping[str, Any]) -> dict[tuple[str, str], list[Mapping[str, Any]]]:
    owners: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for record in config.get("consumers", []):
        repository = str(record.get("repository", ""))
        for relative_path in record.get("finding_files", []):
            key = (repository, relative_path)
            owners.setdefault(key, []).append(record)
    return owners


def _consumer_entry_point_exists(repository: str, entry_point: str) -> bool:
    roots = dict(DEFAULT_REPOSITORIES)
    root = roots.get(repository)
    relative = Path(entry_point)
    if root is None or relative.is_absolute() or any(part == ".." for part in relative.parts):
        return False
    candidate = root / relative
    try:
        return candidate.is_file()
    except OSError:
        return False


def validate_consumer_coverage(
    config: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    owners = _consumer_file_owners(config)
    finding_keys = {
        (str(finding["repository"]), str(finding["file"]))
        for finding in findings
        if finding.get("repository") is not None and finding.get("file") is not None
    }
    production_files = sorted(
        {
            (str(finding["repository"]), str(finding["file"]))
            for finding in findings
            if finding.get("classification") == "production_bypass"
        }
    )
    covered = [key for key in production_files if key in owners and len(owners[key]) == 1]
    unclassified = [key for key in production_files if key not in owners or len(owners[key]) != 1]
    orphan_owner_claims = [
        {"repository": repository, "file": relative_path}
        for repository, relative_path in sorted(owners)
        if (repository, relative_path) not in finding_keys
    ]
    missing_entry_points = [
        {"repository": str(record.get("repository", "")), "entry_point": str(record.get("entry_point", ""))}
        for record in config.get("consumers", [])
        if not _consumer_entry_point_exists(str(record.get("repository", "")), str(record.get("entry_point", "")))
    ]
    write_keys = sorted(
        {
            (str(finding["repository"]), str(finding["file"]))
            for finding in findings
            if finding.get("classification") == "production_write_transport"
        }
    )
    protected_write_keys = [
        key
        for key in write_keys
        if len(owners.get(key, [])) == 1
        and owners[key][0].get("migration_status") == "protected_write_boundary"
    ]
    unprotected_write_keys = [key for key in write_keys if key not in protected_write_keys]

    def file_records(keys: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
        return [{"repository": repository, "file": relative_path} for repository, relative_path in keys]

    return {
        "production_bypass_files": len(production_files),
        "covered_production_files": len(covered),
        "unclassified_production_files": [
            {"repository": repository, "file": relative_path}
            for repository, relative_path in unclassified
        ],
        "owners": {
            f"{repository}:{relative_path}": [
                str(record.get("entry_point", "")) for record in owners[(repository, relative_path)]
            ]
            for repository, relative_path in production_files
            if (repository, relative_path) in owners
        },
        "orphan_owner_claims": orphan_owner_claims,
        "missing_entry_points": missing_entry_points,
        "write_transport_files": file_records(write_keys),
        "protected_write_transport_files": file_records(protected_write_keys),
        "unprotected_write_transport_files": file_records(unprotected_write_keys),
        "write_transport_file_keys": [f"{repository}:{relative_path}" for repository, relative_path in write_keys],
        "protected_write_transport_file_keys": [
            f"{repository}:{relative_path}" for repository, relative_path in protected_write_keys
        ],
        "unprotected_write_transport_file_keys": [
            f"{repository}:{relative_path}" for repository, relative_path in unprotected_write_keys
        ],
        "migration_production_bypass_files": [
            f"{repository}:{relative_path}" for repository, relative_path in production_files
        ],
    }


def _bundle_source_bindings(consumer_config: Mapping[str, Any] | None) -> dict[str, Any]:
    test_sources: dict[str, str] = {}
    for relative in ("tests/test_consumer_inventory_v3.py", "tests/test_stocktoday_audit.py"):
        path = ROOT / relative
        test_sources[relative] = _sha256_bytes(path.read_bytes())
    return {
        "binding_version": "consumer-audit-source-binding-v1",
        "scanner_source": "scripts/audit_data_consumers.py",
        "scanner_source_sha256": _sha256_bytes(Path(__file__).read_bytes()),
        "config_source": "config/data-consumers.v3.json" if consumer_config is not None else None,
        "consumer_config_sha256": _sha256_json(consumer_config) if consumer_config is not None else None,
        "test_sources": test_sources,
        "test_sources_sha256": _sha256_json(test_sources),
    }


def _summary(
    findings: Sequence[Mapping[str, Any]],
    consumer_config: Mapping[str, Any] | None = None,
    agent_tooling_channels: Sequence[Mapping[str, Any]] = (),
    agent_tooling_excluded_paths: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    counts = {
        "production_bypass": 0,
        "production_bypass_files": 0,
        "covered_production_files": 0,
        "unclassified_production_files": 0,
        "unclassified_production_file_keys": [],
        "production_reference": 0,
        "dependency_metadata": 0,
        "approved_internal_transport": 0,
        "production_write_transport": 0,
        "approved_compatibility": 0,
        "provider_internal": 0,
        "test": 0,
        "fixture": 0,
        "archive": 0,
        "generated": 0,
        "documentation": 0,
        "ambiguous_findings": 0,
        "unknown_findings": 0,
        "total_findings": len(findings),
        "unclassified_consumers": 0,
        "orphan_owner_claims": 0,
        "orphan_owner_claim_keys": [],
        "missing_entry_points": 0,
        "missing_entry_point_keys": [],
        "write_transport_files": 0,
        "protected_write_transport_files": 0,
        "unprotected_write_transport_files": 0,
        "write_transport_file_keys": [],
        "protected_write_transport_file_keys": [],
        "unprotected_write_transport_file_keys": [],
        "migration_production_bypass_files": [],
        "agent_tooling_channels": [dict(item) for item in agent_tooling_channels],
        "agent_tooling_channels_sha256": _sha256_json(list(agent_tooling_channels)),
        "agent_tooling_channel_files": len(agent_tooling_channels),
        "agent_tooling_out_of_band_files": sum(
            1 for item in agent_tooling_channels if item.get("classification") == "out_of_band_provider_channel"
        ),
        "agent_tooling_human_review_files": sum(
            1 for item in agent_tooling_channels if item.get("classification") == "human_review_required"
        ),
        "agent_tooling_scan_error_files": sum(
            1
            for item in agent_tooling_channels
            if str(item.get("scan_status") or "").startswith(("scan_error:", "stat_error:", "read_error:"))
            or str(item.get("scan_status") or "").startswith("symlink_alias_error:")
            or item.get("scan_status") == "symlink_not_followed"
        ),
        "agent_tooling_excluded_paths": [dict(item) for item in agent_tooling_excluded_paths],
        "agent_tooling_config_unmatched": [],
    }
    for finding in findings:
        classification = str(finding.get("classification", "unknown"))
        if classification in counts:
            counts[classification] += 1
        if classification == "unknown":
            counts["unknown_findings"] += 1
        # The current v3 scanner resolves every observed match to a
        # deterministic classification.  Keep this explicit field so a
        # future heuristic/semantic sidecar cannot silently turn an unresolved
        # item into an accepted consumer.
        if finding.get("classification") == "ambiguous":
            counts["ambiguous_findings"] += 1
    if consumer_config is not None:
        try:
            consumers = consumer_config.get("consumers", [])
            counts["unclassified_consumers"] = sum(
                1 for record in consumers if record.get("migration_status") in {"unknown", "unclassified"}
            )
            coverage = validate_consumer_coverage(consumer_config, findings)
            counts["production_bypass_files"] = coverage["production_bypass_files"]
            counts["covered_production_files"] = coverage["covered_production_files"]
            counts["unclassified_production_files"] = len(coverage["unclassified_production_files"])
            counts["unclassified_production_file_keys"] = [
                f"{item['repository']}:{item['file']}"
                for item in coverage["unclassified_production_files"]
            ]
            counts["orphan_owner_claims"] = len(coverage["orphan_owner_claims"])
            counts["orphan_owner_claim_keys"] = [
                f"{item['repository']}:{item['file']}" for item in coverage["orphan_owner_claims"]
            ]
            counts["missing_entry_points"] = len(coverage["missing_entry_points"])
            counts["missing_entry_point_keys"] = [
                f"{item['repository']}:{item['entry_point']}" for item in coverage["missing_entry_points"]
            ]
            for field in (
                "write_transport_files",
                "protected_write_transport_files",
                "unprotected_write_transport_files",
            ):
                counts[field] = len(coverage[field])
            for field in (
                "write_transport_file_keys",
                "protected_write_transport_file_keys",
                "unprotected_write_transport_file_keys",
                "migration_production_bypass_files",
            ):
                counts[field] = list(coverage[field])
            configured_channels = {
                (str(item.get("repository", "")), str(item.get("path", "")))
                for item in consumer_config.get("agent_tooling_channels", [])
                if isinstance(item, Mapping)
            }
            actual_channels = {
                (str(item.get("repository", "")), str(item.get("path", "")))
                for item in agent_tooling_channels
            }
            counts["agent_tooling_config_unmatched"] = [
                f"{repository}:{relative_path}"
                for repository, relative_path in sorted(configured_channels ^ actual_channels)
            ]
        except AttributeError:
            counts["unclassified_consumers"] = 1
    else:
        production_files = sorted(
            {
                (str(finding["repository"]), str(finding["file"]))
                for finding in findings
                if finding.get("classification") == "production_bypass"
            }
        )
        counts["production_bypass_files"] = len(production_files)
        counts["unclassified_production_files"] = len(production_files)
        counts["unclassified_production_file_keys"] = [
            f"{repository}:{relative_path}" for repository, relative_path in production_files
        ]
    return counts


def scan_repositories(
    repositories: Sequence[tuple[str, Path]] = DEFAULT_REPOSITORIES,
    *,
    consumer_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    all_findings: list[dict[str, Any]] = []
    repository_manifests: list[dict[str, Any]] = []
    alias_bindings = {
        (str(item["repository"]), str(item["path"])): dict(item)
        for item in (consumer_config or {}).get("agent_tooling_aliases", [])
        if isinstance(item, Mapping)
    }
    content_bindings = {
        (str(item["repository"]), str(item["path"])): dict(item)
        for item in (consumer_config or {}).get("agent_tooling_content_bindings", [])
        if isinstance(item, Mapping)
    }
    for repository, root in repositories:
        findings, manifest = _scan_repository(
            repository,
            Path(root),
            alias_bindings=alias_bindings,
            content_bindings=content_bindings,
        )
        all_findings.extend(findings)
        repository_manifests.append(manifest)
    all_findings.sort(key=lambda item: (item["repository"], item["file"], item["line"], item["match_kind"], item["symbol_or_url"]))
    file_entries = [entry for manifest in repository_manifests for entry in manifest["file_entries"]]
    file_entries.sort(key=lambda item: (item["repository"], item["file"]))
    agent_tooling_channels = [
        item
        for manifest in repository_manifests
        for item in manifest.get("agent_tooling_channels", [])
    ]
    agent_tooling_channels.sort(key=lambda item: (item["repository"], item["path"]))
    agent_tooling_excluded_paths = [
        item
        for manifest in repository_manifests
        for item in manifest.get("agent_tooling_excluded_paths", [])
    ]
    agent_tooling_excluded_paths.sort(key=lambda item: (item["path"], item["channel"]))
    scan_failures = [
        item
        for manifest in repository_manifests
        for item in manifest.get("scan_failures", [])
    ]
    scan_failures.sort(key=lambda item: (item.get("path", ""), item.get("operation", ""), item.get("reason", "")))
    source_bindings = _bundle_source_bindings(consumer_config)
    manifest_payload = {
        "schema_version": SCHEMA_VERSION,
        "scanner_version": SCANNER_VERSION,
        "source_bindings": source_bindings,
        "scanner_source_sha256": source_bindings["scanner_source_sha256"],
        "consumer_config_sha256": source_bindings["consumer_config_sha256"],
        "test_sources_sha256": source_bindings["test_sources_sha256"],
        "repositories": [manifest["repository"] for manifest in repository_manifests],
        "repository_manifests": repository_manifests,
        "scan_failures": scan_failures,
        "file_entries": file_entries,
        "agent_tooling_channels": agent_tooling_channels,
        "agent_tooling_channels_sha256": _sha256_json(agent_tooling_channels),
        "agent_tooling_aliases": [dict(item) for item in (consumer_config or {}).get("agent_tooling_aliases", [])],
        "agent_tooling_content_bindings": [
            dict(item) for item in (consumer_config or {}).get("agent_tooling_content_bindings", [])
        ],
        "agent_tooling_excluded_paths": agent_tooling_excluded_paths,
        # A single-repository fixture keeps paths repository-relative, which
        # makes mutation tests and small audit reports directly readable.  A
        # multi-repository report prefixes paths to prevent collisions; the
        # per-repository manifests always retain the relative file set.
        "scanned_files": (
            repository_manifests[0]["scanned_files"]
            if len(repository_manifests) == 1
            else [
                f"{manifest['repository']}/{file}"
                for manifest in repository_manifests
                for file in manifest["scanned_files"]
            ]
        ),
        "exclude_rules": {
            "directory_names": sorted(EXCLUDED_DIRECTORY_NAMES),
            "symlinks": "record_and_do_not_follow_except_bound_repository_relative_aliases",
            "gitignore": "not_consulted",
            "text_size_limit_bytes": MAX_TEXT_BYTES,
        },
        "compatibility_rules": _compatibility_rules_manifest(),
        "ignored_by_gitignore": [],
    }
    file_set_entries: list[dict[str, Any]] = []
    for entry in file_entries:
        projection = {
            key: entry.get(key)
            for key in (
                "repository",
                "file",
                "language",
                "classification",
                "kind",
                "size_bytes",
                "sha256",
                "symlink_target",
                "expected_relative_target",
                "expected_target_tree_sha256",
                "target_tree_sha256",
                "scan_status",
            )
            if key in entry
        }
        if isinstance(projection.get("symlink_target"), str) and os.path.isabs(projection["symlink_target"]):
            projection["symlink_target"] = "redacted:absolute-path"
        file_set_entries.append(projection)
    manifest_payload["file_set_sha256"] = _sha256_json(
        {
            "file_entries": file_set_entries,
            "agent_tooling_channels": agent_tooling_channels,
        }
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "scanner_version": SCANNER_VERSION,
        "repositories": [name for name, _ in repositories],
        "findings": all_findings,
        "manifest": manifest_payload,
        "summary": _summary(
            all_findings,
            consumer_config,
            agent_tooling_channels,
            agent_tooling_excluded_paths,
        ),
    }
    return report


def load_consumer_config(path: Path | str) -> dict[str, Any]:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("consumer config must be a JSON object")
    return value


def validate_consumer_config(config: Mapping[str, Any]) -> None:
    if config.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("consumer config schema_version is not v3")
    expected_repositories = {name for name, _ in DEFAULT_REPOSITORIES}
    configured_repositories = config.get("repositories")
    if set(configured_repositories or ()) != expected_repositories:
        raise ValueError("consumer config must list exactly the eight Task 5A repositories")
    tooling_channels = config.get("agent_tooling_channels")
    if not isinstance(tooling_channels, list):
        raise ValueError("consumer config must contain agent_tooling_channels list")
    seen_tooling: set[tuple[str, str]] = set()
    allowed_tooling_classifications = {
        "compliant",
        "specialized_explicit_only",
        "specialized_non_data",
        "out_of_band_provider_channel",
        "human_review_required",
    }
    for index, channel in enumerate(tooling_channels):
        if not isinstance(channel, dict) or set(channel) != set(REQUIRED_AGENT_TOOLING_FIELDS):
            raise ValueError(f"agent tooling channel {index} fields do not match schema")
        if channel["repository"] not in expected_repositories:
            raise ValueError(f"agent tooling channel {index} has unknown repository")
        if not all(isinstance(channel[field], str) and channel[field].strip() for field in REQUIRED_AGENT_TOOLING_FIELDS):
            raise ValueError(f"agent tooling channel {index} fields must be non-empty strings")
        relative = Path(channel["path"])
        if relative.is_absolute() or any(part == ".." for part in relative.parts):
            raise ValueError(f"agent tooling channel {index} has unsafe path")
        if channel["classification"] not in allowed_tooling_classifications:
            raise ValueError(f"agent tooling channel {index} has unknown classification")
        key = (channel["repository"], channel["path"])
        if key in seen_tooling:
            raise ValueError(f"duplicate agent tooling channel: {key}")
        seen_tooling.add(key)
    aliases = config.get("agent_tooling_aliases")
    if not isinstance(aliases, list):
        raise ValueError("consumer config must contain agent_tooling_aliases list")
    seen_aliases: set[tuple[str, str]] = set()
    for index, alias in enumerate(aliases):
        if not isinstance(alias, dict) or set(alias) != set(REQUIRED_AGENT_ALIAS_FIELDS):
            raise ValueError(f"agent tooling alias {index} fields do not match schema")
        if alias["repository"] not in expected_repositories:
            raise ValueError(f"agent tooling alias {index} has unknown repository")
        relative = Path(alias["path"])
        if relative.is_absolute() or any(part in {"", "."} for part in relative.parts) or relative.as_posix() != alias["path"]:
            raise ValueError(f"agent tooling alias {index} has unsafe path")
        target = Path(alias["expected_relative_target"])
        if not alias["expected_relative_target"] or target.is_absolute() or "\x00" in alias["expected_relative_target"]:
            raise ValueError(f"agent tooling alias {index} has unsafe relative target")
        if not isinstance(alias["target_tree_sha256"], str) or not SHA256_RE.fullmatch(alias["target_tree_sha256"]):
            raise ValueError(f"agent tooling alias {index} target tree hash is invalid")
        key = (alias["repository"], alias["path"])
        if key in seen_aliases:
            raise ValueError(f"duplicate agent tooling alias: {key}")
        if key not in seen_tooling:
            raise ValueError(f"agent tooling alias is not a configured channel: {key}")
        seen_aliases.add(key)
    content_bindings = config.get("agent_tooling_content_bindings")
    if not isinstance(content_bindings, list):
        raise ValueError("consumer config must contain agent_tooling_content_bindings list")
    seen_content_bindings: set[tuple[str, str]] = set()
    for index, binding in enumerate(content_bindings):
        if not isinstance(binding, dict) or set(binding) != set(REQUIRED_AGENT_CONTENT_BINDING_FIELDS):
            raise ValueError(f"agent tooling content binding {index} fields do not match schema")
        if binding["repository"] not in expected_repositories:
            raise ValueError(f"agent tooling content binding {index} has unknown repository")
        relative = Path(binding["path"])
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts) or relative.as_posix() != binding["path"]:
            raise ValueError(f"agent tooling content binding {index} has unsafe path")
        if not isinstance(binding["content_sha256"], str) or not SHA256_RE.fullmatch(binding["content_sha256"]):
            raise ValueError(f"agent tooling content binding {index} content hash is invalid")
        if binding["classification"] not in {"specialized_non_data", "compliant"}:
            raise ValueError(f"agent tooling content binding {index} has invalid classification")
        key = (binding["repository"], binding["path"])
        if key in seen_content_bindings:
            raise ValueError(f"duplicate agent tooling content binding: {key}")
        if key not in seen_tooling:
            raise ValueError(f"agent tooling content binding is not a configured channel: {key}")
        seen_content_bindings.add(key)
    consumers = config.get("consumers")
    if not isinstance(consumers, list) or not consumers:
        raise ValueError("consumer config must contain non-empty consumers")
    allowed_statuses = {
        "pending_inventory",
        "approved_compatibility_pending_migration",
        "observation_only_compliant",
        "compliant",
        "migration_not_started",
        "protected_write_boundary",
    }
    seen: set[tuple[str, str, str]] = set()
    for index, record in enumerate(consumers):
        if not isinstance(record, dict):
            raise ValueError(f"consumer record {index} must be an object")
        if set(record) != set(REQUIRED_CONSUMER_FIELDS):
            raise ValueError(f"consumer record {index} fields do not match required v3 schema")
        if record["repository"] not in expected_repositories:
            raise ValueError(f"consumer record {index} has unknown repository")
        for field in REQUIRED_CONSUMER_FIELDS:
            if field == "evidence_refs":
                if not isinstance(record[field], list) or not record[field] or not all(record[field]):
                    raise ValueError(f"consumer record {index} evidence_refs must be non-empty")
            elif field == "finding_files":
                if not isinstance(record[field], list) or not all(
                    isinstance(relative_path, str) and relative_path.strip()
                    for relative_path in record[field]
                ):
                    raise ValueError(f"consumer record {index} finding_files must be a list of paths")
                for relative_path in record[field]:
                    normalized = Path(relative_path).as_posix()
                    if (
                        Path(relative_path).is_absolute()
                        or normalized != relative_path
                        or relative_path == "."
                        or any(part == ".." for part in Path(relative_path).parts)
                    ):
                        raise ValueError(f"consumer record {index} has invalid finding_file path")
            elif not isinstance(record[field], str) or not record[field].strip():
                raise ValueError(f"consumer record {index} field {field} must be non-empty")
        if record["migration_status"] not in allowed_statuses:
            raise ValueError(f"consumer record {index} has unknown migration_status")
        key = (record["repository"], record["entry_point"], record["business_shape"])
        if key in seen:
            raise ValueError(f"duplicate consumer record: {key}")
        seen.add(key)
    file_owners = _consumer_file_owners(config)
    duplicate_files = sorted(
        f"{repository}:{relative_path}"
        for (repository, relative_path), owners in file_owners.items()
        if len(owners) != 1
    )
    if duplicate_files:
        raise ValueError(f"finding_files must have one owner: {duplicate_files}")
    missing = expected_repositories - {record["repository"] for record in consumers}
    if missing:
        raise ValueError(f"consumer config has no records for: {sorted(missing)}")


CONSUMER_AUDIT_OUTPUT_ROOT = ROOT / "outputs" / "consumer-audit"


def _validate_consumer_audit_output_path(output_dir: Path | str) -> Path:
    allowed_root = CONSUMER_AUDIT_OUTPUT_ROOT
    try:
        root_stat = allowed_root.lstat()
    except FileNotFoundError:
        allowed_root.mkdir(parents=True, exist_ok=True)
        root_stat = allowed_root.lstat()
    if stat.S_ISLNK(root_stat.st_mode):
        raise ValueError("consumer audit output root must not be a symlink")
    destination = Path(output_dir)
    lexical_destination = Path(os.path.abspath(os.fspath(destination)))
    lexical_root = Path(os.path.abspath(os.fspath(allowed_root)))
    resolved_destination = destination.resolve(strict=False)
    resolved_root = allowed_root.resolve(strict=False)
    if not _inside(lexical_destination, lexical_root) or not _inside(resolved_destination, resolved_root):
        raise ValueError("consumer audit output must stay under the repository output root")
    relative = lexical_destination.relative_to(lexical_root)
    current = lexical_root
    for part in relative.parts:
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"unable to validate consumer audit output path: {exc}") from exc
        if stat.S_ISLNK(current_stat.st_mode):
            raise ValueError("consumer audit output path must not contain symlinks")
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def _redact_bundle_paths(value: Any, *, key: str | None = None, repository: str | None = None) -> Any:
    if isinstance(value, dict):
        current_repository = str(value.get("repository")) if value.get("repository") else repository
        result: dict[str, Any] = {}
        for field, child in value.items():
            if field == "realpath":
                result[field] = f"repository://{current_repository}" if current_repository else "redacted:absolute-path"
            elif field == "file_realpath":
                relative = value.get("file")
                if current_repository and isinstance(relative, str):
                    result[field] = f"repository://{current_repository}/{relative}"
                else:
                    result[field] = "redacted:absolute-path"
            else:
                result[field] = _redact_bundle_paths(child, key=field, repository=current_repository)
        return result
    if isinstance(value, list):
        return [_redact_bundle_paths(child, key=key, repository=repository) for child in value]
    if isinstance(value, str) and os.path.isabs(value):
        return "redacted:absolute-path"
    return copy.deepcopy(value)


def write_audit_outputs(report: Mapping[str, Any], output_dir: Path | str) -> Path:
    destination = _validate_consumer_audit_output_path(output_dir)
    manifest = _redact_bundle_paths(report["manifest"])
    findings = _redact_bundle_paths(report["findings"])
    summary = _redact_bundle_paths(report["summary"])
    payloads = {
        "manifest.json": manifest,
        "findings.json": findings,
        "summary.json": summary,
    }
    for filename, payload in payloads.items():
        temporary = destination / f".{filename}.tmp"
        if os.path.lexists(temporary) and temporary.is_symlink():
            raise ValueError("consumer audit temporary output must not be a symlink")
        target = destination / filename
        if os.path.lexists(target) and target.is_symlink():
            raise ValueError("consumer audit output target must not be a symlink")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        temporary.replace(target)
    return destination


def _default_output_directory() -> Path:
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S%z")
    return ROOT / "outputs" / "consumer-audit" / stamp


def audit_exit_code(summary: Mapping[str, Any]) -> int:
    """Return a fail-closed status for an audit summary without rescanning."""
    return int(
        bool(
            summary.get("unknown_findings")
            or summary.get("unclassified_consumers")
            or summary.get("unclassified_production_files")
            or summary.get("ambiguous_findings")
            or summary.get("orphan_owner_claims")
            or summary.get("missing_entry_points")
            or summary.get("unprotected_write_transport_files")
            or summary.get("agent_tooling_scan_error_files")
            or summary.get("agent_tooling_human_review_files")
            or summary.get("agent_tooling_config_unmatched")
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "data-consumers.v3.json")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="emit the full report as JSON on stdout")
    args = parser.parse_args(argv)

    try:
        config = load_consumer_config(args.config)
        validate_consumer_config(config)
        report = scan_repositories(DEFAULT_REPOSITORIES, consumer_config=config)
        output_dir = write_audit_outputs(report, args.output or _default_output_directory())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"consumer audit failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps({"output_dir": str(output_dir), **report["summary"]}, ensure_ascii=False, sort_keys=True))
    # Production bypasses are inventory results.  Unknown, unowned production
    # files, or write transports mean the freeze is not safe to consume, so
    # the CLI fails closed while still writing the complete evidence bundle.
    return audit_exit_code(report["summary"])


if __name__ == "__main__":
    raise SystemExit(main())
