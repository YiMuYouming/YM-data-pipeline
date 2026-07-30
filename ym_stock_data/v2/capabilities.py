"""Versioned, network-free capability discovery for research consumers."""

from __future__ import annotations

from .. import api
from ..providers.tdx_mcp import TDX_DIAGNOSTIC_NAMES
from ..providers.wind_mcp import WIND_ENRICHMENT_CAPABILITIES, WIND_PROVIDER_NAMES
from ..routing import all_route_specs


CAPABILITY_SCHEMA_VERSION = "1.0"

_V2_INTENT_STATUS = {
    "realtime_market": "stable",
    "sector_index": "stable",
    "stock_snapshot": "stable",
    "stock_kline": "stable",
    "review_sentiment": "stable",
    "market_limit_state": "experimental",
    "stock_event": "experimental",
}
_V1_ROUTE_STATUS = {
    "limit_state": "experimental",
    "market_limit_state": "experimental",
    "stock_event": "experimental",
    "iwencai_content": "experimental",
    "industry_research": "experimental",
    "research": "stable",
    "filings": "stable",
    "news": "stable",
}


def _routes_for(provider_names: set[str]) -> list[str]:
    return sorted(
        {
            spec.intent
            for spec in all_route_specs()
            if provider_names.intersection(spec.providers)
        }
    )


def capability_manifest() -> dict:
    """Derive provider availability from the canonical registry and routes."""

    registry_names = set(api.PROVIDER_REGISTRY)
    tdx_names = set(TDX_DIAGNOSTIC_NAMES)
    wind_names = set(WIND_PROVIDER_NAMES)
    tdx_routes = _routes_for(tdx_names)
    wind_routes = _routes_for(wind_names)
    providers = {
        "tdx_mcp": {
            "status": "registered_optional",
            "registered": tdx_names.issubset(registry_names),
            "provider_names": sorted(tdx_names),
            "routes": tdx_routes,
            "automatic_fallback_intents": tdx_routes,
            "default_route": False,
        },
        "wind_mcp": {
            "status": "registered_experimental",
            "registered": wind_names.issubset(registry_names),
            "provider_names": sorted(wind_names),
            "routes": wind_routes,
            "automatic_fallback_intents": [
                intent for intent in wind_routes if intent != "wind_enrichment"
            ],
            "explicit_intents": [
                intent for intent in wind_routes if intent == "wind_enrichment"
            ],
            "default_route": False,
            "capabilities": sorted(
                [*WIND_ENRICHMENT_CAPABILITIES, "stock_screener"]
            ),
        },
    }
    return {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "v2_intents": {
            intent: {"status": status}
            for intent, status in _V2_INTENT_STATUS.items()
        },
        "v1_routes": {
            route: {"status": status}
            for route, status in _V1_ROUTE_STATUS.items()
        },
        "providers": providers,
        # Compatibility alias for old manifest consumers. These entries are
        # projections of the derived provider records, not a second inventory.
        "manual_sources": {
            name: dict(value) for name, value in providers.items()
        },
    }
