from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import ym_stock_data.api as api
from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome

try:
    import ym_stock_data.provider_policy as provider_policy
except ModuleNotFoundError:
    provider_policy = None


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=TZ_SHANGHAI)


class FakeProvider:
    def __init__(self, name: str, outcomes: list[ProviderOutcome]):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict]] = []

    def call(self, intent: str, params: dict) -> ProviderOutcome:
        self.calls.append((intent, params))
        outcome = self.outcomes.pop(0)
        return outcome


def outcome(provider: str, status: str, *, data=None, error_code=None) -> ProviderOutcome:
    return ProviderOutcome(
        provider=provider,
        status=status,
        data=data,
        error_code=error_code,
        latency_ms=1,
        fetched_at="2026-09-22T12:00:00+08:00",
    )


def full_snapshot_data(code: str = "600519") -> dict:
    now = datetime.now(TZ_SHANGHAI).isoformat(timespec="seconds")
    return {
        code: {
            "price": 1500.0,
            "last_close": 1490.0,
            "open": 1495.0,
            "high": 1510.0,
            "low": 1485.0,
            "volume": 1000.0,
            "amount": 1500000.0,
            "quote_time": now,
        }
    }


def fresh_snapshot_outcome(provider: str, code: str = "600519") -> ProviderOutcome:
    return ProviderOutcome(
        provider=provider,
        status="success",
        data=full_snapshot_data(code),
        latency_ms=1,
        fetched_at=datetime.now(TZ_SHANGHAI).isoformat(timespec="seconds"),
    )


