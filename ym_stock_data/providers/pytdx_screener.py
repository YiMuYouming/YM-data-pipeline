"""Dedicated zero-auth PyTDX provider for a constrained structured screen."""

from __future__ import annotations

import socket
import time
import unicodedata
from collections.abc import Callable, Iterable
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from ..a_share_codes import is_supported_a_share_code
from ..config import PYTDX_CONNECT_TIMEOUT, PYTDX_SERVERS
from ..pytdx_screener_query import (
    CompiledPytdxScreenerQuery,
    compile_pytdx_screener_query,
)
from ..sources.pytdx import _load_tdx_hq_api
from .base import ProviderOutcome


COMPILER_VERSION = "pytdx-structured-1"
QUOTE_BATCH_SIZE = 80


class _PayloadError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _stock_family(market: int, code: str) -> bool:
    exchange = "SH" if market == 1 else "SZ"
    return is_supported_a_share_code(code, exchange)


def _is_st(name: str) -> bool:
    normalized = unicodedata.normalize("NFKC", name).upper()
    normalized = "".join(normalized.split())
    return normalized.startswith(("*ST", "ST", "S*ST", "SST"))


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


class PytdxScreenerProvider:
    name = "pytdx_screener"

    def __init__(
        self,
        *,
        api_factory: Callable[[], object] | None = None,
        servers: Iterable[tuple[str, int]] = PYTDX_SERVERS,
        connect_timeout: float = PYTDX_CONNECT_TIMEOUT,
    ):
        self._api_factory = api_factory
        self._servers = tuple(servers)
        self._connect_timeout = connect_timeout

    def probe(self) -> dict:
        return {
            "provider": self.name,
            "status": "configured_unverified",
            "auth": {"required": False, "status": "not_required"},
        }

    def call(self, intent: str, params: dict) -> ProviderOutcome:
        started = time.perf_counter()
        if intent != "review_sentiment" or not isinstance(params, dict):
            return self._failure(started, "incompatible", "INCOMPATIBLE_INTENT")
        compiled = compile_pytdx_screener_query(params.get("query"))
        if compiled is None:
            return self._failure(
                started, "incompatible", "PYTDX_SCREENER_INCOMPATIBLE"
            )
        raw_limit = params.get("limit", 50)
        if isinstance(raw_limit, bool):
            return self._failure(started, "incompatible", "PYTDX_INVALID_LIMIT")
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return self._failure(started, "incompatible", "PYTDX_INVALID_LIMIT")
        if limit <= 0:
            return self._failure(started, "incompatible", "PYTDX_INVALID_LIMIT")

        last_status = "network_error"
        last_code = "PYTDX_CONNECT_FAILED"
        for host, port in self._servers:
            api = None
            try:
                factory = self._api_factory
                api = factory() if factory is not None else _load_tdx_hq_api()()
                connected = api.connect(
                    host,
                    port,
                    time_out=self._connect_timeout,
                )
                if not connected:
                    continue
                payload = self._screen(api, compiled, limit=limit)
            except ImportError:
                return self._failure(
                    started, "dependency_missing", "PYTDX_DEPENDENCY_MISSING"
                )
            except (TimeoutError, socket.timeout):
                last_status, last_code = "timeout", "PYTDX_TIMEOUT"
                continue
            except _PayloadError as error:
                last_status, last_code = "provider_error", error.code
                continue
            except Exception:
                last_status, last_code = "provider_error", "PYTDX_PROVIDER_ERROR"
                continue
            finally:
                if api is not None:
                    try:
                        api.disconnect()
                    except Exception:
                        pass

            rows = payload["datas"]
            status = "success" if rows else "empty"
            return ProviderOutcome(
                provider=self.name,
                status=status,
                data=payload,
                latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
                quality={
                    "status": "normal" if rows else "empty",
                    "returned_count": len(rows),
                    "reason_codes": ["truncated"] if payload["truncated"] else [],
                },
                auth={"required": False, "status": "not_required"},
            )
        return self._failure(started, last_status, last_code)

    def _screen(
        self,
        api,
        compiled: CompiledPytdxScreenerQuery,
        *,
        limit: int,
    ) -> dict:
        catalogue = self._complete_catalogue(api, compiled.markets)
        candidates = [
            (market, code, name)
            for market, code, name in catalogue
            if (compiled.code is None or code == compiled.code)
            and (not compiled.exclude_st or not _is_st(name))
        ]
        quote_keys = [(market, code) for market, code, _name in candidates]
        quote_rows = self._complete_quotes(api, quote_keys)
        names = {(market, code): name for market, code, name in candidates}
        matched = []
        excluded_invalid = 0
        for market, code in quote_keys:
            row = quote_rows[(market, code)]
            price = _decimal(row.get("price"))
            last_close = _decimal(row.get("last_close"))
            if price is None or last_close is None or price < 0 or last_close <= 0:
                raise _PayloadError("PYTDX_INVALID_QUOTE")
            if price == 0:
                excluded_invalid += 1
                continue
            pct_change = (
                (price - last_close) / last_close * Decimal("100")
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if not compiled.matches(
                price=float(price), pct_change=float(pct_change)
            ):
                continue
            matched.append(
                {
                    "股票代码": code,
                    "股票简称": names[(market, code)],
                    "交易所": "SH" if market == 1 else "SZ",
                    "最新价": float(price),
                    "昨收": float(last_close),
                    "涨幅": float(pct_change),
                }
            )
        if quote_keys and excluded_invalid == len(quote_keys):
            raise _PayloadError("PYTDX_QUOTES_NOT_READY")
        matched.sort(key=lambda row: row["股票代码"])
        selected = matched[:limit]
        return {
            "datas": selected,
            "row_count": len(selected),
            "matched_count": len(matched),
            "scanned_count": len(quote_keys),
            "excluded_invalid_quote_count": excluded_invalid,
            "truncated": len(matched) > len(selected),
            "compiler_version": COMPILER_VERSION,
        }

    @staticmethod
    def _complete_catalogue(
        api, markets: tuple[int, ...]
    ) -> list[tuple[int, str, str]]:
        result: list[tuple[int, str, str]] = []
        seen: set[tuple[int, str]] = set()
        for market in markets:
            try:
                count = int(api.get_security_count(market))
            except Exception as error:
                raise _PayloadError("PYTDX_DIRECTORY_INCOMPLETE") from error
            if count <= 0:
                raise _PayloadError("PYTDX_DIRECTORY_INCOMPLETE")
            raw_rows = []
            for start in range(0, count, 1000):
                try:
                    page = api.get_security_list(market, start)
                except Exception as error:
                    raise _PayloadError("PYTDX_DIRECTORY_INCOMPLETE") from error
                if not isinstance(page, list):
                    raise _PayloadError("PYTDX_DIRECTORY_INCOMPLETE")
                raw_rows.extend(page)
            if len(raw_rows) != count:
                raise _PayloadError("PYTDX_DIRECTORY_INCOMPLETE")
            for row in raw_rows:
                if not isinstance(row, dict):
                    raise _PayloadError("PYTDX_DIRECTORY_INCOMPLETE")
                code = row.get("code")
                if not isinstance(code, str) or not _stock_family(market, code):
                    continue
                name = row.get("name")
                key = (market, code)
                if not isinstance(name, str) or not name.strip() or key in seen:
                    raise _PayloadError("PYTDX_DIRECTORY_INCOMPLETE")
                result.append((market, code, name.strip()))
                seen.add(key)
        return result

    @staticmethod
    def _complete_quotes(
        api, keys: list[tuple[int, str]]
    ) -> dict[tuple[int, str], dict]:
        result: dict[tuple[int, str], dict] = {}
        expected_by_code = {code: (market, code) for market, code in keys}
        for start in range(0, len(keys), QUOTE_BATCH_SIZE):
            batch = keys[start : start + QUOTE_BATCH_SIZE]
            try:
                rows = api.get_security_quotes(batch)
            except Exception as error:
                raise _PayloadError("PYTDX_QUOTE_INCOMPLETE") from error
            if not isinstance(rows, list):
                raise _PayloadError("PYTDX_QUOTE_INCOMPLETE")
            if len(rows) < len(batch):
                raise _PayloadError("PYTDX_QUOTE_INCOMPLETE")
            if len(rows) != len(batch):
                raise _PayloadError("PYTDX_INVALID_QUOTE")
            batch_keys = set(batch)
            for row in rows:
                if not isinstance(row, dict):
                    raise _PayloadError("PYTDX_INVALID_QUOTE")
                code = row.get("code")
                key = expected_by_code.get(code) if isinstance(code, str) else None
                if key not in batch_keys or key in result:
                    raise _PayloadError("PYTDX_INVALID_QUOTE")
                result[key] = row
        if len(result) != len(keys):
            raise _PayloadError("PYTDX_QUOTE_INCOMPLETE")
        return result

    def _failure(
        self, started: float, status: str, error_code: str
    ) -> ProviderOutcome:
        return ProviderOutcome(
            provider=self.name,
            status=status,
            error_code=error_code,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            auth={"required": False, "status": "not_required"},
        )
