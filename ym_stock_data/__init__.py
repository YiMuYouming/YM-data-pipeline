"""ym-stock-data — 弈沐资本 A 股数据平台

用法:
    from ym_stock_data import query
    data = query("stock_snapshot", codes=["688017"])
"""

__version__ = "2.0.0"

from .api import query
from .fetch import fetch, list_supported

__all__ = ["fetch", "list_supported", "query"]
