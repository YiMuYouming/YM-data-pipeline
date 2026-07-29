"""Command line interface for the unified A-share data channel."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from .acceptance import (
    AcceptanceError,
    acceptance_template,
    build_daily_acceptance,
    validate_daily_acceptance,
)
from .api import query as canonical_query
from .doctor import (
    collect_diagnostics,
    report_tdx_import_unavailable,
    setup_pywencai,
)
from .fetch import CANONICAL_ROUTES, LEGACY_DIRECT_ROUTES, list_supported
from .providers.tdx_mcp import CredentialImportError, import_tdx_credentials
from .routing import _ROUTES
from .smoke import run_live_smoke


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
    smoke_parser.add_argument("--live", action="store_true")
    smoke_parser.add_argument("--case-timeout", type=float, default=45.0)
    smoke_parser.add_argument("--total-timeout", type=float, default=360.0)
    acceptance_parser = commands.add_parser(
        "acceptance", help="build or validate offline daily acceptance metadata"
    )
    acceptance_commands = acceptance_parser.add_subparsers(
        dest="acceptance_command", required=True
    )
    acceptance_template_parser = acceptance_commands.add_parser("template")
    acceptance_template_parser.add_argument("--date", required=True)
    acceptance_build = acceptance_commands.add_parser("build")
    acceptance_build.add_argument("--date", required=True)
    acceptance_build.add_argument("--doctor", required=True, type=Path)
    acceptance_build.add_argument("--smoke", required=True, type=Path)
    acceptance_build.add_argument("--downstream", required=True, type=Path)
    acceptance_build.add_argument("--calendar", required=True, type=Path)
    acceptance_build.add_argument("--output-dir", type=Path)
    acceptance_build.add_argument("--repo-root", type=Path)
    acceptance_validate = acceptance_commands.add_parser("validate")
    acceptance_validate.add_argument("path", type=Path)
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
        if not args.live:
            _print_json({"status": "not_run", "action": "pass --live explicitly"})
            return 2
        try:
            receipt = run_live_smoke(
                case_timeout_sec=args.case_timeout,
                total_timeout_sec=args.total_timeout,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _print_json({"status": "unavailable", "error_code": "SMOKE_FAILED"})
            return 1
        _print_json(
            {
                "status": "complete",
                "receipt": receipt["receipt"],
                "summary": receipt["summary"],
            }
        )
        return 0
    if args.command == "acceptance":
        try:
            if args.acceptance_command == "template":
                _print_json(acceptance_template(args.date))
                return 0
            if args.acceptance_command == "build":
                kwargs = {
                    "date": args.date,
                    "doctor_path": args.doctor,
                    "smoke_path": args.smoke,
                    "downstream_path": args.downstream,
                    "calendar_path": args.calendar,
                }
                if args.output_dir is not None:
                    kwargs["output_dir"] = args.output_dir
                if args.repo_root is not None:
                    kwargs["repo_root"] = args.repo_root
                result = build_daily_acceptance(**kwargs)
                _print_json({"status": "complete", **result})
                return 0
            result = validate_daily_acceptance(args.path)
            _print_json(result)
            return 0
        except AcceptanceError as error:
            _print_json({"status": "unavailable", "error_code": error.code})
            return 2
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _print_json({"status": "unavailable", "error_code": "ACCEPTANCE_FAILED"})
            return 1
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
