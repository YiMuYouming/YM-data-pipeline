"""Packaged V3 evidence manifests for the StockToday audit layer."""

from __future__ import annotations

from pathlib import Path


PROBE_MANIFEST_PATH = Path(__file__).with_name("stocktoday-probes.v3.json")

__all__ = ["PROBE_MANIFEST_PATH"]
