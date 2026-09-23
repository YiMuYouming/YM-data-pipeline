"""Frozen, credential-free inventory of StockToday's public data methods."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit


SOURCE_URL = "https://stocktoday.cn/api/tools/methods"
SCHEMA_VERSION = "3.0"
INVENTORY_PATH = Path(__file__).with_name("stocktoday_methods.v3.json")
METHOD_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
PARAMETER_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
INVENTORY_KEYS = frozenset(
    {"schema_version", "source_url", "fetched_at", "payload_sha256", "methods"}
)
METHOD_KEYS = frozenset(
    {
        "name",
        "source_order",
        "category",
        "subcategory",
        "desc",
        "example",
        "has_example",
        "params",
    }
)
PARAMETER_KEYS = frozenset({"name", "default"})
_SENSITIVE_WORDS = frozenset(
    {
        "token",
        "secret",
        "password",
        "authorization",
        "bearer",
        "credential",
        "credentials",
    }
)
_SENSITIVE_COMPACT_KEYS = frozenset(
    {
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "clientsecret",
        "apikey",
        "privatekey",
        "authheader",
    }
)
_PUBLIC_IDENTIFIER_WORDS = frozenset(
    {"code", "id", "identifier", "symbol", "url", "uri", "hash"}
)
_UUID_VALUE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_JWT_VALUE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_CREDENTIAL_PREFIX = re.compile(
    r"^(?:bearer(?:\s+|$)|sk[-_]|pk[-_]|gh[pousr]_|github_pat_|xox[a-z]*[-_]?|AKIA|AIza)",
    re.IGNORECASE,
)
_OPAQUE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+/=-]*$")
_MAX_CREDENTIAL_DEPTH = 8


def method_content_sha256(methods: list[dict[str, Any]]) -> str:
    """Hash the canonical, sorted method content used for drift checks."""

    canonical = json.dumps(
        methods,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _is_sensitive_key(key: str) -> bool:
    camel_spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", key)
    camel_spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", camel_spaced)
    words = [
        word.casefold()
        for word in re.sub(r"[^A-Za-z0-9]+", " ", camel_spaced).split()
    ]
    compact = "".join(words)
    return bool(_SENSITIVE_WORDS.intersection(words)) or compact in _SENSITIVE_COMPACT_KEYS


def _is_public_identifier_key(key: str | None) -> bool:
    if not key:
        return False
    camel_spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", key)
    camel_spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", camel_spaced)
    words = {
        word.casefold()
        for word in re.sub(r"[^A-Za-z0-9]+", " ", camel_spaced).split()
    }
    return bool(_PUBLIC_IDENTIFIER_WORDS.intersection(words))


def _parse_http_url(value: str):
    if any(character.isspace() for character in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    return parsed


def _is_http_url(value: str) -> bool:
    return _parse_http_url(value) is not None


def _is_opaque_credential(value: str) -> bool:
    return (
        len(value) >= 32
        and _OPAQUE_VALUE.fullmatch(value) is not None
        and re.search(r"[A-Za-z]", value) is not None
        and re.search(r"[0-9]", value) is not None
        and len(set(value)) >= 10
    )


def _is_explicit_credential_value(value: str) -> bool:
    return _CREDENTIAL_PREFIX.match(value) is not None or _JWT_VALUE.fullmatch(value) is not None


def _assert_safe_field_name(field: str, context: str) -> None:
    if _is_sensitive_key(field):
        raise ValueError(f"{context} contains a sensitive field")
    if _is_explicit_credential_value(field) or _is_opaque_credential(field):
        raise ValueError(f"{context} contains a credential-shaped field")


def _assert_url_query_credential_free(
    pairs: list[tuple[str, str]], context: str, depth: int
) -> None:
    if depth > _MAX_CREDENTIAL_DEPTH:
        raise ValueError("credential inspection depth exceeded")
    for query_key, query_value in pairs:
        _assert_safe_field_name(query_key, context)
        _assert_credential_free(
            query_value,
            f"{context} value",
            key=query_key,
            depth=depth + 1,
        )


def _assert_http_url_credential_free(parsed_url: Any, context: str, depth: int) -> None:
    if depth > _MAX_CREDENTIAL_DEPTH:
        raise ValueError("credential inspection depth exceeded")
    if parsed_url.username is not None or parsed_url.password is not None:
        raise ValueError(f"{context} contains URL credentials")
    _assert_url_query_credential_free(
        parse_qsl(parsed_url.query, keep_blank_values=True), f"{context} query", depth
    )
    _assert_url_query_credential_free(
        parse_qsl(parsed_url.fragment, keep_blank_values=True), f"{context} fragment", depth
    )


def _assert_credential_free(
    value: Any, context: str, key: str | None = None, depth: int = 0
) -> None:
    if depth > _MAX_CREDENTIAL_DEPTH:
        raise ValueError("credential inspection depth exceeded")
    if isinstance(value, dict):
        for field, child in value.items():
            if not isinstance(field, str):
                raise ValueError(f"{context} contains a non-string key")
            _assert_safe_field_name(field, context)
            _assert_credential_free(
                child, f"{context}.<field>", key=field, depth=depth + 1
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_credential_free(
                child, f"{context}[{index}]", key=key, depth=depth + 1
            )
    elif isinstance(value, str):
        parsed_url = _parse_http_url(value)
        if parsed_url is not None:
            _assert_http_url_credential_free(parsed_url, context, depth)
            return
        if _UUID_VALUE.fullmatch(value):
            return
        if _is_explicit_credential_value(value):
            raise ValueError(f"{context} contains a credential-shaped string")
        if _is_public_identifier_key(key):
            return
        if _is_opaque_credential(value):
            raise ValueError(f"{context} contains a credential-shaped string")


def _validate_methods(methods: Any) -> None:
    if not isinstance(methods, list) or not methods:
        raise ValueError("StockToday inventory methods must be a non-empty list")

    names: list[str] = []
    source_orders: list[int] = []
    for method in methods:
        if not isinstance(method, dict):
            raise ValueError("StockToday inventory method must be an object")
        name = method.get("name")
        if not isinstance(name, str) or not METHOD_NAME.fullmatch(name):
            raise ValueError("invalid StockToday method name")
        if name == "token_info":
            raise ValueError("token_info is an account diagnostic, not a data method")
        names.append(name)
        source_order = method.get("source_order")
        if type(source_order) is not int or source_order < 0:
            raise ValueError(f"StockToday method {name} has invalid source_order")
        source_orders.append(source_order)
        if set(method) != METHOD_KEYS:
            raise ValueError(f"StockToday method {name} has missing or unknown fields")

        for key in ("category", "subcategory", "desc"):
            if not isinstance(method.get(key), str):
                raise ValueError(f"StockToday method {name} has invalid {key}")
        example = method.get("example")
        if not isinstance(example, dict):
            raise ValueError(f"StockToday method {name} has invalid example")
        has_example = method.get("has_example")
        if type(has_example) is not bool or has_example != bool(example):
            raise ValueError(f"StockToday method {name} has inconsistent has_example")

        params = method.get("params")
        if not isinstance(params, list):
            raise ValueError(f"StockToday method {name} has invalid params")
        _assert_credential_free(example, f"method {name} example")
        parameter_names: list[str] = []
        for parameter in params:
            if not isinstance(parameter, dict):
                raise ValueError(f"StockToday method {name} has invalid parameter")
            if set(parameter) != PARAMETER_KEYS:
                raise ValueError(f"StockToday method {name} has missing or unknown parameter fields")
            parameter_name = parameter.get("name")
            if not isinstance(parameter_name, str) or not PARAMETER_NAME.fullmatch(parameter_name):
                raise ValueError(f"StockToday method {name} has invalid parameter name")
            if _is_sensitive_key(parameter_name):
                raise ValueError(f"StockToday method {name} has a sensitive parameter name")
            if "default" not in parameter:
                raise ValueError(f"StockToday method {name} parameter lacks default")
            _assert_credential_free(
                parameter["default"],
                f"method {name} parameter default",
                key=parameter_name,
            )
            parameter_names.append(parameter_name)
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError(f"StockToday method {name} has duplicate parameters")

    if len(names) != len(set(names)):
        raise ValueError("StockToday inventory contains duplicate method names")
    if names != sorted(names):
        raise ValueError("StockToday inventory methods must be sorted by name")
    if len(source_orders) != len(set(source_orders)):
        raise ValueError("StockToday inventory contains duplicate source_order values")
    if set(source_orders) != set(range(len(methods))):
        raise ValueError("StockToday source_order must cover the original method order")


def validate_inventory(payload: dict) -> dict:
    """Validate and return a frozen StockToday inventory payload."""

    if not isinstance(payload, dict):
        raise ValueError("StockToday inventory must be an object")

    # Validate names and method content before metadata so malformed fixtures
    # report the useful duplicate/unsafe-name error first.
    methods = payload.get("methods")
    _validate_methods(methods)
    if set(payload) != INVENTORY_KEYS:
        raise ValueError("StockToday inventory has missing or unknown top-level fields")

    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported StockToday inventory schema version")
    if payload.get("source_url") != SOURCE_URL:
        raise ValueError("unexpected StockToday inventory source URL")
    fetched_at = payload.get("fetched_at")
    if not isinstance(fetched_at, str) or not fetched_at.strip():
        raise ValueError("StockToday inventory fetched_at is required")
    try:
        parsed = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("StockToday inventory fetched_at is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("StockToday inventory fetched_at must include a timezone")

    payload_sha256 = payload.get("payload_sha256")
    if not isinstance(payload_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
        raise ValueError("StockToday inventory payload_sha256 is invalid")
    if payload_sha256 != method_content_sha256(methods):
        raise ValueError("StockToday inventory payload_sha256 does not match methods")
    return payload


def load_inventory(path: Path = INVENTORY_PATH) -> dict:
    """Load and validate a frozen inventory JSON file."""

    inventory_path = Path(path)
    try:
        payload = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to load StockToday inventory: {inventory_path}") from exc
    return validate_inventory(payload)


def method_map(path: Path = INVENTORY_PATH) -> dict[str, dict]:
    """Return frozen methods keyed by their safe public method name."""

    return {item["name"]: item for item in load_inventory(path)["methods"]}


def catalog_methods(keyword: str | None = None, path: Path = INVENTORY_PATH) -> dict:
    """Return a deterministic, credential-free catalog projection.

    Matching is a case-insensitive substring over method name, description,
    category, and subcategory.  The provider inventory is loaded locally;
    this helper never performs network or provider calls.
    """

    if keyword is not None and not isinstance(keyword, str):
        raise ValueError("StockToday catalog keyword must be a string")
    inventory = load_inventory(path)
    term = keyword.strip().casefold() if keyword else ""
    selected = []
    for item in inventory["methods"]:
        searchable = " ".join(
            str(item.get(field) or "")
            for field in ("name", "desc", "category", "subcategory")
        ).casefold()
        if term and term not in searchable:
            continue
        selected.append(
            {
                "name": item["name"],
                "desc": item["desc"],
                "category": item["category"],
                "subcategory": item["subcategory"],
                "allowed_params": sorted(
                    parameter["name"] for parameter in item["params"]
                ),
                "example": dict(item["example"]),
            }
        )
    return {
        "status": "complete",
        "query": keyword.strip() if isinstance(keyword, str) and keyword.strip() else None,
        "method_count": len(selected),
        "inventory_sha256": inventory["payload_sha256"],
        "methods": selected,
    }


__all__ = [
    "INVENTORY_PATH",
    "METHOD_NAME",
    "SOURCE_URL",
    "catalog_methods",
    "load_inventory",
    "method_content_sha256",
    "method_map",
    "validate_inventory",
]
