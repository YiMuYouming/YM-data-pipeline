import threading
import time
import unittest
from unittest.mock import Mock, patch

from ym_stock_data.sources import eastmoney, research
from ym_stock_data.sources.eastmoney_http import BreakerResponse, EastmoneyClient


class EastmoneyClientTests(unittest.TestCase):
    def test_second_request_waits_for_minimum_interval(self):
        client = EastmoneyClient(min_interval=1.0, jitter=(0.0, 0.0))
        response = Mock(status_code=200)
        with (
            patch.object(client.session, "get", return_value=response),
            patch(
                "ym_stock_data.sources.eastmoney_http.time.monotonic",
                side_effect=[10.0, 10.0, 10.2, 11.0],
            ),
            patch("ym_stock_data.sources.eastmoney_http.time.sleep") as sleep,
        ):
            client.get("https://example.eastmoney.com/a")
            client.get("https://example.eastmoney.com/b")
        sleep.assert_called_once_with(0.8)

    def test_session_get_calls_are_serialized_across_threads(self):
        client = EastmoneyClient(min_interval=0, jitter=(0.0, 0.0))
        start = threading.Barrier(3)
        state_lock = threading.Lock()
        active = 0
        max_active = 0

        def fake_get(*args, **kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with state_lock:
                active -= 1
            return Mock(status_code=200)

        def worker():
            start.wait()
            client.get("https://example.eastmoney.com/a")

        with patch.object(client.session, "get", side_effect=fake_get):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join()

        self.assertEqual(1, max_active)

    def test_403_opens_breaker_without_immediate_retry(self):
        client = EastmoneyClient(min_interval=0, breaker_seconds=60)
        response = Mock(status_code=403)
        with (
            patch.object(client.session, "get", return_value=response) as get,
            patch(
                "ym_stock_data.sources.eastmoney_http.time.monotonic",
                return_value=100.0,
            ),
        ):
            first = client.get("https://example.eastmoney.com/a")
            second = client.get("https://example.eastmoney.com/a")
        self.assertEqual(403, first.status_code)
        self.assertTrue(second.skipped_by_breaker)
        get.assert_called_once()

    def test_429_opens_longer_rate_breaker(self):
        client = EastmoneyClient(
            min_interval=0,
            breaker_seconds=60,
            rate_breaker_seconds=300,
        )
        response = Mock(status_code=429)
        with (
            patch.object(client.session, "get", return_value=response) as get,
            patch(
                "ym_stock_data.sources.eastmoney_http.time.monotonic",
                side_effect=[100.0, 100.0, 200.0],
            ),
        ):
            first = client.get("https://example.eastmoney.com/a")
            second = client.get("https://example.eastmoney.com/a")
        self.assertEqual(429, first.status_code)
        self.assertTrue(second.skipped_by_breaker)
        get.assert_called_once()


class EastmoneyCallerTests(unittest.TestCase):
    @patch("ym_stock_data.sources.eastmoney.CLIENT.get")
    def test_dragon_tiger_exposes_open_breaker(self, get):
        get.return_value = BreakerResponse()

        result = eastmoney.fetch_daily_dragon_tiger("2026-07-14")

        self.assertEqual("eastmoney_breaker_open", result["error"])
        self.assertEqual("breaker_open", result["error_type"])
        self.assertEqual("none", result["_source"])

    @patch("ym_stock_data.sources.research.CLIENT.get")
    def test_research_exposes_open_breaker(self, get):
        get.return_value = BreakerResponse()

        result = research.fetch_reports("600519", max_pages=1)

        self.assertEqual("eastmoney_breaker_open", result["error"])
        self.assertEqual("breaker_open", result["error_type"])
        self.assertEqual("none", result["_source"])

if __name__ == "__main__":
    unittest.main()
