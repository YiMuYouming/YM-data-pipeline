"""Provider registry exports for the unified data channel."""

from .base import Provider, ProviderOutcome
from .local import LOCAL_PROVIDER_NAMES, LocalProvider

__all__ = [
    "LOCAL_PROVIDER_NAMES",
    "LocalProvider",
    "Provider",
    "ProviderOutcome",
    "TDX_PROVIDER_NAMES",
    "TdxMcpProvider",
    "WIND_PROVIDER_NAMES",
    "WindMcpProvider",
]


def __getattr__(name: str):
    """Load optional MCP providers only when callers explicitly request them."""

    if name in {"TDX_PROVIDER_NAMES", "TdxMcpProvider"}:
        from . import tdx_mcp

        return getattr(tdx_mcp, name)
    if name in {"WIND_PROVIDER_NAMES", "WindMcpProvider"}:
        from . import wind_mcp

        return getattr(wind_mcp, name)
    raise AttributeError(name)
