"""ym-stock-data — 弈沐资本 A 股数据平台

用法:
    from ym_stock_data import fetch
    data = fetch("quotes", codes=["688017"])
"""

__version__ = "0.1.0"

from .fetch import fetch, list_supported

__all__ = ["fetch", "list_supported"]
