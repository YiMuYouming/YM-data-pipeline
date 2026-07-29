"""Command line interface for the unified A-share data channel."""

from __future__ import annotations

import argparse
import json
import subprocess

from .api import query as canonical_query
from .doctor import (
    collect_diagnostics,
    report_tdx_import_unavailable,
    setup_pywencai,
)
from .fetch import CANONICAL_ROUTES, LEGACY_DIRECT_ROUTES, list_supported
from .providers.tdx_mcp import CredentialImportError, import_tdx_credentials
from .routing import _ROUTES


CANONICAL_INTENTS = frozenset(_ROUTES) | {"review_sentiment", "stock_kline"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ym-data")
    commands = parser.add_subparsers(dest="command", required=True)

    query_parser = commands.add_parser("query", help="run one canonical intent")
    query_parser.add_argument("intent")
    query_parser.add_argument("params", nargs="*", metavar="key=value")

    doctor_parser = commands.add_parser("doctor", help="read-only provider diagnostics")
    doctor_parser.add_argument("--json", action="store_true", dest="as_json")

    setup_parser = commands.add_parser("setup", help="explicit optional runtime setup")
    setup_commands = setup_parser.add_subparsers(dest="setup_command", required=True)
    setup_commands.add_parser("pywencai")

    auth_parser = commands.add_parser("auth", help="explicit owned-auth operations")
    auth_commands = auth_parser.add_subparsers(dest="auth_command", required=True)
    import_tdx = auth_commands.add_parser("import-tdx")
    import_tdx.add_argument("--from-workbuddy", action="store_true")

    smoke_parser = commands.add_parser("smoke", help="explicit live read-only probes")
    smoke_parser.add_argument("--live", action="store_true", required=True)
    commands.add_parser("list", help="list canonical and compatibility routes")
    return parser


def _parse_params(values: list[str], parser: argparse.ArgumentParser) -> dict:
    result = {}
    for value in values:
        if "=" not in value:
            parser.error(f"query parameter must use key=value: {value}")
        key, raw = value.split("=", 1)
        if not key:
            parser.error("query parameter key cannot be empty")
        try:
            result[key] = json.loads(raw)
        except json.JSONDecodeError:
            result[key] = raw
    return result


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "query":
        result = canonical_query(args.intent, **_parse_params(args.params, parser))
        _print_json(result)
        return 0 if result.get("_meta", {}).get("status") != "error" else 1
    if args.command == "doctor":
        report = collect_diagnostics()
        if args.as_json:
            _print_json(report)
        else:
            for name, item in report["providers"].items():
                print(f"{name}: {item['status']}")
        return 0
    if args.command == "setup" and args.setup_command == "pywencai":
        try:
            result = setup_pywencai()
        except subprocess.CalledProcessError as error:
            _print_json({"status": "unavailable", "exit_code": error.returncode})
            return 1
        _print_json(result)
        return 0
    if args.command == "auth" and args.auth_command == "import-tdx":
        try:
            result = import_tdx_credentials(from_workbuddy=args.from_workbuddy)
        except CredentialImportError:
            _print_json({"status": "unavailable", "error_code": "IMPORT_FAILED"})
            return 2
        _print_json(result)
        return 0 if result.get("status") == "ready" else 2
    if args.command == "smoke":
        _print_json(
            {
                "status": "unavailable",
                "action": "live smoke probes are implemented in Task 11",
            }
        )
        return 2
    if args.command == "list":
        _print_json(
            {
                "canonical_intents": sorted(CANONICAL_INTENTS),
                "canonical_legacy_mappings": CANONICAL_ROUTES,
                "legacy_direct": sorted(LEGACY_DIRECT_ROUTES),
                "supported": list_supported(),
            }
        )
        return 0
    parser.error("unsupported command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
