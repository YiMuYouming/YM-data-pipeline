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
    evidence_kind: str = "canonical_result"
    capability: str = "canonical_query"

    def safe_params(self) -> dict:
        return {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in self.params
        }

CURRENT_SMOKE_SCHEMA_VERSION = "2"
CURRENT_SMOKE_BASELINE = "five-source-capabilities-v1"
CASE_SPECS = (
    SmokeCaseSpec("zero_realtime_market", "zero_auth", "realtime_market", (), capability="realtime_market"),
    SmokeCaseSpec(
        "zero_sector_index",
        "zero_auth",
        "sector_index",
        (("sample_id", "ths_sector"),),
        capability="sector_index",
    ),
    SmokeCaseSpec(
        "zero_stock_snapshot",
        "zero_auth",
        "stock_snapshot",
        (("fixture_id", "large_cap_a"),),
        capability="stock_snapshot",
    ),
    SmokeCaseSpec(
        "zero_stock_kline",
        "zero_auth",
        "stock_kline",
        (("fixture_id", "large_cap_a"), ("period", "daily"), ("count", 3)),
        capability="stock_kline",
    ),
    SmokeCaseSpec(
        "zero_review_sentiment",
        "zero_auth",
        "review_sentiment",
        (("sample_id", "default_breadth"),),
        capability="market_breadth",
    ),
    SmokeCaseSpec(
        "zero_market_limit_state", "zero_auth", "market_limit_state", (), capability="market_limit_state"
    ),
    SmokeCaseSpec(
        "zero_stock_event",
        "zero_auth",
        "stock_event",
        (("fixture_id", "large_cap_a"), ("event", "lockup")),
        capability="stock_event",
    ),
    SmokeCaseSpec(
        "explicit_wencai",
        "api_key",
        "review_sentiment",
        (("sample_id", "explicit_wencai"), ("limit", 3)),
        capability="canonical_screener",
    ),
    SmokeCaseSpec(
        "explicit_structured_screener",
        "five_source_fallback",
        "review_sentiment",
        (("sample_id", "structured_hs_a"), ("limit", 3)),
        direct_provider="pytdx_screener",
        evidence_kind="direct_provider_result",
        capability="structured_screener",
    ),
    SmokeCaseSpec(
        "tdx_probe",
        "owned_oauth",
        "stock_snapshot",
        (("fixture_id", "large_cap_a"),),
        direct_provider="tdx_quotes",
        allow_unattempted_provider_state=True,
        evidence_kind="tdx_protocol_result",
        capability="quotes",
    ),
    SmokeCaseSpec(
        "wind_probe",
        "official_cli",
        "wind_enrichment",
        (("capability", "company_profile"), ("fixture_id", "large_cap_a")),
        direct_provider="wind_mcp",
        allow_unattempted_provider_state=True,
        evidence_kind="direct_provider_result",
        capability="enrichment",
    ),
    SmokeCaseSpec(
        "direct_openapi_screener", "api_key", "review_sentiment",
        (("sample_id", "structured_hs_a"), ("limit", 3)),
        direct_provider="iwencai_openapi", allow_unattempted_provider_state=True,
        evidence_kind="direct_provider_result", capability="screener",
    ),
    SmokeCaseSpec(
        "direct_pywencai_screener", "local_runtime", "review_sentiment",
        (("sample_id", "structured_hs_a"), ("limit", 3)),
        direct_provider="pywencai", allow_unattempted_provider_state=True,
        evidence_kind="direct_provider_result", capability="screener",
    ),
    SmokeCaseSpec(
        "tdx_screener_probe", "owned_oauth", "review_sentiment",
        (("sample_id", "structured_hs_a"), ("limit", 3)),
        direct_provider="tdx_screener", allow_unattempted_provider_state=True,
        evidence_kind="tdx_protocol_result", capability="screener",
    ),
    SmokeCaseSpec(
        "tdx_kline_probe", "owned_oauth", "stock_kline",
        (("fixture_id", "large_cap_a"), ("period", "daily"), ("count", 3)),
        direct_provider="tdx_kline", allow_unattempted_provider_state=True,
        evidence_kind="tdx_protocol_result", capability="kline",
    ),
    SmokeCaseSpec(
        "tdx_report_probe", "owned_oauth", "research",
        (("fixture_id", "large_cap_a"), ("days", 365)),
        direct_provider="tdx_report", allow_unattempted_provider_state=True,
        evidence_kind="tdx_protocol_result", capability="report",
    ),
    SmokeCaseSpec(
        "tdx_notice_probe", "owned_oauth", "filings",
        (("fixture_id", "large_cap_a"), ("days", 365)),
        direct_provider="tdx_notice", allow_unattempted_provider_state=True,
        evidence_kind="tdx_protocol_result", capability="notice",
    ),
    SmokeCaseSpec(
        "tdx_news_probe", "owned_oauth", "news", (("limit", 3),),
        direct_provider="tdx_news", allow_unattempted_provider_state=True,
        evidence_kind="tdx_protocol_result", capability="news",
    ),
    SmokeCaseSpec(
        "wind_screener_probe", "official_cli", "review_sentiment",
        (("sample_id", "structured_hs_a"), ("limit", 3)),
        direct_provider="wind_screener", allow_unattempted_provider_state=True,
        evidence_kind="direct_provider_result", capability="screener",
    ),
    SmokeCaseSpec(
        "wind_filings_probe", "official_cli", "filings",
        (("fixture_id", "large_cap_a"), ("days", 365), ("max_pages", 1)),
        direct_provider="wind_documents", allow_unattempted_provider_state=True,
        evidence_kind="direct_provider_result", capability="filings",
    ),
    SmokeCaseSpec(
        "canonical_five_source_fallback", "five_source_fallback", "review_sentiment",
        (("sample_id", "structured_hs_a"), ("limit", 3)),
        evidence_kind="controlled_canonical_route", capability="five_source_fallback",
    ),
)
CURRENT_SMOKE_CASE_IDS = tuple(spec.case_id for spec in CASE_SPECS)

LEGACY_SMOKE_SCHEMA_VERSION = "1"
LEGACY_SMOKE_CASE_COUNT = 10
