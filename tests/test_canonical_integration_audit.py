from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs/audit/2026-07-29-canonical-integration-readiness.md"

EXPECTED_CLASSIFICATIONS = {
    "README.md": "incorporated_and_evolved",
    "tests/test_v2_capabilities.py": "incorporated_and_evolved",
    "ym_stock_data/v2/capabilities.py": "incorporated_and_evolved",
    "docs/Wind-MCP-补充源验证清单.md": "incorporated_and_evolved",
    "docs/handoffs/2026-07-22-wind-mcp-sidecar-handoff.md": "incorporated_and_evolved",
    "docs/superpowers/plans/2026-07-29-unified-a-share-data-channel.md": "exact_same",
    "tests/test_wind_sidecar.py": "intentionally_removed_after_parity",
    "ym_stock_data/experimental/__init__.py": "intentionally_removed_after_parity",
    "ym_stock_data/experimental/__pycache__/__init__.cpython-314.pyc": "generated_binary_excluded",
    "ym_stock_data/experimental/__pycache__/wind_sidecar.cpython-314.pyc": "generated_binary_excluded",
    "ym_stock_data/experimental/wind_sidecar.py": "intentionally_removed_after_parity",
}


class CanonicalIntegrationAuditTests(unittest.TestCase):
    def test_frozen_report_covers_every_dirty_path_without_live_checkout_dependency(self):
        text = REPORT.read_text(encoding="utf-8")
        inventory = text.split(
            "<!-- canonical-dirty-inventory:start -->", 1
        )[1].split("<!-- canonical-dirty-inventory:end -->", 1)[0]
        rows = {}
        for line in inventory.splitlines():
            if not line.startswith("| `"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            rows[cells[0].strip("`")] = {
                "status": cells[1],
                "sha256": cells[2].strip("`"),
                "classification": cells[3].strip("`"),
            }

        self.assertEqual(EXPECTED_CLASSIFICATIONS, {
            path: row["classification"] for path, row in rows.items()
        })
        for path, row in rows.items():
            with self.subTest(path=path):
                self.assertIn(
                    row["status"],
                    {"tracked_modified", "untracked", "ignored_generated"},
                )
                self.assertRegex(row["sha256"], r"^[0-9a-f]{64}$")

        generated = {
            path for path, row in rows.items()
            if row["classification"] == "generated_binary_excluded"
        }
        self.assertEqual(
            {
                "ym_stock_data/experimental/__pycache__/__init__.cpython-314.pyc",
                "ym_stock_data/experimental/__pycache__/wind_sidecar.cpython-314.pyc",
            },
            generated,
        )

    def test_report_freezes_missing_public_query_and_recovery_boundaries(self):
        text = REPORT.read_text(encoding="utf-8")
        self.assertIn("canonical_has_public_query: false", text)
        self.assertIn("canonical_resolver_exception: ImportError", text)
        self.assertIn("canonical_protected_leaf_paths: 11", text)
        self.assertIn("unresolved_conflict: 0", text)
        for ref in ("f246fef", "edf1ca7", "88fa1a1"):
            with self.subTest(ref=ref):
                self.assertIn(ref, text)
        self.assertNotRegex(text, re.compile(r"(?i)(access|refresh)[_-]?token\s*[:=]"))


if __name__ == "__main__":
    unittest.main()
