"""Versioned live-smoke baseline shared by the runner and acceptance gate."""

CURRENT_SMOKE_SCHEMA_VERSION = "2"
CURRENT_SMOKE_BASELINE = "five-source-structured-v1"
CURRENT_SMOKE_CASE_IDS = (
    "zero_realtime_market",
    "zero_sector_index",
    "zero_stock_snapshot",
    "zero_stock_kline",
    "zero_review_sentiment",
    "zero_market_limit_state",
    "zero_stock_event",
    "explicit_wencai",
    "explicit_structured_screener",
    "tdx_probe",
    "wind_probe",
)

LEGACY_SMOKE_SCHEMA_VERSION = "1"
LEGACY_SMOKE_CASE_COUNT = 10
