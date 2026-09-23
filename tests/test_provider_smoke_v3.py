from __future__ import annotations

import copy
import hashlib
import importlib
import json
import multiprocessing
import os
import signal
import stat
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ym_stock_data import api
from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.routing import all_route_specs


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "ym_stock_data" / "provider_smoke_v3.py"
FIXED_NOW = datetime(2026, 9, 23, 5, 30, 0, tzinfo=TZ_SHANGHAI)
PROVIDER_NAMES = frozenset(api.PROVIDER_REGISTRY)


class _FakeProvider:
    def __init__(self, name: str, outcome: ProviderOutcome | None = None, *, probe_status: str = "configured_unverified"):
        self.name = name
        self.outcome = outcome or ProviderOutcome(
            provider=name,
            status="success",
            data={"items": [{"business_secret": "must-not-persist"}], "fields": ["business_secret"]},
            fetched_at=FIXED_NOW.isoformat(timespec="seconds"),
            latency_ms=7,
            quality={"status": "normal", "returned_count": 1},
            auth={"required": False, "status": "not_required"},
        )
        self.probe_status = probe_status
        self.calls: list[tuple[str, dict]] = []
        self.probes = 0

    def call(self, intent: str, params: dict) -> ProviderOutcome:
        self.calls.append((intent, dict(params)))
        return self.outcome

    def probe(self) -> dict:
        self.probes += 1
        return {"provider": self.name, "status": self.probe_status}