class ProviderPolicyV3Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state = ProviderState(Path(self.temp_dir.name) / "providers.sqlite3")

    def policy_module(self):
        self.assertIsNotNone(
            provider_policy,
            "provider_policy.py is required before policy behavior can be verified",
        )
        return provider_policy

    def receipt_for_probe(
        self,
        method: str,
        *,
        category: str | None = None,
        nested_overrides: dict | None = None,
        columns_override: list[str] | None = None,
    ) -> dict:
        from ym_stock_data import stocktoday_audit as audit
        from ym_stock_data.providers.stocktoday_inventory import method_map

        probe = copy.deepcopy(audit.load_probes()["methods"][method][0])
        if nested_overrides:
            probe["params"]["params"].update(nested_overrides)
        inventory = method_map()[method]
        case = audit._normalise_case(
            {
                "method": method,
                "probe_id": probe["probe_id"],
                "category": category or inventory["category"],
                "params": probe["params"],
                "fixed_date": probe["fixed_date"],
                "duplicate_key_fields": probe["duplicate_key_fields"],
                "filter_checks": probe["filter_checks"],
                "expected_fields": probe["expected_fields"],
                "pagination": probe["pagination"],
                "primary_declared": probe["primary_declared"],
            }
        )
        nested = case["params"]["params"]
        if method == "rt_k":
            columns = ["ts_code", "close", "updated_at"]
            rows = [
                {
                    "ts_code": "300001.SZ",
                    "close": 10.0,
                    "updated_at": "2026-09-22T11:59:00",
                }
            ]
        elif method in {"rt_min", "stk_mins"}:
            columns = [
                "ts_code",
                "trade_date",
                "trade_time",
                "open",
                "high",
                "low",
                "close",
            ]
            rows = [
                {
                    "ts_code": nested["ts_code"],
                    "trade_date": "2025-06-20",
                    "trade_time": "2025-06-20 10:00:00",
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                }
            ]
        elif method in {"daily", "weekly", "monthly"}:
            columns = [
                "ts_code",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
            ]
            rows = [
                {
                    "ts_code": nested["ts_code"],
                    "trade_date": "20180710",
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                }
            ]
        else:
            self.fail(f"test fixture does not define rows for {method}")
        if columns_override is not None:
            columns = list(columns_override)
        else:
            columns = list(dict.fromkeys(columns + probe["expected_fields"]))
        item = audit._receipt_case(
            case,
            start="2026-09-22T12:00:00+08:00",
            end="2026-09-22T12:00:01+08:00",
            latency_ms=1,
            provider_code=0,
            classification="pass_nonempty",
            columns=columns,
            rows=rows,
            total=1,
            filter_checks={
                "status": "pass",
                "checks": {
                    field: {"status": "pass"}
                    for field in {check["field"] for check in case["filter_checks"]}
                },
            },
            duplicate_key_checks={
                "status": "not_applicable",
                "key_fields": [],
                "duplicate_count": 0,
                "reason": None,
            },
            pagination_checks={
                "status": "pass",
                "total": 1,
                "total_present": True,
                "limit": None,
                "offset": 0,
                "reason": None,
                "truncation_policy": case["pagination"]["truncation_policy"],
            },
        )
        return audit._build_receipt(
            [item],
            inventory_sha=audit.load_inventory()["payload_sha256"],
            manifest_sha=audit.probe_manifest_sha256(),
            started="2026-09-22T12:00:00+08:00",
            ended="2026-09-22T12:00:01+08:00",
        )

    def passing_receipt(self) -> dict:
        return self.receipt_for_probe("rt_k")

    def rewrite_same_metadata(self, path: Path, data: bytes) -> None:
        before = path.stat()
        self.assertEqual(before.st_size, len(data))
        with path.open("r+b") as handle:
            handle.seek(0)
            handle.write(data)
            handle.truncate()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        self.assertEqual(
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns),
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
        )

    def policy_document(
        self,
        receipt: dict | None,
        *,
        active: bool = True,
        snapshot_order: list[str] | None = None,
        split_periods: bool = False,
        expires_at: str = "2027-09-22T12:00:00+08:00",
    ) -> dict:
        evidence = []
        if receipt is not None:
            item = receipt["cases"][0]
            evidence.append(
                {
                    "case_identity": item["case_identity"],
                    "method": item["method"],
                    "classification": item["classification"],
                    "primary_declared": item["primary_declared"],
                    "primary_eligible": item["primary_eligible"],
                }
            )

        def capability(order, *, max_age_sec=300, empty_policy="stop", audit_evidence=None):
            return {
                "provider_order": list(order),
                "empty_policy": empty_policy,
                "max_age_sec": max_age_sec,
                "minimum_audit_classification": "pass_nonempty",
                "audit_evidence": list(audit_evidence or []),
            }

        capabilities = {
            "stock_snapshot": capability(
                snapshot_order or ["stocktoday", "pytdx", "tencent"],
                max_age_sec=60,
                audit_evidence=evidence,
            ),
            "stock_kline_daily": capability(
                ["pytdx", "tencent", "tdx_kline"], max_age_sec=86400
            ),
            "stock_kline_60m": capability(
                ["pytdx", "sina", "tdx_kline"], max_age_sec=300
            ),
            "stock_kline_15m": capability(
                ["pytdx", "sina", "tdx_kline"], max_age_sec=300
            ),
            "stock_kline_5m": capability(
                ["pytdx", "sina", "tdx_kline"], max_age_sec=300
            ),
            "sector_index": capability(["ths_industry"], max_age_sec=300),
            "review_sentiment": capability(
                ["pytdx_breadth", "eastmoney_breadth"], max_age_sec=300
            ),
        }
        if split_periods:
            capabilities["stock_kline_weekly"] = capability(
                ["pytdx", "tencent", "tdx_kline"], max_age_sec=86400
            )
            capabilities["stock_kline_monthly"] = capability(
                ["pytdx", "tencent", "tdx_kline"], max_age_sec=86400
            )

        return {
            "policy_version": "3.0",
            "pipeline_version": "3.0",
            "route_policy_version": "3.0",
            "active": active,
            "expires_at": expires_at,
            "inventory_sha256": receipt["inventory_sha256"] if receipt else None,
            "probe_manifest_sha256": receipt["probe_manifest_sha256"] if receipt else None,
            "audit_receipt_sha256": receipt["receipt_sha256"] if receipt else None,
            "capabilities": capabilities,
        }

    def policy_with_primary_evidence(
        self,
        receipt: dict,
        capability: str,
        *,
        order: list[str] | None = None,
        split_periods: bool = False,
    ) -> dict:
        policy = self.policy_document(
            receipt, snapshot_order=["pytdx"], split_periods=split_periods
        )
        evidence = copy.deepcopy(
            policy["capabilities"]["stock_snapshot"]["audit_evidence"]
        )
        for name, config in policy["capabilities"].items():
            config["audit_evidence"] = []
            if name == capability:
                config["provider_order"] = list(order or ["stocktoday", "pytdx"])
                config["audit_evidence"] = evidence
        return policy

    def test_stocktoday_cannot_be_primary_without_matching_pass_evidence(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        policy = self.policy_document(receipt)
        policy["audit_receipt_sha256"] = "0" * 64
        with self.assertRaises(module.PolicyEvidenceError):
            module.compile_policy(policy, audit_receipt=receipt, now=NOW)

    def test_primary_is_capability_specific(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        compiled = module.compile_policy(
            self.policy_document(
                receipt, snapshot_order=["stocktoday", "pytdx"]
            ),
            audit_receipt=receipt,
            now=NOW,
        )
        self.assertEqual("active", compiled.policy_status)
        self.assertEqual(
            "stocktoday", compiled.route("stock_snapshot", {}).providers[0]
        )
        self.assertNotEqual(
            "stocktoday",
            compiled.route("stock_kline", {"period": "60m"}).providers[0],
        )
        self.assertNotEqual(
            "stocktoday", compiled.route("sector_index", {"codes": ["881001"]}).providers[0]
        )

    def test_active_route_uses_real_stocktoday_provider_with_internal_source(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        policy = self.policy_with_primary_evidence(
            receipt, "stock_snapshot", order=["stocktoday"]
        )
        compiled = module.compile_policy(policy, audit_receipt=receipt, now=NOW)

        response = Mock(status_code=200)
        response.json.return_value = {
            "code": 0,
            "columns": [
                "ts_code", "close", "pre_close", "open", "high", "low",
                "vol", "amount", "updated_at",
            ],
            "data": [
                {
                    "ts_code": "600519.SH",
                    "close": 1500.0,
                    "pre_close": 1490.0,
                    "open": 1495.0,
                    "high": 1510.0,
                    "low": 1485.0,
                    "vol": 1000.0,
                    "amount": 1500000.0,
                    "updated_at": datetime.now(TZ_SHANGHAI).isoformat(
                        timespec="seconds"
                    ),
                }
            ],
            "total": 1,
        }
        transport = Mock(return_value=response)
        from ym_stock_data.providers.stocktoday import StockTodayProvider

        provider = StockTodayProvider(
            token_loader=lambda: "synthetic-policy-test-token",
            post=transport,
            budget_path=Path(self.temp_dir.name) / "stocktoday-budget.sqlite3",
        )
        result = api._query_with(
            "stock_snapshot",
            {"codes": ["600519"]},
            provider_loader=lambda name: provider,
            state_loader=lambda: self.state,
            policy_loader=lambda: compiled,
        )
        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual("stocktoday", result["_meta"]["provider_used"])
        self.assertEqual("active", result["_meta"]["policy_status"])
        transport.assert_called_once()

    def test_internal_source_marker_does_not_leak_to_fallback_provider(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        policy = self.policy_with_primary_evidence(
            receipt, "stock_snapshot", order=["stocktoday", "pytdx"]
        )
        compiled = module.compile_policy(policy, audit_receipt=receipt, now=NOW)

        transport = Mock(side_effect=TimeoutError("synthetic timeout"))
        from ym_stock_data.providers.stocktoday import StockTodayProvider

        stocktoday = StockTodayProvider(
            token_loader=lambda: "synthetic-policy-test-token",
            post=transport,
            budget_path=Path(self.temp_dir.name) / "fallback-budget.sqlite3",
        )
        fallback = FakeProvider(
            "pytdx",
            [fresh_snapshot_outcome("pytdx")],
        )
        result = api._query_with(
            "stock_snapshot",
            {"codes": ["600519"]},
            provider_loader=lambda name: stocktoday if name == "stocktoday" else fallback,
            state_loader=lambda: self.state,
            policy_loader=lambda: compiled,
        )
        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("pytdx", result["_meta"]["provider_used"])
        self.assertNotIn("source", fallback.calls[0][1])

    def test_active_review_query_keeps_independent_natural_language_route(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        policy = self.policy_with_primary_evidence(receipt, "stock_snapshot")
        compiled = module.compile_policy(policy, audit_receipt=receipt, now=NOW)

        query_route = compiled.route("review_sentiment", {"query": "今日涨停"})
        self.assertEqual(
            (
                "iwencai_openapi",
                "pywencai",
                "tdx_screener",
                "wind_screener",
            ),
            query_route.providers,
        )
        default_route = compiled.route("review_sentiment", {})
        self.assertEqual("pytdx_breadth", default_route.providers[0])

    def test_stocktoday_anywhere_in_automatic_order_requires_evidence(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        policy = self.policy_document(
            receipt, snapshot_order=["pytdx", "stocktoday", "tencent"]
        )
        policy["capabilities"]["stock_snapshot"]["audit_evidence"] = []
        with self.assertRaises(module.PolicyEvidenceError):
            module.compile_policy(policy, audit_receipt=receipt, now=NOW)

    def test_provider_order_allowlist_and_base_freshness_bound_are_code_side(self):
        module = self.policy_module()
        receipt = self.passing_receipt()

        incompatible = self.policy_document(receipt)
        incompatible["capabilities"]["stock_snapshot"]["provider_order"] = [
            "ths_industry"
        ]
        with self.assertRaises(module.PolicySchemaError):
            module.compile_policy(incompatible, audit_receipt=receipt, now=NOW)

        unregistered = self.policy_document(receipt)
        unregistered["capabilities"]["stock_snapshot"]["provider_order"] = [
            "pro_bar"
        ]
        with self.assertRaises(module.PolicySchemaError):
            module.compile_policy(unregistered, audit_receipt=receipt, now=NOW)

        for capability, too_new in (
            ("stock_snapshot", 61),
            ("stock_kline_daily", 86401),
            ("stock_kline_60m", 301),
            ("sector_index", 301),
            ("review_sentiment", 301),
        ):
            with self.subTest(capability=capability):
                policy = self.policy_document(receipt)
                policy["capabilities"][capability]["max_age_sec"] = too_new
                with self.assertRaises(module.PolicySchemaError):
                    module.compile_policy(policy, audit_receipt=receipt, now=NOW)

        self.assertNotIn("pro_bar", module._CAPABILITY_METHODS["stock_kline_daily"])
        self.assertNotIn("sina", module._CAPABILITY_PROVIDER_ALLOWLIST["stock_snapshot"])
        packaged = json.loads(Path(module.POLICY_PATH).read_text(encoding="utf-8"))
        self.assertNotIn(
            "sina", packaged["capabilities"]["stock_snapshot"]["provider_order"]
        )

    def test_period_capabilities_are_split_and_only_matching_endpoint_promotes(self):
        module = self.policy_module()
        for period, method, capability in (
            ("daily", "daily", "stock_kline_daily"),
            ("weekly", "weekly", "stock_kline_weekly"),
            ("monthly", "monthly", "stock_kline_monthly"),
        ):
            with self.subTest(period=period):
                receipt = self.receipt_for_probe(method)
                policy = self.policy_with_primary_evidence(
                    receipt,
                    capability,
                    split_periods=True,
                )
                compiled = module.compile_policy(
                    policy, audit_receipt=receipt, now=NOW
                )
                self.assertEqual(
                    "stocktoday",
                    compiled.route("stock_kline", {"period": period}).providers[0],
                )
                for other in ("daily", "weekly", "monthly"):
                    if other != period:
                        self.assertNotEqual(
                            "stocktoday",
                            compiled.route(
                                "stock_kline", {"period": other}
                            ).providers[0],
                        )

    def test_daily_evidence_cannot_promote_weekly_or_monthly(self):
        module = self.policy_module()
        receipt = self.receipt_for_probe("daily")
        policy = self.policy_with_primary_evidence(
            receipt,
            "stock_kline_weekly",
            split_periods=True,
        )
        with self.assertRaises(module.PolicyEvidenceError):
            module.compile_policy(policy, audit_receipt=receipt, now=NOW)

    def test_runtime_columns_gate_uses_receipt_columns_not_policy_fields(self):
        module = self.policy_module()
        cases = (
            (
                "stock_snapshot",
                "rt_k",
                ["ts_code"],
            ),
            (
                "stock_kline_daily",
                "daily",
                ["ts_code", "trade_date", "open", "high", "low"],
            ),
        )
        for capability, method, columns in cases:
            with self.subTest(capability=capability):
                receipt = self.receipt_for_probe(method, columns_override=columns)
                policy = self.policy_with_primary_evidence(receipt, capability)
                with self.assertRaises(module.PolicyEvidenceError):
                    module.compile_policy(
                        policy, audit_receipt=receipt, now=NOW
                    )

        self.assertFalse(
            module._runtime_columns_match(
                "stock_kline_60m",
                ["ts_code", "open", "high", "low", "close"],
            )
        )

    def test_probe_expected_fields_bind_runtime_columns_for_weekly_and_monthly(self):
        module = self.policy_module()
        for capability, method in (
            ("stock_kline_weekly", "weekly"),
            ("stock_kline_monthly", "monthly"),
        ):
            with self.subTest(capability=capability):
                receipt = self.receipt_for_probe(
                    method,
                    columns_override=[
                        "ts_code",
                        "trade_date",
                        "open",
                        "high",
                        "low",
                        "close",
                    ],
                )
                policy = self.policy_with_primary_evidence(
                    receipt, capability, split_periods=True
                )
                with self.assertRaises(module.PolicyEvidenceError):
                    module.compile_policy(policy, audit_receipt=receipt, now=NOW)

    def test_declared_probe_filter_without_actual_pass_check_cannot_activate(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        receipt["cases"][0]["filter_checks"] = {"status": "pass", "checks": {}}
        from ym_stock_data import stocktoday_audit as audit

        unsigned = copy.deepcopy(receipt)
        unsigned.pop("receipt_sha256")
        receipt["receipt_sha256"] = audit._sha256(unsigned)
        policy = self.policy_with_primary_evidence(receipt, "stock_snapshot")
        with self.assertRaises(module.PolicyEvidenceError):
            module.compile_policy(policy, audit_receipt=receipt, now=NOW)

    def test_evidence_binds_case_identity_classification_and_primary_flags(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        for field, value in (
            ("case_identity", "0" * 64),
            ("classification", "reachable_empty"),
            ("primary_declared", False),
            ("primary_eligible", False),
        ):
            with self.subTest(field=field):
                policy = self.policy_document(receipt)
                policy["capabilities"]["stock_snapshot"]["audit_evidence"][0][field] = value
                with self.assertRaises(module.PolicyEvidenceError):
                    module.compile_policy(policy, audit_receipt=receipt, now=NOW)

        relabeled = self.receipt_for_probe(
            "rt_min", nested_overrides={"freq": "60MIN"}
        )
        policy = self.policy_document(relabeled)
        policy["capabilities"]["stock_snapshot"]["audit_evidence"] = []
        policy["capabilities"]["stock_kline_60m"]["provider_order"] = [
            "stocktoday",
            "pytdx",
        ]
        policy["capabilities"]["stock_kline_60m"]["audit_evidence"] = copy.deepcopy(
            self.policy_document(relabeled)["capabilities"]["stock_snapshot"][
                "audit_evidence"
            ]
        )
        with self.assertRaises(module.PolicyEvidenceError):
            module.compile_policy(policy, audit_receipt=relabeled, now=NOW)

    def test_minute_evidence_requires_exact_supported_frequency(self):
        module = self.policy_module()
        receipt = self.receipt_for_probe("rt_min")
        evidence = self.policy_document(receipt)["capabilities"]["stock_snapshot"][
            "audit_evidence"
        ]
        for capability in ("stock_kline_60m", "stock_kline_15m", "stock_kline_5m"):
            with self.subTest(capability=capability):
                policy = self.policy_document(receipt)
                policy["capabilities"]["stock_snapshot"]["audit_evidence"] = []
                policy["capabilities"][capability]["provider_order"] = [
                    "stocktoday",
                    "pytdx",
                ]
                policy["capabilities"][capability]["audit_evidence"] = copy.deepcopy(
                    evidence
                )
                with self.assertRaises(module.PolicyEvidenceError):
                    module.compile_policy(policy, audit_receipt=receipt, now=NOW)

    def test_category_cannot_launder_daily_method_into_snapshot(self):
        module = self.policy_module()
        receipt = self.receipt_for_probe("daily", category="stock_snapshot")
        with self.assertRaises(module.PolicyEvidenceError):
            module.compile_policy(
                self.policy_document(receipt), audit_receipt=receipt, now=NOW
            )

    def test_unsupported_capabilities_do_not_default_to_true(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        for capability in ("stocktoday_data", "market_limit_state"):
            with self.subTest(capability=capability):
                policy = self.policy_document(receipt)
                evidence = policy["capabilities"]["stock_snapshot"]["audit_evidence"]
                policy["capabilities"]["stock_snapshot"]["audit_evidence"] = []
                policy["capabilities"][capability] = copy.deepcopy(
                    policy["capabilities"]["stock_kline_daily"]
                )
                policy["capabilities"][capability]["provider_order"] = [
                    "stocktoday",
                    "pytdx",
                ]
                policy["capabilities"][capability]["audit_evidence"] = evidence
                with self.assertRaises(module.PolicyError):
                    module.compile_policy(policy, audit_receipt=receipt, now=NOW)

    def test_fallback_success_cannot_hide_failed_primary(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        compiled = module.compile_policy(
            self.policy_document(
                receipt, snapshot_order=["stocktoday", "pytdx"]
            ),
            audit_receipt=receipt,
            now=NOW,
        )
        providers = {
            "stocktoday": FakeProvider(
                "stocktoday", [outcome("stocktoday", "timeout", error_code="TIMEOUT")]
            ),
            "pytdx": FakeProvider(
                "pytdx",
                [
                    outcome(
                        "pytdx",
                        "empty",
                        data={"600519": {"error": "NO_DATA"}},
                    )
                ],
            ),
        }
        with patch.object(
            api,
            "_provider_for",
            side_effect=lambda name: providers[name],
        ):
            result = api._query_with(
                "stock_snapshot",
                {"codes": ["600519"]},
                state_loader=lambda: self.state,
                policy_loader=lambda: compiled,
            )
        self.assertEqual("error", result["_meta"]["status"])
        self.assertEqual(["stocktoday", "pytdx"], result["_meta"]["source_chain"])
        self.assertEqual("active", result["_meta"]["policy_status"])
        self.assertEqual("fallback", result["_meta"]["source_tier"])
        self.assertEqual(
            ["timeout", "provider_error"],
            [item["status"] for item in result["_meta"]["attempt_classification_trace"]],
        )

    def test_inactive_policy_preserves_v2_routes_and_adds_metadata(self):
        module = self.policy_module()
        policy = self.policy_document(None, active=False)
        compiled = module.compile_policy(policy, audit_receipt=None, now=NOW)
        self.assertEqual("inactive", compiled.policy_status)
        self.assertEqual(
            "stocktoday", compiled.route("stock_snapshot", {}).providers[0]
        )
        self.assertEqual(
            "stocktoday",
            compiled.route("stock_snapshot", {"source": "stocktoday"}).providers[0],
        )

        provider = FakeProvider(
            "stocktoday",
            [fresh_snapshot_outcome("stocktoday")],
        )
        with patch.object(api, "_provider_for", return_value=provider):
            result = api._query_with(
                "stock_snapshot",
                {"codes": ["600519"]},
                state_loader=lambda: self.state,
                policy_loader=lambda: compiled,
            )
        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual("stocktoday", result["_meta"]["provider_used"])
        self.assertEqual("inactive", result["_meta"]["policy_status"])
        self.assertIsNone(result["_meta"]["policy_evidence_sha256"])
        self.assertEqual("3.0", result["_meta"]["pipeline_version"])
        self.assertEqual("3.0", result["_meta"]["route_policy_version"])
        self.assertEqual("primary", result["_meta"]["source_tier"])

    def test_missing_malformed_expired_or_tampered_evidence_is_inactive(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        cases = [
            (self.policy_document(None, active=True), None),
            ({"active": True}, receipt),
            (self.policy_document(receipt, expires_at="2026-09-21T12:00:00+08:00"), receipt),
        ]
        tampered = copy.deepcopy(receipt)
        tampered["receipt_sha256"] = "0" * 64
        cases.append((self.policy_document(receipt), tampered))
        for policy, candidate_receipt in cases:
            with self.subTest(policy=policy.get("expires_at")):
                compiled = module.load_compiled_policy(
                    policy=policy,
                    audit_receipt=candidate_receipt,
                    now=NOW,
                )
                self.assertEqual("inactive", compiled.policy_status)
                self.assertIsNone(compiled.policy_evidence_sha256)

    def test_missing_or_malformed_policy_path_is_inactive(self):
        module = self.policy_module()
        missing = Path(self.temp_dir.name) / "missing-policy.json"
        self.assertEqual(
            "inactive",
            module.load_compiled_policy(path=missing, now=NOW).policy_status,
        )
        malformed = Path(self.temp_dir.name) / "malformed-policy.json"
        malformed.write_text("{", encoding="utf-8")
        self.assertEqual(
            "inactive",
            module.load_compiled_policy(path=malformed, now=NOW).policy_status,
        )
        malformed.write_text(json.dumps({"active": True}), encoding="utf-8")
        self.assertEqual(
            "inactive",
            module.load_compiled_policy(path=malformed, now=NOW).policy_status,
        )

    def test_active_policy_can_use_canonical_reviewed_receipt(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        policy = self.policy_document(receipt)
        canonical = Path(self.temp_dir.name) / "stocktoday-audit-receipt.v3.json"
        canonical.write_text(
            json.dumps(receipt, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        with patch.object(module, "AUDIT_RECEIPT_PATH", canonical):
            compiled = module.load_compiled_policy(policy=policy, now=NOW)
        self.assertEqual("active", compiled.policy_status)
        self.assertIsNotNone(compiled.policy_evidence_sha256)

    def test_active_policy_without_canonical_reviewed_receipt_is_inactive(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        policy = self.policy_document(receipt)
        missing = Path(self.temp_dir.name) / "not-reviewed.json"
        with patch.object(module, "AUDIT_RECEIPT_PATH", missing):
            compiled = module.load_compiled_policy(policy=policy, now=NOW)
        self.assertEqual("inactive", compiled.policy_status)
        self.assertIsNone(compiled.policy_evidence_sha256)

        malformed = Path(self.temp_dir.name) / "malformed-reviewed.json"
        malformed.write_text("{", encoding="utf-8")
        with patch.object(module, "AUDIT_RECEIPT_PATH", malformed):
            compiled = module.load_compiled_policy(policy=policy, now=NOW)
        self.assertEqual("inactive", compiled.policy_status)

        tampered = copy.deepcopy(receipt)
        tampered["receipt_sha256"] = "0" * 64
        malformed.write_text(
            json.dumps(tampered, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        with patch.object(module, "AUDIT_RECEIPT_PATH", malformed):
            compiled = module.load_compiled_policy(policy=policy, now=NOW)
        self.assertEqual("inactive", compiled.policy_status)

    def test_api_default_loader_uses_canonical_policy_and_receipt(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        policy = self.policy_document(receipt)
        policy_path = Path(self.temp_dir.name) / "provider-policy.v3.json"
        receipt_path = Path(self.temp_dir.name) / "stocktoday-audit-receipt.v3.json"
        policy_path.write_text(
            json.dumps(policy, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        provider = FakeProvider(
            "stocktoday",
            [fresh_snapshot_outcome("stocktoday")],
        )
        with patch.object(module, "POLICY_PATH", policy_path), patch.object(
            module, "AUDIT_RECEIPT_PATH", receipt_path
        ), patch.object(
            module,
            "PACKAGED_POLICY_SHA256",
            module._policy_logical_sha256(policy),
        ), patch.object(api, "_provider_for", return_value=provider):
            result = api._query_with(
                "stock_snapshot",
                {"codes": ["600519"]},
                state_loader=lambda: self.state,
            )
        self.assertEqual("active", result["_meta"]["policy_status"])
        self.assertEqual("stocktoday", result["_meta"]["provider_used"])
        self.assertEqual("primary", result["_meta"]["source_tier"])

    def test_canonical_policy_anchor_covers_semantic_document_and_rejects_tampering(self):
        module = self.policy_module()
        original = module.load_policy()
        self.assertEqual(
            module.PACKAGED_POLICY_SHA256,
            module._policy_logical_sha256(original),
        )

        mutations = []
        changed_order = copy.deepcopy(original)
        changed_order["capabilities"]["stock_snapshot"]["provider_order"] = list(
            reversed(changed_order["capabilities"]["stock_snapshot"]["provider_order"])
        )
        mutations.append(changed_order)

        changed_empty = copy.deepcopy(original)
        changed_empty["capabilities"]["stock_snapshot"]["empty_policy"] = (
            "continue_until_exhausted"
        )
        mutations.append(changed_empty)

        changed_age = copy.deepcopy(original)
        changed_age["capabilities"]["stock_snapshot"]["max_age_sec"] = 59
        mutations.append(changed_age)

        changed_minimum = copy.deepcopy(original)
        changed_minimum["capabilities"]["stock_snapshot"][
            "minimum_audit_classification"
        ] = "reachable_empty"
        mutations.append(changed_minimum)

        changed_evidence = copy.deepcopy(original)
        changed_evidence["capabilities"]["stock_snapshot"]["audit_evidence"] = [
            {
                "case_identity": "0" * 64,
                "method": "rt_k",
                "classification": "pass_nonempty",
                "primary_declared": True,
                "primary_eligible": True,
            }
        ]
        mutations.append(changed_evidence)

        changed_expiry = copy.deepcopy(original)
        changed_expiry["expires_at"] = "2027-09-23T12:00:00+08:00"
        mutations.append(changed_expiry)

        changed_receipt_hash = copy.deepcopy(original)
        changed_receipt_hash["audit_receipt_sha256"] = "0" * 64
        mutations.append(changed_receipt_hash)

        changed_active = copy.deepcopy(original)
        changed_active["active"] = True
        mutations.append(changed_active)

        for tampered in mutations:
            with self.subTest(tampered=tampered):
                self.assertNotEqual(
                    module.PACKAGED_POLICY_SHA256,
                    module._policy_logical_sha256(tampered),
                )

        for tampered in (changed_order, changed_age):
            policy_path = Path(self.temp_dir.name) / "tampered-policy.json"
            policy_path.write_text(
                json.dumps(tampered, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            with patch.object(module, "POLICY_PATH", policy_path):
                with self.assertRaises(module.PolicySchemaError):
                    module.load_policy()
                self.assertEqual(
                    "inactive", module.load_compiled_policy(now=NOW).policy_status
                )

    def test_default_policy_cache_reuses_compilation_and_reloads_after_atomic_replace(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        first_policy = self.policy_document(receipt)
        second_policy = copy.deepcopy(first_policy)
        second_policy["capabilities"]["stock_snapshot"]["provider_order"] = [
            "pytdx"
        ]
        policy_path = Path(self.temp_dir.name) / "provider-policy.v3.json"
        receipt_path = Path(self.temp_dir.name) / "stocktoday-audit-receipt.v3.json"
        policy_path.write_text(
            json.dumps(first_policy, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        with patch.object(module, "POLICY_PATH", policy_path), patch.object(
            module, "AUDIT_RECEIPT_PATH", receipt_path
        ), patch.object(
            module,
            "PACKAGED_POLICY_SHA256",
            module._policy_logical_sha256(first_policy),
        ), patch.object(
            module, "compile_policy", wraps=module.compile_policy
        ) as compile_mock:
            provider = FakeProvider(
                "stocktoday",
                [
                    outcome("stocktoday", "success", data={"600519": {"price": 1}}),
                    outcome("stocktoday", "success", data={"600519": {"price": 1}}),
                ],
            )
            with patch.object(api, "_provider_for", return_value=provider):
                first_result = api._query_with(
                    "stock_snapshot",
                    {"codes": ["600519"]},
                    state_loader=lambda: self.state,
                )
                second_result = api._query_with(
                    "stock_snapshot",
                    {"codes": ["600519"]},
                    state_loader=lambda: self.state,
                )
            self.assertEqual("active", first_result["_meta"]["policy_status"])
            self.assertEqual("active", second_result["_meta"]["policy_status"])
            self.assertEqual(1, compile_mock.call_count)

            replacement = Path(self.temp_dir.name) / "replacement-policy.json"
            replacement.write_text(
                json.dumps(second_policy, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            import os

            os.replace(replacement, policy_path)
            module.PACKAGED_POLICY_SHA256 = module._policy_logical_sha256(
                second_policy
            )
            reloaded = module.load_compiled_policy(now=NOW)
            self.assertEqual("active", reloaded.policy_status)
            self.assertEqual(
                ("pytdx",), reloaded.route("stock_snapshot", {}).providers
            )
            self.assertEqual(2, compile_mock.call_count)

    def test_snapshot_key_binds_same_metadata_in_place_content_changes(self):
        module = self.policy_module()
        policy_path = Path(self.temp_dir.name) / "policy.json"
        receipt_path = Path(self.temp_dir.name) / "receipt.json"
        policy_path.write_bytes(b"0123456789")
        receipt_path.write_bytes(b"abcdefghij")
        before = module._snapshot_key(policy_path, receipt_path)
        self.rewrite_same_metadata(policy_path, b"9876543210")
        after = module._snapshot_key(policy_path, receipt_path)
        self.assertNotEqual(before, after)

    def test_default_cache_revalidates_policy_receipt_inventory_and_probe_content(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        policy = self.policy_document(receipt)
        policy_path = Path(self.temp_dir.name) / "provider-policy.v3.json"
        receipt_path = Path(self.temp_dir.name) / "stocktoday-audit-receipt.v3.json"
        policy_bytes = json.dumps(
            policy, ensure_ascii=False, sort_keys=True
        ).encode("utf-8") + b" "
        receipt_bytes = json.dumps(
            receipt, ensure_ascii=False, sort_keys=True
        ).encode("utf-8") + b" "
        policy_path.write_bytes(policy_bytes)
        receipt_path.write_bytes(receipt_bytes)

        from ym_stock_data import stocktoday_audit as audit
        from ym_stock_data.providers.stocktoday_inventory import INVENTORY_PATH
        from ym_stock_data.v3 import PROBE_MANIFEST_PATH

        inventory_path = Path(self.temp_dir.name) / "inventory.json"
        probe_path = Path(self.temp_dir.name) / "probes.json"
        inventory_bytes = INVENTORY_PATH.read_bytes()
        probe_bytes = PROBE_MANIFEST_PATH.read_bytes()
        inventory_path.write_bytes(inventory_bytes)
        probe_path.write_bytes(probe_bytes)

        with patch.object(module, "POLICY_PATH", policy_path), patch.object(
            module, "AUDIT_RECEIPT_PATH", receipt_path
        ), patch.object(
            module,
            "PACKAGED_POLICY_SHA256",
            module._policy_logical_sha256(policy),
        ), patch.object(
            module,
            "_evidence_paths",
            return_value=(inventory_path, probe_path),
        ):
            self.assertEqual("active", module.load_compiled_policy(now=NOW).policy_status)

            malformed_policy = b"{" + b" " * (len(policy_bytes) - 1)
            self.rewrite_same_metadata(policy_path, malformed_policy)
            self.assertEqual(
                "inactive", module.load_compiled_policy(now=NOW).policy_status
            )
            self.rewrite_same_metadata(policy_path, policy_bytes)
            self.assertEqual("active", module.load_compiled_policy(now=NOW).policy_status)

            changed_receipt = copy.deepcopy(receipt)
            changed_receipt["cases"][0]["columns"] = [
                "ts_code",
                "close",
                "trade_time",
            ]
            unsigned = copy.deepcopy(changed_receipt)
            unsigned.pop("receipt_sha256")
            changed_receipt["receipt_sha256"] = audit._sha256(unsigned)
            changed_receipt_bytes = json.dumps(
                changed_receipt, ensure_ascii=False, sort_keys=True
            ).encode("utf-8") + b" "
            self.assertEqual(len(receipt_bytes), len(changed_receipt_bytes))
            self.rewrite_same_metadata(receipt_path, changed_receipt_bytes)
            self.assertEqual(
                "inactive", module.load_compiled_policy(now=NOW).policy_status
            )
            self.rewrite_same_metadata(receipt_path, receipt_bytes)
            self.assertEqual("active", module.load_compiled_policy(now=NOW).policy_status)

            revoked = copy.deepcopy(policy)
            revoked["active"] = False
            revoked_bytes = json.dumps(
                revoked, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
            self.assertEqual(len(policy_bytes), len(revoked_bytes))
            self.rewrite_same_metadata(policy_path, revoked_bytes)
            self.assertEqual(
                "inactive", module.load_compiled_policy(now=NOW).policy_status
            )

            self.rewrite_same_metadata(policy_path, policy_bytes)
            self.assertEqual("active", module.load_compiled_policy(now=NOW).policy_status)

            changed_inventory = bytearray(inventory_bytes)
            marker = b'"desc": "'
            index = changed_inventory.find(marker)
            self.assertGreaterEqual(index, 0)
            value_index = index + len(marker)
            changed_inventory[value_index] = (
                ord("x") if changed_inventory[value_index] != ord("x") else ord("y")
            )
            self.rewrite_same_metadata(inventory_path, bytes(changed_inventory))
            self.assertEqual(
                "inactive", module.load_compiled_policy(now=NOW).policy_status
            )
            self.rewrite_same_metadata(inventory_path, inventory_bytes)
            self.assertEqual("active", module.load_compiled_policy(now=NOW).policy_status)

            changed_probe = bytearray(probe_bytes)
            marker = b'"source": "vendor_example"'
            index = changed_probe.find(marker)
            self.assertGreaterEqual(index, 0)
            replacement = b'"source": "vendor_examplf"'
            self.assertEqual(len(marker), len(replacement))
            changed_probe[index : index + len(marker)] = replacement
            self.rewrite_same_metadata(probe_path, bytes(changed_probe))
            self.assertEqual(
                "inactive", module.load_compiled_policy(now=NOW).policy_status
            )

    def test_concurrent_default_loads_never_return_a_mixed_policy_and_receipt_snapshot(self):
        module = self.policy_module()
        from ym_stock_data import stocktoday_audit as audit

        receipt_a = self.passing_receipt()
        policy_a = self.policy_document(receipt_a)
        receipt_b = copy.deepcopy(receipt_a)
        receipt_b["cases"][0]["columns"] = [
            "ts_code",
            "close",
            "trade_time",
        ]
        unsigned = copy.deepcopy(receipt_b)
        unsigned.pop("receipt_sha256")
        receipt_b["receipt_sha256"] = audit._sha256(unsigned)
        policy_b = self.policy_document(receipt_b)
        policy_path = Path(self.temp_dir.name) / "provider-policy.v3.json"
        receipt_path = Path(self.temp_dir.name) / "stocktoday-audit-receipt.v3.json"
        policy_path.write_text(
            json.dumps(policy_a, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        receipt_path.write_text(
            json.dumps(receipt_a, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        with patch.object(module, "POLICY_PATH", policy_path), patch.object(
            module, "AUDIT_RECEIPT_PATH", receipt_path
        ), patch.object(
            module,
            "PACKAGED_POLICY_SHA256",
            module._policy_logical_sha256(policy_a),
        ):
            real_read_snapshot = module._read_file_snapshot
            replaced = False

            def rotate_before_receipt_snapshot(path, *, kind):
                nonlocal replaced
                snapshot = real_read_snapshot(path, kind=kind)
                if kind == "receipt" and not replaced:
                    replacement_policy = Path(self.temp_dir.name) / "policy-b.json"
                    replacement_receipt = Path(self.temp_dir.name) / "receipt-b.json"
                    replacement_policy.write_text(
                        json.dumps(policy_b, ensure_ascii=False, sort_keys=True),
                        encoding="utf-8",
                    )
                    replacement_receipt.write_text(
                        json.dumps(receipt_b, ensure_ascii=False, sort_keys=True),
                        encoding="utf-8",
                    )
                    import os

                    os.replace(replacement_policy, policy_path)
                    os.replace(replacement_receipt, receipt_path)
                    replaced = True
                return snapshot

            results = []
            start = threading.Barrier(4)

            def worker():
                start.wait()
                results.append(module.load_compiled_policy(now=NOW))

            with patch.object(
                module,
                "_read_file_snapshot",
                side_effect=rotate_before_receipt_snapshot,
            ):
                threads = [threading.Thread(target=worker) for _ in range(4)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertEqual(4, len(results))
            self.assertTrue(all(item.policy_status == "inactive" for item in results))
            module.PACKAGED_POLICY_SHA256 = module._policy_logical_sha256(policy_b)
            compiled = module.load_compiled_policy(now=NOW)
            self.assertEqual("active", compiled.policy_status)

    def test_explicit_policy_injection_bypasses_default_compiled_cache(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        policy = self.policy_document(receipt)
        with patch.object(
            module, "compile_policy", wraps=module.compile_policy
        ) as compile_mock:
            first = module.load_compiled_policy(
                policy=policy, audit_receipt=receipt, now=NOW
            )
            second = module.load_compiled_policy(
                policy=policy, audit_receipt=receipt, now=NOW
            )
        self.assertEqual("active", first.policy_status)
        self.assertEqual("active", second.policy_status)
        self.assertEqual(2, compile_mock.call_count)

    def test_empty_policy_and_max_age_are_capability_specific(self):
        module = self.policy_module()
        receipt = self.passing_receipt()
        policy = self.policy_document(receipt)
        policy["capabilities"]["stock_snapshot"]["empty_policy"] = "stop"
        policy["capabilities"]["stock_kline_60m"]["empty_policy"] = "continue_until_exhausted"
        compiled = module.compile_policy(policy, audit_receipt=receipt, now=NOW)
        snapshot = compiled.route("stock_snapshot", {})
        minute = compiled.route("stock_kline", {"period": "60m"})
        self.assertEqual("stop", snapshot.empty_policy)
        self.assertEqual(60, snapshot.max_age_sec)
        self.assertEqual("continue_until_exhausted", minute.empty_policy)
        self.assertEqual(300, minute.max_age_sec)

    def test_packaged_policy_is_canonical_inactive_and_does_not_auto_promote(self):
        module = self.policy_module()
        expected_path = Path(module.__file__).resolve().parent / "v3" / "provider-policy.v3.json"
        self.assertEqual(expected_path, module.POLICY_PATH.resolve())
        policy = module.load_policy()
        self.assertFalse(policy["active"])
        compiled = module.load_compiled_policy(now=NOW)
        self.assertEqual("inactive", compiled.policy_status)
        self.assertEqual(
            "stocktoday", compiled.route("stock_snapshot", {}).providers[0]
        )


if __name__ == "__main__":
    unittest.main()
