"""Versioned live-smoke baseline shared by the runner and acceptance gate."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SmokeCaseSpec:
    case_id: str
    category: str
    intent: str
    params: tuple[tuple[str, object], ...]
    direct_provider: str | None = None
    allow_unattempted_provider_state: bool = False

    def safe_params(self) -> dict:
        return {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in self.params
        }

CURRENT_SMOKE_SCHEMA_VERSION = "2"
CURRENT_SMOKE_BASELINE = "five-source-structured-v1"
CASE_SPECS = (
    SmokeCaseSpec("zero_realtime_market", "zero_auth", "realtime_market", ()),
    SmokeCaseSpec(
        "zero_sector_index",
        "zero_auth",
        "sector_index",
        (("sample_id", "ths_sector"),),
    ),
    SmokeCaseSpec(
        "zero_stock_snapshot",
        "zero_auth",
        "stock_snapshot",
        (("codes", ("600519",)),),
    ),
    SmokeCaseSpec(
        "zero_stock_kline",
        "zero_auth",
        "stock_kline",
        (("code", "600519"), ("period", "daily"), ("count", 3)),
    ),
    SmokeCaseSpec(
        "zero_review_sentiment",
        "zero_auth",
        "review_sentiment",
        (("sample_id", "default_breadth"),),
    ),
    SmokeCaseSpec(
        "zero_market_limit_state", "zero_auth", "market_limit_state", ()
    ),
    SmokeCaseSpec(
        "zero_stock_event",
        "zero_auth",
        "stock_event",
        (("code", "600519"), ("event", "lockup")),
    ),
    SmokeCaseSpec(
        "explicit_wencai",
        "api_key",
        "review_sentiment",
        (("sample_id", "explicit_wencai"), ("limit", 3)),
    ),
    SmokeCaseSpec(
        "explicit_structured_screener",
        "five_source_fallback",
        "review_sentiment",
        (("sample_id", "structured_hs_a"), ("limit", 3)),
        direct_provider="pytdx_screener",
    ),
    SmokeCaseSpec(
        "tdx_probe",
        "owned_oauth",
        "stock_snapshot",
        (("codes", ("600519",)),),
        direct_provider="tdx_quotes",
        allow_unattempted_provider_state=True,
    ),
    SmokeCaseSpec(
        "wind_probe",
        "official_cli",
        "wind_enrichment",
        (("capability", "company_profile"), ("code", "600519")),
        direct_provider="wind_mcp",
        allow_unattempted_provider_state=True,
    ),
)
CURRENT_SMOKE_CASE_IDS = tuple(spec.case_id for spec in CASE_SPECS)

LEGACY_SMOKE_SCHEMA_VERSION = "1"
LEGACY_SMOKE_CASE_COUNT = 10
