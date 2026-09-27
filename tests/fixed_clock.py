"""Fixed trading-session clock so quote freshness tests do not depend on today."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from ym_stock_data import api
from ym_stock_data.contracts import TZ_SHANGHAI

# Thursday 2026-09-24 10:00, inside the continuous trading session.
FIXED_NOW = datetime(2026, 9, 24, 10, 0, tzinfo=TZ_SHANGHAI)
FIXED_NOW_ISO = FIXED_NOW.isoformat(timespec="seconds")


def freeze_trading_clock(testcase) -> None:
    clock = patch.object(api, "_now_shanghai", return_value=FIXED_NOW)
    clock.start()
    testcase.addCleanup(clock.stop)
