"""Central exchange-specific code families for supported A-share equities."""

from __future__ import annotations


A_SHARE_CODE_PREFIXES_BY_EXCHANGE = {
    "SH": ("600", "601", "603", "605", "688", "689"),
    "SZ": ("000", "001", "002", "003", "300", "301"),
    # Since the October 2025 transition, BSE stocks use the 920 code family.
    "BJ": ("920",),
}


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