class ProviderSmokeV3Tests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE_PATH.is_file(), "provider-smoke V3 runner/validator is not implemented")
        self.smoke = importlib.import_module("ym_stock_data.provider_smoke_v3")

    def _fake_loader(self, *, outcomes: dict[str, ProviderOutcome] | None = None, delays: set[str] | None = None):
        providers = {}
        outcomes = outcomes or {}
        delays = delays or set()

        class DelayedProvider(_FakeProvider):
            def call(inner_self, intent: str, params: dict) -> ProviderOutcome:
                if inner_self.name in delays:
                    time.sleep(0.08)
                return super().call(intent, params)

        def loader(name: str):
            if name not in providers:
                providers[name] = DelayedProvider(name, outcomes.get(name))
            return providers[name]

        return loader, providers

    def _run_fake(self, *, outcomes=None, delays=None, case_timeout_sec=1.0, global_timeout_sec=30.0):
        loader, providers = self._fake_loader(outcomes=outcomes, delays=delays)
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self.smoke.run_provider_smoke(
                output_root=Path(temp_dir),
                provider_loader=loader,
                now_fn=lambda: FIXED_NOW,
                case_timeout_sec=case_timeout_sec,
                global_timeout_sec=global_timeout_sec,
                _test_only_output_root=True,
            )
            receipt_path = Path(result["receipt"])
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            # Keep the parsed evidence available after TemporaryDirectory exits.
            return result, receipt, providers

    def test_probe_manifest_matches_exact_registry_and_declares_external_pending(self):
        manifest = self.smoke.load_probe_manifest()
        entries = manifest["providers"]
        names = [item["provider"] for item in entries]

        self.assertEqual(PROVIDER_NAMES, set(names))
        expected_pairs = {
            (provider, spec.intent)
            for spec in all_route_specs()
            for provider in spec.providers
        }
        expected_pairs.add(("pytdx_screener", "review_sentiment"))
        direct_pairs = {
            (item["provider"], item["intent"])
            for item in entries
            if item["operation"] == "direct_provider_call"
        }
        self.assertEqual(len(expected_pairs) + 1, len(names))
        self.assertEqual(len(expected_pairs), len(direct_pairs))
        self.assertEqual(
            expected_pairs,
            direct_pairs,
        )
        self.assertEqual(
            {"workbuddy_tencent_stock_mcp", "external_tdx_channel"},
            {item["provider"] for item in manifest["external_pending"]},
        )
        for item in entries:
            self.assertTrue(item["capability"])
            self.assertIn(item["operation"], {"direct_provider_call", "direct_provider_probe"})
            if item["provider"] == "tdx_mcp":
                self.assertEqual("direct_provider_probe", item["operation"])
            else:
                self.assertEqual("direct_provider_call", item["operation"])
                self.assertTrue(item["intent"])

    def test_runner_calls_every_registry_provider_directly_without_canonical_query(self):
        result, receipt, providers = self._run_fake()

        self.assertEqual(len(receipt["providers"]), receipt["case_counts"]["total"])
        self.assertEqual(PROVIDER_NAMES, {item["provider"] for item in receipt["providers"]})
        self.assertEqual(0, receipt["case_counts"].get("timeout", 0))
        self.assertTrue(all(item["direct_probe"] is True for item in receipt["providers"]))
        self.assertEqual(
            len(receipt["providers"]) - 1,
            sum(item["operation"] == "direct_provider_call" for item in receipt["providers"]),
        )
        self.assertEqual(1, sum(item["operation"] == "direct_provider_probe" for item in receipt["providers"]))
        self.assertEqual(str(Path(result["receipt"]).name), "provider-smoke.v3.json")

    def test_degraded_requires_declared_provider_internal_fallback(self):
        fallback = ProviderOutcome(
            provider="pytdx",
            status="success",
            data={"items": [{"code": "600519"}], "fields": ["code"]},
            fetched_at=FIXED_NOW.isoformat(timespec="seconds"),
            quality={"status": "normal", "returned_count": 1},
            provenance={"fallback_from": "eastmoney", "kind": "source_internal", "verified": True},
        )
        outcomes = {"eastmoney": fallback}
        _result, receipt, _providers = self._run_fake(outcomes=outcomes)

        by_name = {item["provider"]: item for item in receipt["providers"]}
        self.assertEqual("degraded", by_name["eastmoney"]["status"])
        self.assertEqual("eastmoney", by_name["eastmoney"]["provenance"]["fallback_from"])
        self.assertEqual("success", by_name["cls"]["status"])
        tampered = copy.deepcopy(receipt)
        tampered["providers"][0]["status"] = "degraded"
        tampered["providers"][0]["provenance"] = {}
        with self.assertRaises(ValueError):
            self.smoke.validate_provider_receipt(tampered)

    def test_each_case_binds_exact_canonical_manifest_entry(self):
        _result, receipt, _providers = self._run_fake()
        for field, value in (
            ("operation", "direct_provider_probe"),
            ("intent", "wrong_intent"),
            ("capability", "wrong_capability"),
            ("direct_probe", False),
        ):
            with self.subTest(field=field):
                tampered = copy.deepcopy(receipt)
                target = next(item for item in tampered["providers"] if item["provider"] == "cls")
                target[field] = value
                with self.assertRaises(ValueError):
                    self.smoke.validate_provider_receipt(tampered)

    def test_effective_provider_replacement_requires_verified_degraded_fallback(self):
        _result, receipt, _providers = self._run_fake()
        target = next(item for item in receipt["providers"] if item["provider"] == "cls")
        mutations = (
            {"status": "success", "provenance": {"effective_provider": "tencent", "fallback_from": "cls", "kind": "source_internal", "verified": True}},
            {"status": "degraded", "provenance": {"effective_provider": "tencent", "fallback_from": "cls", "kind": "source_internal", "verified": False}},
            {"status": "degraded", "provenance": {"effective_provider": "tencent", "fallback_from": "other", "kind": "source_internal", "verified": True}},
            {"status": "success", "provenance": {"effective_provider": "cls", "fallback_from": "tencent", "kind": "source_internal", "verified": True}},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                tampered = copy.deepcopy(receipt)
                target = next(item for item in tampered["providers"] if item["provider"] == "cls")
                target.update(mutation)
                with self.assertRaises(ValueError):
                    self.smoke.validate_provider_receipt(tampered)

    def test_status_consistency_is_fail_closed_even_when_counts_are_resynced(self):
        _result, receipt, _providers = self._run_fake()
        mutations = (
            lambda item: item.update({"status": "success", "row_count": 0, "error_code": None, "error_type": None}),
            lambda item: item.update({"status": "empty", "row_count": 1, "error_code": None, "error_type": None}),
            lambda item: item.update({"status": "configured_unverified", "fields": ["leak"], "schema": {"kind": "mapping", "keys": ["leak"]}}),
            lambda item: item.update({"status": "success", "error_code": "BAD_SUCCESS", "error_type": "provider_error"}),
            lambda item: item.update({"status": "provider_error", "row_count": 0, "error_code": None, "error_type": None}),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                tampered = copy.deepcopy(receipt)
                target = tampered["providers"][0]
                mutate(target)
                counts = {status: 0 for status in self.smoke.PROVIDER_SMOKE_STATUSES}
                for item in tampered["providers"]:
                    counts[item["status"]] += 1
                tampered["case_counts"].update(counts)
                with self.assertRaises(ValueError):
                    self.smoke.validate_provider_receipt(tampered)

    def test_receipt_and_case_time_bounds_are_fail_closed(self):
        _result, receipt, _providers = self._run_fake()
        mutations = (
            lambda value: value.update({"started_at": "2026-09-23T05:31:00+08:00", "completed_at": "2026-09-23T05:30:00+08:00"}),
            lambda value: value["providers"][0].update({"case_started_at": "2026-09-23T05:29:00+08:00"}),
            lambda value: value["providers"][0].update({"case_completed_at": "2026-09-23T05:31:00+08:00"}),
            lambda value: value["providers"][0].update({"observed_at": "2026-09-23T05:31:00+08:00"}),
            lambda value: value["providers"][0].update({"freshness": {"fetched_at": "2026-09-23T05:31:00+08:00", "status": "observed"}}),
            lambda value: value["providers"][0].update({"latency_ms": 999999}),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                tampered = copy.deepcopy(receipt)
                mutate(tampered)
                with self.assertRaises(ValueError):
                    self.smoke.validate_provider_receipt(tampered)

    def test_public_runner_cannot_write_arbitrary_output_root(self):
        loader, _providers = self._fake_loader()
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                self.smoke.run_provider_smoke(
                    output_root=Path(temp_dir),
                    provider_loader=loader,
                    now_fn=lambda: FIXED_NOW,
                    case_timeout_sec=1.0,
                    global_timeout_sec=30.0,
                )

    def test_process_timeout_terminates_signal_swallowing_provider_and_global_budget(self):
        providers = {}
        before_threads = {thread.ident for thread in threading.enumerate()}

        class SignalSwallowingProvider(_FakeProvider):
            def call(inner_self, intent: str, params: dict) -> ProviderOutcome:
                signal.signal(signal.SIGALRM, lambda *_args: None)
                time.sleep(0.2)
                return super().call(intent, params)

        def loader(name: str):
            if name not in providers:
                providers[name] = SignalSwallowingProvider(name)
            return providers[name]

        started = time.monotonic()
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self.smoke.run_provider_smoke(
                output_root=Path(temp_dir),
                provider_loader=loader,
                now_fn=lambda: FIXED_NOW,
                case_timeout_sec=0.01,
                global_timeout_sec=0.04,
                _test_only_output_root=True,
            )
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertGreater(result["case_counts"]["timeout"], 0)
        self.assertEqual([], multiprocessing.active_children())
        self.assertEqual(before_threads, {thread.ident for thread in threading.enumerate()})

    def test_injected_fixture_is_not_live_and_cannot_use_canonical_output(self):
        loader, _providers = self._fake_loader()
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self.smoke.run_provider_smoke(
                output_root=Path(temp_dir),
                provider_loader=loader,
                now_fn=lambda: FIXED_NOW,
                case_timeout_sec=1.0,
                global_timeout_sec=30.0,
                _test_only_output_root=True,
            )
            receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
            self.assertFalse(receipt["live"])
            with self.assertRaises(ValueError):
                self.smoke.run_provider_smoke(
                    output_root=self.smoke.OUTPUT_ROOT,
                    provider_loader=loader,
                    now_fn=lambda: FIXED_NOW,
                    case_timeout_sec=1.0,
                    global_timeout_sec=30.0,
                    _test_only_output_root=True,
                )

    def test_source_binding_includes_baseline_adapter_and_inactive_policy(self):
        _result, receipt, _providers = self._run_fake()
        binding = receipt["source_binding"]
        self.assertEqual(
            "ym-stock-data/smoke/2026-09-23T051625+0800.json",
            binding["baseline_logical_path"],
        )
        self.assertEqual(64, len(binding["baseline_sha256"]))
        self.assertEqual("ym_stock_data/api.py", binding["registry_source"]["logical_path"])
        self.assertTrue(any(item["logical_path"] == "ym_stock_data/providers/local.py" for item in binding["adapter_sources"]))
        self.assertEqual("ym_stock_data/v3/provider-policy.v3.json", binding["policy"]["logical_path"])
        self.assertIs(binding["policy"]["active"], False)

    def test_statuses_and_timeout_are_explicit_and_case_bounds_are_hard(self):
        outcomes = {
            "cninfo": ProviderOutcome("cninfo", "dependency_missing", error_code="DEPENDENCY_MISSING"),
            "cls": ProviderOutcome("cls", "auth_error", error_code="AUTH_MISSING"),
            "eastmoney": ProviderOutcome("eastmoney", "rate_limited", error_code="RATE_LIMITED"),
            "tencent": ProviderOutcome("tencent", "invalid_params", error_code="INVALID_PARAMS"),
            "sina": ProviderOutcome("sina", "provider_error", error_code="UPSTREAM_ERROR"),
        }
        _result, receipt, _providers = self._run_fake(outcomes=outcomes, delays={"tdx_kline"}, case_timeout_sec=0.02)

        by_name = {item["provider"]: item for item in receipt["providers"]}
        self.assertEqual("dependency_missing", by_name["cninfo"]["status"])
        self.assertEqual("auth_error", by_name["cls"]["status"])
        self.assertEqual("rate_limited", by_name["eastmoney"]["status"])
        self.assertEqual("invalid_params", by_name["tencent"]["status"])
        self.assertEqual("provider_error", by_name["sina"]["status"])
        self.assertEqual("timeout", by_name["tdx_kline"]["status"])
        self.assertEqual("TIMEOUT", by_name["tdx_kline"]["error_code"])
        with self.assertRaises(ValueError):
            self.smoke.run_provider_smoke(case_timeout_sec=30.01)
        with self.assertRaises(ValueError):
            self.smoke.run_provider_smoke(global_timeout_sec=900.01)

    def test_receipt_is_metadata_only_and_validator_rejects_tamper(self):
        _result, receipt, _providers = self._run_fake()
        serialized = json.dumps(receipt, ensure_ascii=False)

        self.assertNotIn("business_secret", serialized)
        self.assertNotIn("must-not-persist", serialized)
        self.assertNotIn("Authorization", serialized)
        self.assertNotIn("Cookie", serialized)
        self.assertNotIn(str(Path.home()), serialized)
        self.assertEqual(receipt["source_binding"]["probe_manifest"], "ym_stock_data/v3/provider-smoke.v3.json")
        self.assertEqual(64, len(receipt["source_binding"]["probe_manifest_sha256"]))
        self.smoke.validate_provider_receipt(receipt)

        for mutation in (
            lambda value: value["registry"]["providers"].append("not_registered"),
            lambda value: value["source_binding"].update({"probe_manifest_sha256": "0" * 64}),
            lambda value: value["providers"][0].update({"business_rows": [{"secret": "x"}]}),
            lambda value: value["providers"][0].update({"error_code": "../../escape"}),
            lambda value: value["providers"][0].update({"observed_at": "/Users/yimu/secret"}),
            lambda value: value["source_binding"].update({"baseline_logical_path": "Bearer x"}),
            lambda value: value["source_binding"].update({"baseline_logical_path": "C:\\Users\\secret.json"}),
            lambda value: value["providers"][0].update({"fields": [f"f{i}" for i in range(201)]}),
            lambda value: value["providers"][0].update({"schema": {"kind": "mapping", "keys": [f"f{i}" for i in range(201)]}}),
        ):
            tampered = copy.deepcopy(receipt)
            mutation(tampered)
            with self.assertRaises(ValueError):
                self.smoke.validate_provider_receipt(tampered)

    def test_atomic_receipt_write_is_private_and_rejects_symlink_escape(self):
        _result, receipt, _providers = self._run_fake()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "provider-audit"
            destination = self.smoke.write_provider_receipt(
                receipt, output_root=root, _test_only_output_root=True
            )
            self.assertEqual("provider-smoke.v3.json", destination.name)
            self.assertEqual(0o700, stat.S_IMODE(destination.parent.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(destination.stat().st_mode))
            self.assertFalse(any(path.name.startswith(".") for path in destination.parent.iterdir()))

            outside = Path(temp_dir) / "outside"
            outside.mkdir()
            symlink_root = Path(temp_dir) / "symlink-root"
            symlink_root.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                self.smoke.write_provider_receipt(receipt, output_root=symlink_root)

    def test_validator_requires_exact_case_counts_and_external_channels_are_not_cases(self):
        _result, receipt, _providers = self._run_fake()
        self.assertEqual(len(receipt["providers"]), receipt["case_counts"]["total"])
        self.assertEqual(
            receipt["case_counts"]["total"],
            sum(receipt["case_counts"][status] for status in self.smoke.PROVIDER_SMOKE_STATUSES),
        )
        self.assertEqual(0, receipt["case_counts"].get("external_pending", 0))
        self.assertEqual(2, len(receipt["external_pending"]))

        tampered = copy.deepcopy(receipt)
        tampered["case_counts"]["total"] = 25
        with self.assertRaises(ValueError):
            self.smoke.validate_provider_receipt(tampered)


if __name__ == "__main__":
    unittest.main()
