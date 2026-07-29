#!/usr/bin/env python3
"""Print a metadata-only public API comparison; never invent manual source state."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ym_stock_data import query
from ym_stock_data.doctor import collect_diagnostics
from ym_stock_data.smoke import summarize_query_result


def _safe_query(query_fn: Callable, intent: str, **params) -> dict:
    try:
        return summarize_query_result(query_fn(intent, **params))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return {
            "status": "error",
            "provider_used": None,
            "attempts": [],
            "row_count": 0,
            "error_code": "UNHANDLED_EXCEPTION",
        }


def build_comparison(
    *,
    query_fn: Callable = query,
    diagnostics_fn: Callable[[], dict] = collect_diagnostics,
) -> dict:
    try:
        diagnostics = diagnostics_fn()
    except Exception:
        diagnostics = {"providers": {}}
    providers = diagnostics.get("providers") if isinstance(diagnostics, dict) else {}
    safe_providers = {}
    for name in ("tdx_mcp", "wind_mcp"):
        item = providers.get(name) if isinstance(providers, dict) else None
        status = item.get("status") if isinstance(item, dict) else "unavailable"
        safe_providers[name] = {"status": str(status)}
    return {
        "queried_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "queries": {
            "review_sentiment": _safe_query(
                query_fn,
                "review_sentiment",
                query="A股 非ST 涨停",
                limit=3,
            ),
            "market_limit_state": _safe_query(query_fn, "market_limit_state"),
        },
        "providers": safe_providers,
    }


def main() -> int:
    print(json.dumps(build_comparison(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
