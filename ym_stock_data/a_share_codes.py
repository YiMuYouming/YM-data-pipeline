"""Central exchange-specific code families for supported A-share equities."""

from __future__ import annotations


A_SHARE_CODE_PREFIXES_BY_EXCHANGE = {
    "SH": ("600", "601", "603", "605", "688", "689"),
    "SZ": ("000", "001", "002", "003", "300", "301"),
    # Since the October 2025 transition, BSE stocks use the 920 code family.
    "BJ": ("920",),
}


_STAR_MARKET_PREFIXES = ("688", "689")


def is_star_market_code(code: object) -> bool:
    """Return whether a code belongs to the Shanghai STAR market (688/689).

    2026-10-26（批次4 / A3）：科创板行情成交量按股计，其余沪深股票按手计，
    厂商（腾讯）快照/分钟/日 K 三类字段保持同一板块约定，因此该判定是
    成交量单位换算的唯一依据，不按市场前缀散落重复实现。
    """

    return isinstance(code, str) and code.split(".")[0].startswith(_STAR_MARKET_PREFIXES)


def is_supported_a_share_code(code: object, exchange: object) -> bool:
    """Return whether a six-digit code belongs to the allowed stock family."""

    if not isinstance(code, str) or not isinstance(exchange, str):
        return False
    prefixes = A_SHARE_CODE_PREFIXES_BY_EXCHANGE.get(exchange)
    return (
        prefixes is not None
        and len(code) == 6
        and code.isdigit()
        and code.startswith(prefixes)
    )
