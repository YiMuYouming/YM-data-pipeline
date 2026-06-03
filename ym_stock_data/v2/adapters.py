"""Thin adapters around the v1 data pipeline."""

from ym_stock_data import fetch


def fetch_v1(data_type: str, **kwargs) -> dict:
    """Call the existing v1 fetch() contract without changing it."""
    return fetch(data_type, **kwargs)
