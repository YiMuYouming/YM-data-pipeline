"""东方财富域名的线程安全请求治理。"""

from __future__ import annotations

from dataclasses import dataclass
import random
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ym_stock_data.config import (
    EASTMONEY_BREAKER_SECONDS,
    EASTMONEY_JITTER_MAX,
    EASTMONEY_JITTER_MIN,
    EASTMONEY_MIN_INTERVAL,
    EASTMONEY_RATE_BREAKER_SECONDS,
)


@dataclass
class BreakerResponse:
    status_code: int = 0
    skipped_by_breaker: bool = True
    reason: str = "eastmoney_breaker_open"

    def json(self) -> dict:
        return {"error": self.reason}


class EastmoneyClient:
    def __init__(
        self,
        min_interval: float = EASTMONEY_MIN_INTERVAL,
        jitter: tuple[float, float] = (
            EASTMONEY_JITTER_MIN,
            EASTMONEY_JITTER_MAX,
        ),
        breaker_seconds: float = EASTMONEY_BREAKER_SECONDS,
        rate_breaker_seconds: float = EASTMONEY_RATE_BREAKER_SECONDS,
    ) -> None:
        self.min_interval = min_interval
        self.jitter = jitter
        self.breaker_seconds = breaker_seconds
        self.rate_breaker_seconds = rate_breaker_seconds
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.6,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._breaker_until = 0.0

    def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: float = 15,
        **kwargs,
    ):
        with self._lock:
            now = time.monotonic()
            if now < self._breaker_until:
                return BreakerResponse()
            wait = self.min_interval - (now - self._last_call)
            if wait > 0:
                wait += random.uniform(*self.jitter)
                time.sleep(round(wait, 10))
            response = self.session.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
                **kwargs,
            )
            self._last_call = time.monotonic()
            if response.status_code == 403:
                self._breaker_until = self._last_call + self.breaker_seconds
            elif response.status_code == 429:
                self._breaker_until = (
                    self._last_call + self.rate_breaker_seconds
                )
            return response


CLIENT = EastmoneyClient()

DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def query_datacenter(
    report_name: str,
    *,
    filter_str: str = "",
    page_size: int = 50,
    sort_columns: str = "",
    sort_types: str = "-1",
) -> list[dict]:
    response = CLIENT.get(
        DATACENTER_URL,
        params={
            "reportName": report_name,
            "columns": "ALL",
            "filter": filter_str,
            "pageNumber": "1",
            "pageSize": str(page_size),
            "sortColumns": sort_columns,
            "sortTypes": sort_types,
            "source": "WEB",
            "client": "WEB",
        },
        headers={"Referer": "https://data.eastmoney.com/"},
        timeout=15,
    )
    if getattr(response, "skipped_by_breaker", False) is True:
        raise RuntimeError(response.reason)
    response.raise_for_status()
    payload = response.json()
    return ((payload.get("result") or {}).get("data") or [])
