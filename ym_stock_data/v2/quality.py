"""Pure semantic quality assessment for V2 result rows."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping


_STOCK_IDENTITY_MARKERS = (
    "股票代码",
    "股票简称",
    "股票名称",
    "证券代码",
    "证券简称",
    "证券名称",
    "个股代码",
    "个股简称",
    "个股名称",
    "stock_code",
    "stock_name",
    "security_code",
    "security_name",
)
_SECTOR_MARKERS = (
    "板块代码",
    "板块名称",
    "板块简称",
    "行业代码",
    "行业名称",
    "行业简称",
    "所属行业",
    "概念代码",
    "概念名称",
    "概念简称",
    "所属概念",
    "指数代码",
    "指数简称",
    "sector_code",
    "sector_name",
    "industry_code",
    "industry_name",
    "concept_code",
    "concept_name",
)
_STATUS_SEVERITY = {
    "normal": 0,
    "partial": 1,
    "empty": 2,
    "semantic_degraded": 3,
    "error": 4,
}


def _normalized_key(value: object) -> str:
    return str(value).strip().lower().replace(" ", "")


def _looks_like_stock_code(value: object) -> bool:
    normalized = re.sub(r"[^0-9]", "", str(value or ""))
    return len(normalized) == 6 and not normalized.startswith("881")


def _looks_like_sector_code(value: object) -> bool:
    normalized = re.sub(r"[^0-9]", "", str(value or ""))
    return len(normalized) == 6 and normalized.startswith("881")


def _row_shape(row: Mapping[str, object]) -> str:
    normalized_items = [(_normalized_key(key), value) for key, value in row.items()]

    has_stock_identity = any(
        any(marker in key for marker in _STOCK_IDENTITY_MARKERS)
        for key, _ in normalized_items
    )
    if not has_stock_identity:
        has_stock_identity = any(
            key in {"code", "代码"} and _looks_like_stock_code(value)
            for key, value in normalized_items
        )
    if has_stock_identity:
        return "stock_rows"

    has_sector_identity = any(
        any(marker in key for marker in _SECTOR_MARKERS)
        for key, _ in normalized_items
    )
    if not has_sector_identity:
        has_sector_identity = any(
            key in {"code", "代码"} and _looks_like_sector_code(value)
            for key, value in normalized_items
        )
    if has_sector_identity:
        return "sector_rows"
    return "unknown"


def _rows_shape(rows: list[object]) -> tuple[str, str | None]:
    shapes = [
        _row_shape(row)
        for row in rows
        if isinstance(row, Mapping)
    ]
    if not shapes:
        return "unknown", "row_shape_unknown" if rows else None

    unique_shapes = set(shapes)
    if len(unique_shapes) > 1:
        return "unknown", "mixed_row_shapes"

    row_shape = next(iter(unique_shapes))
    if row_shape == "unknown":
        return row_shape, "row_shape_unknown"
    return row_shape, None


def _coverage(returned_count: int, expected_count: int | None) -> float | None:
    if expected_count is None or expected_count <= 0:
        return None
    return min(1.0, returned_count / expected_count)


def assess_quality(
    rows: list[dict[str, object]],
    *,
    expected_row_shape: str | None = None,
    expected_count: int | None = None,
    missing: list[str] | None = None,
    source_error: bool = False,
) -> dict[str, object]:
    """Assess result semantics independently from transport freshness."""
    normalized_rows = list(rows or [])
    missing_items = list(missing or [])
    returned_count = len(normalized_rows)
    row_shape, row_shape_issue = _rows_shape(normalized_rows)
    coverage = _coverage(returned_count, expected_count)

    if expected_row_shape is None:
        semantic_equivalence = "unknown"
    elif row_shape_issue == "mixed_row_shapes":
        semantic_equivalence = "non_equivalent"
    elif row_shape == "unknown":
        semantic_equivalence = "unknown"
    elif row_shape == expected_row_shape:
        semantic_equivalence = "exact"
    else:
        semantic_equivalence = "non_equivalent"

    reason_codes = []
    if source_error:
        reason_codes.append("source_error")
    if not normalized_rows:
        reason_codes.append("empty_result")
    if expected_row_shape is not None and row_shape_issue:
        reason_codes.append(row_shape_issue)
    elif semantic_equivalence == "non_equivalent":
        reason_codes.append("row_shape_mismatch")
    if missing_items:
        reason_codes.append("missing_items")
    if coverage is not None and coverage < 1.0:
        reason_codes.append("coverage_shortfall")

    if source_error:
        status = "error"
    elif not normalized_rows:
        status = "empty"
    elif expected_row_shape is not None and (
        row_shape_issue is not None or semantic_equivalence == "non_equivalent"
    ):
        status = "semantic_degraded"
    elif missing_items or (coverage is not None and coverage < 1.0):
        status = "partial"
    else:
        status = "normal"

    return {
        "status": status,
        "row_shape": row_shape,
        "expected_row_shape": expected_row_shape,
        "requested_count": expected_count,
        "returned_count": returned_count,
        "coverage": coverage,
        "missing": missing_items,
        "missing_count": len(missing_items),
        "semantic_equivalence": semantic_equivalence,
        "reason_codes": reason_codes,
    }


def rollup_qualities(qualities: Iterable[dict[str, object]]) -> dict[str, object]:
    """Roll per-query qualities into one worst-severity summary."""
    items = list(qualities)
    if not items:
        return assess_quality([])

    worst_status = max(
        (str(item.get("status", "normal")) for item in items),
        key=lambda status: _STATUS_SEVERITY.get(status, 0),
    )
    row_shapes = {str(item.get("row_shape", "unknown")) for item in items}
    expected_shapes = {item.get("expected_row_shape") for item in items}
    requested_counts = [item.get("requested_count") for item in items]

    requested_count = None
    if all(isinstance(value, int) for value in requested_counts):
        requested_count = sum(requested_counts)
    returned_count = sum(int(item.get("returned_count", 0)) for item in items)
    missing = []
    for item in items:
        for value in item.get("missing", []):
            if value not in missing:
                missing.append(value)
    reason_codes = []
    for item in items:
        for value in item.get("reason_codes", []):
            if value not in reason_codes:
                reason_codes.append(value)

    equivalences = {str(item.get("semantic_equivalence", "unknown")) for item in items}
    if "non_equivalent" in equivalences:
        semantic_equivalence = "non_equivalent"
    elif equivalences == {"exact"}:
        semantic_equivalence = "exact"
    else:
        semantic_equivalence = "unknown"

    expected_row_shape = next(iter(expected_shapes)) if len(expected_shapes) == 1 else None
    return {
        "status": worst_status,
        "row_shape": next(iter(row_shapes)) if len(row_shapes) == 1 else "unknown",
        "expected_row_shape": expected_row_shape,
        "requested_count": requested_count,
        "returned_count": returned_count,
        "coverage": _coverage(returned_count, requested_count),
        "missing": missing,
        "missing_count": len(missing),
        "semantic_equivalence": semantic_equivalence,
        "reason_codes": reason_codes,
    }
