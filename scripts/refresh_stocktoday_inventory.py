#!/usr/bin/env python3
"""Refresh or check the frozen public StockToday method inventory."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


def _load_inventory_module():
    """Load the pure inventory module without executing package __init__."""

    module_path = (
        Path(__file__).resolve().parents[1]
        / "ym_stock_data"
        / "providers"
        / "stocktoday_inventory.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_stocktoday_inventory_refresh_module", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load StockToday inventory implementation")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_inventory_module = _load_inventory_module()
INVENTORY_PATH = _inventory_module.INVENTORY_PATH
SCHEMA_VERSION = _inventory_module.SCHEMA_VERSION
SOURCE_URL = _inventory_module.SOURCE_URL
METHOD_NAME = _inventory_module.METHOD_NAME
load_inventory = _inventory_module.load_inventory
method_content_sha256 = _inventory_module.method_content_sha256
validate_inventory = _inventory_module.validate_inventory

UPSTREAM_KEYS = frozenset({"code", "total_apis", "data", "level1_order"})
UPSTREAM_METHOD_KEYS = frozenset(
    {"desc", "example", "has_example", "name", "params"}
)
UPSTREAM_PARAMETER_KEYS = frozenset({"name", "default"})


def _validate_upstream_payload(payload: dict) -> None:
    if not isinstance(payload, dict) or set(payload) != UPSTREAM_KEYS:
        raise ValueError("StockToday methods envelope has missing or unknown top-level fields")
    if type(payload["code"]) is not int or payload["code"] != 0:
        raise ValueError("StockToday methods envelope code must be integer zero")
    total_apis = payload["total_apis"]
    if type(total_apis) is not int or total_apis < 0:
        raise ValueError("StockToday methods envelope total_apis is invalid")

    data = payload["data"]
    if not isinstance(data, dict) or any(
        not isinstance(category, str) or not category for category in data
    ):
        raise ValueError("StockToday methods data categories are invalid")
    level1_order = payload["level1_order"]
    if (
        not isinstance(level1_order, list)
        or not level1_order
        or any(not isinstance(category, str) or not category for category in level1_order)
        or len(level1_order) != len(set(level1_order))
        or set(level1_order) != set(data)
    ):
        raise ValueError("StockToday level1_order does not match data categories")

    method_count = 0
    for category in level1_order:
        subcategories = data[category]
        if not isinstance(subcategories, dict):
            raise ValueError("StockToday category structure is invalid")
        for subcategory, methods in subcategories.items():
            if not isinstance(subcategory, str) or not subcategory:
                raise ValueError("StockToday subcategory name is invalid")
            if not isinstance(methods, list):
                raise ValueError("StockToday subcategory methods must be a list")
            for method in methods:
                if not isinstance(method, dict) or set(method) != UPSTREAM_METHOD_KEYS:
                    raise ValueError("StockToday method has missing or unknown fields")
                if (
                    not isinstance(method["name"], str)
                    or not METHOD_NAME.fullmatch(method["name"])
                    or not isinstance(method["desc"], str)
                    or not isinstance(method["example"], dict)
                    or type(method["has_example"]) is not bool
                    or method["has_example"] != bool(method["example"])
                    or not isinstance(method["params"], list)
                ):
                    raise ValueError("StockToday method structure is invalid")
                for parameter in method["params"]:
                    if not isinstance(parameter, dict) or set(parameter) != UPSTREAM_PARAMETER_KEYS:
                        raise ValueError("StockToday parameter has missing or unknown fields")
                    if not isinstance(parameter["name"], str):
                        raise ValueError("StockToday parameter name is invalid")
                method_count += 1
    if total_apis != method_count:
        raise ValueError("StockToday total_apis does not match flattened method count")


def fetch_payload() -> dict:
    """Fetch the public methods page with the fixed, credential-free request."""

    response = requests.get(
        SOURCE_URL,
        timeout=(4, 20),
        allow_redirects=False,
        headers={"Accept": "application/json"},
    )
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and 300 <= status_code < 400:
        raise RuntimeError(f"StockToday inventory redirect blocked: HTTP {status_code}")
    if not isinstance(status_code, int) or not 200 <= status_code < 300:
        raise RuntimeError(f"StockToday inventory request failed: HTTP {status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("StockToday inventory response was not JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("StockToday inventory response must be a JSON object")
    flatten_methods(payload)
    return payload


def flatten_methods(payload: dict) -> list[dict[str, Any]]:
    """Flatten category/subcategory groups while preserving method fields."""

    _validate_upstream_payload(payload)

    flattened: list[dict[str, Any]] = []
    source_order = 0
    for category in payload["level1_order"]:
        subcategories = payload["data"][category]
        for subcategory, methods in subcategories.items():
            for method in methods:
                example = copy.deepcopy(method.get("example", {}))
                flattened.append(
                    {
                        "name": method.get("name"),
                        "source_order": source_order,
                        "category": category,
                        "subcategory": subcategory,
                        "desc": method.get("desc", ""),
                        "example": example,
                        "has_example": method.get("has_example", bool(example)),
                        "params": copy.deepcopy(method.get("params", [])),
                    }
                )
                source_order += 1
    if len(flattened) != payload["total_apis"]:
        raise ValueError("StockToday flattened method count does not match total_apis")
    return sorted(flattened, key=lambda item: item.get("name", ""))


def build_inventory(payload: dict, *, fetched_at: str | None = None) -> dict:
    """Build and validate a versioned frozen inventory from the live payload."""

    methods = flatten_methods(payload)
    inventory = {
        "schema_version": SCHEMA_VERSION,
        "source_url": SOURCE_URL,
        "fetched_at": fetched_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "payload_sha256": method_content_sha256(methods),
        "methods": methods,
    }
    return validate_inventory(inventory)


def write_inventory(path: Path, inventory: dict) -> None:
    """Atomically replace a JSON inventory file in its containing directory."""

    validated = validate_inventory(inventory)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
            json.dump(validated, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, destination)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def inventories_match(online: dict, frozen: dict) -> bool:
    """Compare only sorted method names and canonical method content."""

    online_methods = flatten_methods(online)
    frozen_methods = frozen["methods"]
    online_names = [method["name"] for method in online_methods]
    frozen_names = [method["name"] for method in frozen_methods]
    return online_names == frozen_names and method_content_sha256(online_methods) == frozen["payload_sha256"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh/check StockToday public method inventory")
    parser.add_argument(
        "--check",
        action="store_true",
        help="check online method names/content against the frozen file without writing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        online_payload = fetch_payload()
        online_inventory = build_inventory(online_payload)
        if args.check:
            frozen_inventory = load_inventory(INVENTORY_PATH)
            if inventories_match(online_payload, frozen_inventory):
                print(f"StockToday inventory unchanged: {len(online_inventory['methods'])} methods")
                return 0
            print("StockToday inventory drift detected", file=sys.stderr)
            return 1
        write_inventory(INVENTORY_PATH, online_inventory)
        print(f"Wrote {INVENTORY_PATH}: {len(online_inventory['methods'])} methods")
        return 0
    except requests.RequestException:
        print("StockToday inventory refresh failed: network request error", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"StockToday inventory refresh failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
