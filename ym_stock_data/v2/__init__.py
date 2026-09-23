"""v2 sidecar entry points for YM data pipeline."""

from .capabilities import capability_manifest
from .resolve import resolve

__all__ = ["capability_manifest", "resolve"]
