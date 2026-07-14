"""Versioned, network-free capability discovery for research consumers."""

from __future__ import annotations

from copy import deepcopy


CAPABILITY_SCHEMA_VERSION = "1.0"

_MANIFEST = {
    "schema_version": CAPABILITY_SCHEMA_VERSION,
    "v2_intents": {
        "realtime_market": {"status": "stable"},
        "sector_index": {"status": "stable"},
        "stock_snapshot": {"status": "stable"},
        "stock_kline": {"status": "stable"},
        "review_sentiment": {"status": "stable"},
    },
    "v1_routes": {
        "limit_state": {"status": "experimental"},
        "market_limit_state": {"status": "experimental"},
        "stock_event": {"status": "experimental"},
        "iwencai_content": {"status": "experimental"},
        "industry_research": {"status": "experimental"},
        "research": {"status": "stable"},
        "filings": {"status": "stable"},
        "news": {"status": "stable"},
    },
    "manual_sources": {
        "tdx_mcp": {"status": "manual_cross_check_only"},
    },
}


def capability_manifest() -> dict:
    """Return an isolated capability manifest without performing I/O."""
    return deepcopy(_MANIFEST)
