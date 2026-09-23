"""Command line interface for the unified A-share data channel."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
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
    setup_pywencai,
)
from .fetch import CANONICAL_ROUTES, LEGACY_DIRECT_ROUTES, list_supported
from .intent_registry import list_registered_intents, resolve_intent
from .providers.tdx_auth import (
    DEFAULT_FILE_PATH,
    FileCredentialStore,
    READ_SCOPE,
    TdxAuthError,
    TdxOwnedAuth,
    default_credential_store,
    persist_credential_store_selection,
)
from .provider_smoke_v3 import run_provider_smoke
from .providers.stocktoday_inventory import catalog_methods
from .routing import _ROUTES
from .smoke import run_live_smoke
from .stocktoday_audit import (
    audit_status,
    default_cases,
    inventory_summary,
    run_audit as run_stocktoday_audit,
    validate_output_path,
)


CANONICAL_INTENTS = frozenset(_ROUTES) | {"review_sentiment", "stock_kline"}
_STRING_PARAM_KEYS = frozenset(
    {"code", "ts_code", "date", "trade_date", "start_date", "end_date"}
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ym-data")
    commands = parser.add_subparsers(dest="command", required=True)

    query_parser = commands.add_parser("query", help="run one canonical intent")
    query_parser.add_argument("intent")
    query_parser.add_argument("params", nargs="*", metavar="key=value")

    registered_parser = commands.add_parser(
        "intent", help="run one exact registered natural-language intent"
    )
    registered_parser.add_argument("phrase")
    registered_parser.add_argument("params", nargs="*", metavar="key=value")

    doctor_parser = commands.add_parser("doctor", help="read-only provider diagnostics")
    doctor_parser.add_argument("--json", action="store_true", dest="as_json")

    setup_parser = commands.add_parser("setup", help="explicit optional runtime setup")
    setup_commands = setup_parser.add_subparsers(dest="setup_command", required=True)
    setup_commands.add_parser("pywencai")

    auth_parser = commands.add_parser("auth", help="explicit owned-auth operations")
    auth_commands = auth_parser.add_subparsers(dest="auth_command", required=True)
    stocktoday_auth = auth_commands.add_parser("set-stocktoday")
    stocktoday_auth.add_argument("--stdin", required=True, action="store_true", help="read token from stdin and store in macOS Keychain")
    auth_commands.add_parser("status-stocktoday")
    for auth_name in ("login-tdx", "status-tdx"):
        tdx_auth = auth_commands.add_parser(auth_name)
        tdx_auth.add_argument(
            "--store",
            choices=("keychain", "file"),
            default=None,
        )
        tdx_auth.add_argument("--file-path", type=Path)
        if auth_name == "login-tdx":
            tdx_auth.add_argument("--show-url", action="store_true")

    smoke_parser = commands.add_parser("smoke", help="explicit live read-only probes")
    smoke_parser.add_argument("--live", action="store_true")
    smoke_parser.add_argument("--case-timeout", type=float, default=45.0)
    smoke_parser.add_argument("--total-timeout", type=float, default=360.0)
    provider_smoke_parser = commands.add_parser(
        "provider-smoke", help="run the bounded direct provider/capability matrix"
    )
    provider_smoke_parser.add_argument("--case-timeout", type=float, default=25.0)
    provider_smoke_parser.add_argument("--global-timeout", type=float, default=840.0)
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

    stocktoday_parser = commands.add_parser("stocktoday", help="StockToday inventory and explicit audit")
    stocktoday_commands = stocktoday_parser.add_subparsers(dest="stocktoday_command", required=True)
    stocktoday_inventory = stocktoday_commands.add_parser("inventory")
    stocktoday_inventory.add_argument("--json", action="store_true", dest="as_json")
    stocktoday_catalog = stocktoday_commands.add_parser(
        "catalog", help="search the local credential-free StockToday method catalog"
    )
    stocktoday_catalog.add_argument("keyword", nargs="?")
    stocktoday_catalog.add_argument("--json", action="store_true", dest="as_json")
    stocktoday_audit = stocktoday_commands.add_parser("audit")
    stocktoday_audit.add_argument("--live", action="store_true")
    stocktoday_audit.add_argument("--resume", type=Path)
    stocktoday_audit.add_argument("--output", type=Path, required=True)
    stocktoday_status = stocktoday_commands.add_parser("audit-status")
    stocktoday_status.add_argument("receipt", type=Path)
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
        if key in _STRING_PARAM_KEYS:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw
            result[key] = parsed if isinstance(parsed, str) else raw
            continue
        try:
            result[key] = json.loads(raw)
        except json.JSONDecodeError:
            result[key] = raw
    return result


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def create_tdx_auth(
    *,
    mode: str | None = None,
    file_path: Path | None = None,
) -> TdxOwnedAuth:
    if file_path is not None and mode != "file":
        raise ValueError("--file-path requires --store file")
    kwargs = {"mode": mode}
    if file_path is not None:
        kwargs["file_path"] = file_path
    return TdxOwnedAuth(store=default_credential_store(**kwargs))


def _credential_store_mode(auth: TdxOwnedAuth, explicit: str | None) -> str:
    if explicit in {"keychain", "file"}:
        return explicit
    report_mode = getattr(getattr(auth, "store", None), "report_mode", None)
    if report_mode == "selected":
        return report_mode
    if isinstance(getattr(auth, "store", None), FileCredentialStore):
        return "file"
    return "keychain"


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "query":
        result = canonical_query(args.intent, **_parse_params(args.params, parser))
        _print_json(result)
        return 0 if result.get("_meta", {}).get("status") != "error" else 1
    if args.command == "intent":
        try:
            descriptor = resolve_intent(
                args.phrase, **_parse_params(args.params, parser)
            )
        except ValueError as error:
            parser.error(str(error))
        result = canonical_query(
            descriptor["intent"], **descriptor.get("params", {})
        )
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
    if args.command == "auth":
        if args.auth_command in {"set-stocktoday", "status-stocktoday"}:
            from .providers.stocktoday import StockTodayProvider
            from .providers.stocktoday_auth import save_token
            if args.auth_command == "set-stocktoday":
                try:
                    save_token(sys.stdin.read(2049).strip())
                except Exception:
                    _print_json({"provider": "stocktoday", "status": "error", "error_code": "CREDENTIAL_STORE_FAILED"})
                    return 1
                _print_json({"provider": "stocktoday", "status": "configured_unverified", "storage": "macOS Keychain"})
            else:
                _print_json(StockTodayProvider().probe())
            return 0
        try:
            auth = create_tdx_auth(mode=args.store, file_path=args.file_path)
            store_mode = _credential_store_mode(auth, args.store)
            if args.auth_command == "login-tdx" and args.show_url:
                auth.browser_open = lambda url: print(url, file=sys.stderr, flush=True)
            status = (
                auth.login()
                if args.auth_command == "login-tdx"
                else auth.probe()
            )
            if (
                args.auth_command == "login-tdx"
                and status == "configured_unverified"
            ):
                selected_path = (
                    args.file_path
                    if store_mode == "file" and args.file_path is not None
                    else (
                        auth.store.path
                        if isinstance(getattr(auth, "store", None), FileCredentialStore)
                        else DEFAULT_FILE_PATH
                    )
                )
                persist_credential_store_selection(
                    store_mode,
                    file_path=selected_path if store_mode == "file" else None,
                )
        except (TdxAuthError, ValueError):
            _print_json(
                {
                    "status": "unavailable",
                    "error_code": "TDX_AUTH_FAILED",
                    "scope": READ_SCOPE,
                    "store": args.store or "selected",
                }
            )
            return 2
        _print_json({"status": status, "scope": READ_SCOPE, "store": store_mode})
        return 0 if status == "configured_unverified" else 2
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
                "source_status": receipt["source_status"],
                "chain_status": receipt["chain_status"],
                "gate_status": receipt["gate_status"],
            }
        )
        return 0 if receipt["gate_status"] == "pass" else 2
    if args.command == "provider-smoke":
        try:
            result = run_provider_smoke(
                case_timeout_sec=args.case_timeout,
                global_timeout_sec=args.global_timeout,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except (OSError, ValueError, json.JSONDecodeError):
            _print_json({"status": "failed", "error_code": "PROVIDER_SMOKE_FAILED"})
            return 2
        _print_json(
            {
                "status": "complete",
                "receipt": result["receipt"],
                "receipt_sha256": result["receipt_sha256"],
                "case_counts": result["case_counts"],
                "external_pending": len(result["external_pending"]),
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
    if args.command == "stocktoday":
        repo_root = Path(__file__).resolve().parents[1]
        if args.stocktoday_command == "inventory":
            report = inventory_summary()
            if args.as_json:
                _print_json(report)
            else:
                print(
                    "stocktoday methods={method_count} with_examples={methods_with_examples} "
                    "without_examples={methods_without_examples}".format(**report)
                )
            return 0
        if args.stocktoday_command == "catalog":
            report = catalog_methods(args.keyword)
            if args.as_json:
                _print_json(report)
            else:
                for method in report["methods"]:
                    params = ",".join(method["allowed_params"])
                    print(f'{method["name"]}: {method["desc"]} [{params}]')
            return 0
        if args.stocktoday_command == "audit-status":
            try:
                _print_json(audit_status(args.receipt))
            except Exception:
                _print_json({"counts": {"provider_error": 1}, "classifications": {"provider_error": "AUDIT_STATUS_FAILED"}})
                return 2
            return 0
        if not args.live:
            _print_json({"counts": {"not_run": 1}, "classifications": {"not_run": "pass --live explicitly"}})
            return 2
        try:
            output = validate_output_path(args.output, repo_root=repo_root)
            resume = args.resume
            if resume is not None:
                resume = validate_output_path(resume, repo_root=repo_root)
            receipt = run_stocktoday_audit(
                cases=default_cases(),
                resume=resume,
                output=output,
                repo_root=repo_root,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _print_json({"counts": {"provider_error": 1}, "classifications": {"provider_error": "AUDIT_FAILED"}})
            return 1
        _print_json({"counts": receipt["counts"], "classifications": receipt["classifications"]})
        return 0
    if args.command == "list":
        _print_json(
            {
                "canonical_intents": sorted(CANONICAL_INTENTS),
                "registered_intents": list_registered_intents(),
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
