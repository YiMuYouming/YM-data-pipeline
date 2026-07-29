"""Read-only provider diagnostics and explicit setup helpers."""

from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path
from typing import Callable, Iterable

from . import api
from .config import PYWENCAI_RUNTIME_DIR
from .providers.tdx_mcp import (
    TDX_AUTH_PATH,
    TDX_DIAGNOSTIC_NAMES,
    TdxCredentialStore,
    TdxMcpProvider,
)


ALLOWED_PROVIDER_STATES = frozenset(
    {
        "ready",
        "auth_missing",
        "auth_expired",
        "dependency_missing",
        "configured_unverified",
        "breaker_open",
        "unavailable",
    }
)
WIND_CONFIG_PATH = Path.home() / ".wind-aifinmarket" / "config"
WIND_PROVIDER_NAMES = ("wind_mcp", "wind_documents")


def _safe_probe(name: str, provider_loader: Callable[[str], object]) -> dict:
    try:
        raw = provider_loader(name).probe()
    except Exception:
        return {"provider": name, "status": "unavailable"}
    status = raw.get("status") if isinstance(raw, dict) else None
    if status not in ALLOWED_PROVIDER_STATES:
        status = "unavailable"
    result = {"provider": name, "status": status}
    if isinstance(raw, dict):
        for key in ("breaker", "action", "runtime_source"):
            value = raw.get(key)
            if isinstance(value, (str, bool)):
                result[key] = value
        auth = raw.get("auth")
        if isinstance(auth, dict):
            result["auth"] = {
                key: value
                for key, value in auth.items()
                if key in {"required", "status"}
                and isinstance(value, (str, bool))
            }
    return result


def collect_diagnostics(
    *,
    provider_names: Iterable[str] | None = None,
    provider_loader: Callable[[str], object] = api._provider_for,
    tdx_auth_path: Path = TDX_AUTH_PATH,
    wind_config_path: Path = WIND_CONFIG_PATH,
) -> dict:
    """Inspect every provider independently without making provider calls."""

    names = tuple(sorted(api.PROVIDER_REGISTRY) if provider_names is None else provider_names)
    providers = {}
    for name in names:
        if (
            name in TDX_DIAGNOSTIC_NAMES
            and provider_loader is api._provider_for
            and Path(tdx_auth_path) != TDX_AUTH_PATH
        ):
            provider = TdxMcpProvider(
                name,
                credential_store=TdxCredentialStore(tdx_auth_path),
            )
            providers[name] = _safe_probe(name, lambda _name, value=provider: value)
        else:
            providers[name] = _safe_probe(name, provider_loader)
    wind_status = "configured_unverified" if Path(wind_config_path).exists() else "unavailable"
    for name in WIND_PROVIDER_NAMES:
        providers[name] = {"provider": name, "status": wind_status}
    counts = Counter(item["status"] for item in providers.values())
    return {
        "schema_version": "1",
        "providers": providers,
        "summary": {status: counts.get(status, 0) for status in sorted(ALLOWED_PROVIDER_STATES)},
    }


def setup_pywencai(
    *,
    target: Path = PYWENCAI_RUNTIME_DIR,
    uv_executable: str = "uv",
    runner: Callable = subprocess.run,
    emit: Callable[[str], object] = print,
) -> dict:
    """Create the managed runtime only after this explicit function is called."""

    target = Path(target).expanduser()
    emit(str(target))
    commands = [
        [uv_executable, "venv", "--python", "3.12", str(target)],
        [
            uv_executable,
            "pip",
            "install",
            "--python",
            str(target / "bin" / "python"),
            "pywencai==0.13.1",
            "pandas",
            "numpy",
        ],
    ]
    for command in commands:
        runner(
            command,
            check=True,
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return {"status": "ready", "target": str(target)}


def report_tdx_import_unavailable(
    *,
    target: Path = TDX_AUTH_PATH,
    from_workbuddy: bool = False,
    emit: Callable[[str], object] = print,
) -> dict:
    """Task 8 boundary: report target, but never discover or write credentials."""

    target = Path(target).expanduser()
    emit(str(target))
    return {
        "status": "unavailable",
        "action": "TDX credential import is implemented in Task 9",
        "requested_from_workbuddy": bool(from_workbuddy),
    }
