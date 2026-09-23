import unittest
from unittest.mock import patch

from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.providers.stocktoday import StockTodayProvider
from ym_stock_data.sources import pytdx


class TransientSourceRetryTests(unittest.TestCase):
    def test_qfq_retries_only_invalid_response_once(self):
        provider = StockTodayProvider(token_loader=lambda: None)
        bad = ProviderOutcome("stocktoday", "provider_error", error_code="INVALID_RESPONSE")
        good = ProviderOutcome("stocktoday", "empty", data={"items": [], "_stocktoday": {}})
        with patch.object(provider, "_request_table", side_effect=[bad, good]) as request:
            outcome = provider.call("stock_kline", {"code": "600519", "period": "daily", "count": 2, "adjustment": "qfq", "source": "stocktoday"})
        self.assertEqual("empty", outcome.status)
        self.assertEqual(2, request.call_count)
        self.assertTrue(all(call.args[0] == "pro_bar" for call in request.call_args_list))

    def test_fund_flow_does_not_retry_auth_failure(self):
        provider = StockTodayProvider(token_loader=lambda: None)
        denied = ProviderOutcome("stocktoday", "auth_error", error_code="AUTH_DENIED")
        with patch.object(provider, "_request_table", return_value=denied) as request:
            outcome = provider.call("fund_flow", {"trade_date": "20260922"})
        self.assertEqual("AUTH_DENIED", outcome.error_code)
        request.assert_called_once()

    def test_failed_quote_read_invalidates_socket_for_next_poll(self):
        class Broken:
            def __init__(self):
                self.closed = False

            def get_security_quotes(self, _codes):
                raise ConnectionResetError()

            def disconnect(self):
                self.closed = True

        broken = Broken()
        old_api = pytdx._api
        old_connected = pytdx._connected_at
        try:
            pytdx._api = broken
            pytdx._connected_at = 1
            with patch.object(pytdx, "_get_api", return_value=broken):
                outcome = pytdx.fetch_quotes(["600519"], fast=True)
            self.assertEqual("PYTDX_DIRECT_UNAVAILABLE", outcome["error_type"])
            self.assertTrue(broken.closed)
            self.assertIsNone(pytdx._api)
        finally:
            pytdx._api = old_api
            pytdx._connected_at = old_connected


if __name__ == "__main__":
    unittest.main()
