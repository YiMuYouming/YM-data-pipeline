from __future__ import annotations

import copy
import importlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_TOKEN = "synthetic-stocktoday-audit-token-001"
FIXED_EPOCH = 1_790_027_200.0


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: object | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {
            "code": 0,
            "data": {
                "fields": ["ts_code", "trade_date", "close"],
                "items": [["000001.SZ", "20250930", 10.0]],
            },
            "total": 1,
        }

    def json(self):
        return copy.deepcopy(self._payload)


class FakeTransport:
    def __init__(self, response=None):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if callable(self.response):
            return self.response(url, kwargs)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response or FakeResponse()


class FakeClock:
    def __init__(self, epoch: float = FIXED_EPOCH):
        self.epoch = epoch
        self.mono = 100.0
        self.sleeps: list[float] = []

    def time(self):
        return self.epoch

    def monotonic(self):
        return self.mono

    def sleep(self, seconds: float):
        self.sleeps.append(seconds)
        self.epoch += seconds
        self.mono += seconds


class SequenceBudget:
    def __init__(self, decisions, wait_seconds: float = 5.0):
        self.decisions = list(decisions)
        self.wait_seconds_value = wait_seconds
        self.acquires = 0

    def acquire(self, *, now=None):
        self.acquires += 1
        if self.decisions:
            return self.decisions.pop(0)
        return True

    def wait_seconds(self, *, now=None):
        return self.wait_seconds_value


def _audit_module(testcase: unittest.TestCase):
    try:
        return importlib.import_module("ym_stock_data.stocktoday_audit")
    except ModuleNotFoundError as exc:
        if exc.name == "ym_stock_data.stocktoday_audit":
            testcase.fail("stocktoday_audit module is not implemented yet")
        raise


def _probe_generator(testcase: unittest.TestCase):
    script = ROOT / "scripts" / "build_stocktoday_probes.py"
    spec = importlib.util.spec_from_file_location("build_stocktoday_probes", script)
    if spec is None or spec.loader is None:
        testcase.fail("probe generator cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _case(method="daily", *, probe_id="test-1", nested=None, fields="", **extra):
    return {
        "method": method,
        "probe_id": probe_id,
            "category": "测试",
            "params": {
                "api_name": method,
                "params": nested if nested is not None else {
                    "ts_code": "000001.SZ",
                    "trade_date": "20250930",
                },
            "fields": fields,
        },
        **extra,
    }


def _success_payload(*, rows=None, total=None, fields=None):
    rows = rows if rows is not None else [["000001.SZ", "20250930", 10.0]]
    fields = fields if fields is not None else ["ts_code", "trade_date", "close"]
    return {
        "code": 0,
        "data": {"fields": fields, "items": rows},
        "total": len(rows) if total is None else total,
    }


def _manifest_response(url, kwargs):
    """Return a credential-free, schema-complete response for every probe."""

    request = kwargs["json"]
    name = request["api_name"]
    if name == "token_info":
        return FakeResponse(
            payload={
                "code": 0,
                "data": {
                    "fields": ["token_type", "access_level"],
                    "items": [["v2", "read"]],
                },
                "total": 1,
            }
        )
    nested = request.get("params", {})
    requested = request.get("fields") or ""
    fields = [field.strip() for field in requested.split(",") if field.strip()]
    if not fields:
        fields = ["ts_code"] if nested.get("ts_code") else ["marker"]

    def value(field):
        if field == "ts_code" and nested.get("ts_code"):
            return str(nested["ts_code"]).split(",")[0]
        if field in nested:
            return nested[field]
        if field in {"trade_date", "date", "ann_date", "pub_date", "imp_date"}:
            return nested.get("trade_date") or nested.get(field) or nested.get("end_date") or "20250930"
        if field in {"start_date", "end_date"}:
            return nested.get(field) or "20250930"
        if field == "month":
            return nested.get("m") or nested.get("end_m") or "202509"
        return 1

    return FakeResponse(
        payload={
            "code": 0,
            "data": {"fields": fields, "items": [[value(field) for field in fields]]},
            "total": 1,
        }
    )


class StockTodayAuditTests(unittest.TestCase):
    def test_duplicate_keys_are_endpoint_aware_and_conservative(self):
        generator = _probe_generator(self)
        cases = [
            (
                "daily",
                {"ts_code": "600519.SH", "start_date": "20250101", "end_date": "20250930"},
                ["ts_code", "trade_date", "close"],
                ["ts_code", "trade_date"],
            ),
            (
                "etf_mins",
                {"ts_code": "510330.SH", "start_date": "2025-09-30 09:30:00", "end_date": "2025-09-30 10:00:00"},
                ["ts_code", "trade_time", "close"],
                ["ts_code", "trade_time"],
            ),
            (
                "rt_tick",
                {"ts_code": "600519.SH"},
                ["ts_code", "trade_time", "price"],
                ["ts_code", "trade_time"],
            ),
            (
                "income_vip",
                {"ts_code": "600519.SH", "start_date": "20240101", "end_date": "20250930"},
                ["ts_code", "ann_date", "end_date", "period", "revenue"],
                [],
            ),
            (
                "news",
                {"start_date": "20250901", "end_date": "20250930"},
                ["pub_time", "title", "content"],
                [],
            ),
            (
                "kpl_concept",
                {"trade_date": "20250930"},
                ["ts_code", "name", "trade_date"],
                [],
            ),
            (
                "dc_concept_cons",
                {"trade_date": "20250930", "ts_code": "600519.SH"},
                ["ts_code", "trade_date"],
                [],
            ),
        ]
        for name, nested, fields, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(expected, generator.duplicate_keys(nested, fields, name=name))

    def test_fixed_probe_unknown_fields_are_explicitly_conservative(self):
        generator = _probe_generator(self)
        conservative_methods = {
            "dc_concept",
            "dc_concept_cons",
            "fund_company",
            "hm_list",
            "kpl_concept",
            "realtime_list",
            "realtime_quote",
            "realtime_tick",
            "rt_etf_tick",
            "rt_hk_tick",
            "rt_idx_tick",
            "rt_sw_k",
            "rt_sw_tick",
            "rt_tick",
            "st",
            "ths_news",
        }
        for method in conservative_methods:
            with self.subTest(method=method):
                self.assertEqual("", generator.FIXED_FIELDS[method])

        probes = _audit_module(self).load_probes()["methods"]
        for method in conservative_methods:
            with self.subTest(manifest_method=method):
                probe = probes[method][0]
                self.assertEqual([], probe["duplicate_key_fields"])
                self.assertEqual([], probe["expected_fields"])
                self.assertEqual([], probe["filter_checks"])

    def test_explicit_empty_filter_declaration_suppresses_runtime_inference(self):
        audit = _audit_module(self)
        cases = [
            _case(
                method="dc_concept",
                probe_id="dc-concept-conservative",
                nested={"trade_date": "20250930"},
                filter_checks=[],
                expected_fields=[],
                duplicate_key_fields=[],
            ),
            _case(
                method="dc_concept_cons",
                probe_id="dc-concept-cons-conservative",
                nested={"ts_code": "600519.SH", "trade_date": "20250930"},
                filter_checks=[],
                expected_fields=[],
                duplicate_key_fields=[],
            ),
            _case(
                method="kpl_concept",
                probe_id="kpl-concept-conservative",
                nested={"trade_date": "20250930"},
                filter_checks=[],
                expected_fields=[],
                duplicate_key_fields=[],
            ),
            _case(
                method="hm_list",
                probe_id="hm-list-conservative",
                nested={"name": "章盟主"},
                filter_checks=[],
                expected_fields=[],
                duplicate_key_fields=[],
            ),
        ]
        receipt = audit.run_audit(
            cases=cases,
            transport=FakeTransport(_manifest_response),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True] * len(cases)),
            clock=FakeClock(),
        )
        self.assertEqual(["pass_nonempty"] * len(cases), [case["classification"] for case in receipt["cases"]])
        self.assertEqual(["pass"] * len(cases), [case["filter_checks"]["status"] for case in receipt["cases"]])
        self.assertEqual([{}] * len(cases), [case["filter_checks"]["checks"] for case in receipt["cases"]])

    def test_case_identity_materializes_inferred_filters_and_blocks_resume_semantic_drift(self):
        audit = _audit_module(self)
        raw_case = _case()
        normalized = audit._normalise_case(raw_case)
        self.assertNotIn("_infer_filter_checks", normalized)
        self.assertEqual(
            ["ts_code", "trade_date"],
            [check["field"] for check in normalized["filter_checks"]],
        )
        first = audit.run_audit(
            cases=[raw_case],
            transport=FakeTransport(),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        explicit_empty = _case(filter_checks=[])
        with self.assertRaises(ValueError):
            audit.run_audit(
                cases=[explicit_empty],
                resume=first,
                transport=FakeTransport(),
                token_loader=lambda: SYNTHETIC_TOKEN,
                budget=SequenceBudget([True]),
                clock=FakeClock(),
            )

    def test_unknown_schema_and_non_complete_points_cannot_be_primary(self):
        audit = _audit_module(self)
        unknown = _case(
            method="hm_list",
            nested={},
            expected_fields=[],
            filter_checks=[],
            duplicate_key_fields=[],
            primary_declared=False,
        )
        point = _case(
            method="realtime_quote",
            nested={"ts_code": "600519.SH"},
            fields="ts_code",
            expected_fields=["ts_code"],
            filter_checks=[{"field": "ts_code", "kind": "set_equal"}],
            duplicate_key_fields=[],
            pagination={
                "total_field": "total",
                "limit_field": "limit",
                "offset_field": "offset",
                "total_required": False,
                "truncation_policy": "fail_if_total_exceeds_rows",
            },
            primary_declared=False,
        )
        unknown_receipt = audit.run_audit(
            cases=[unknown],
            transport=FakeTransport(_manifest_response),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        unknown_item = unknown_receipt["cases"][0]
        self.assertFalse(unknown_item["primary_eligible"])
        self.assertFalse(
            audit.primary_eligible(
                unknown_item["classification"],
                method=unknown_item["method"],
                category=unknown_item["category"],
                primary_declared=unknown_item["primary_declared"],
                expected_fields=unknown_item["expected_fields"],
                filter_checks=unknown_item["filter_checks"],
                duplicate_key_checks=unknown_item["duplicate_key_checks"],
                pagination_truncation_checks=unknown_item["pagination_truncation_checks"],
                pagination=unknown_item["pagination"],
            )
        )
        point_response = FakeResponse(payload=_success_payload(fields=["ts_code"], rows=[["600519.SH"]]))
        point_response._payload.pop("total")
        point_receipt = audit.run_audit(
            cases=[point],
            transport=FakeTransport(point_response),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        self.assertEqual("pass_nonempty", point_receipt["cases"][0]["classification"])
        self.assertFalse(point_receipt["cases"][0]["primary_eligible"])

    def test_primary_declaration_is_separate_from_runtime_eligibility(self):
        audit = _audit_module(self)
        case = _case(
            nested={"ts_code": "000001.SZ", "trade_date": "20250930"},
            fields="ts_code,trade_date",
            expected_fields=["ts_code", "trade_date"],
            filter_checks=[
                {"field": "ts_code", "kind": "set_equal"},
                {"field": "trade_date", "kind": "date_equal"},
            ],
            duplicate_key_fields=[],
            pagination={
                "total_field": "total",
                "limit_field": "limit",
                "offset_field": "offset",
                "total_required": True,
                "truncation_policy": "fail_if_total_exceeds_rows",
            },
            primary_declared=True,
        )
        responses = (
            FakeResponse(
                payload=_success_payload(
                    rows=[["000001.SZ", "20250930"]],
                    total=1,
                    fields=["ts_code", "trade_date"],
                )
            ),
            FakeResponse(
                payload=_success_payload(rows=[], total=0, fields=["ts_code", "trade_date"])
            ),
            FakeResponse(
                payload=_success_payload(
                    rows=[["600519.SH", "20250930"]],
                    total=1,
                    fields=["ts_code", "trade_date"],
                )
            ),
        )
        expected = (("pass_nonempty", True), ("reachable_empty", False), ("semantic_fail", False))
        for response, (classification, eligible) in zip(responses, expected):
            with self.subTest(classification=classification):
                receipt = audit.run_audit(
                    cases=[case],
                    transport=FakeTransport(response),
                    token_loader=lambda: SYNTHETIC_TOKEN,
                    budget=SequenceBudget([True]),
                    clock=FakeClock(),
                )
                item = receipt["cases"][0]
                self.assertEqual(classification, item["classification"])
                self.assertTrue(item["primary_eligible"] is eligible)
                self.assertTrue(item["primary_declared"])
                audit.validate_receipt(receipt)

    def test_code_like_opaque_values_are_rejected_but_market_codes_survive(self):
        audit = _audit_module(self)
        opaque = "A1" * 20
        for key in ("code", "id", "identifier", "business_id", "ts_code"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    audit.validate_receipt_safety({key: opaque})
        for key, value in (
            ("ts_code", "600519.SH"),
            ("code", "300001.SZ"),
            ("contract_code", "CU2507.SHF"),
            ("theme_code", "885001.TI"),
            ("id", "AAPL"),
        ):
            with self.subTest(valid=(key, value)):
                audit.validate_receipt_safety({key: value})

    def test_short_secret_keys_are_rejected_but_provider_auth_metadata_survives(self):
        audit = _audit_module(self)
        for key in ("api_key", "private_key", "auth", "auth_token", "access_token"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    audit.validate_receipt_safety({key: "safe-looking-value"})
        audit.validate_receipt_safety(
            {
                "provider_metadata": {
                    "auth_status": "unverified",
                    "auth_required": True,
                    "auth_type": "read_only",
                    "token_status": "not_exposed",
                }
            }
        )

    def test_wildcard_ts_code_filter_matches_patterns(self):
        audit = _audit_module(self)
        case = _case(
            method="rt_k",
            nested={"ts_code": "3*.SZ"},
            fields="ts_code",
            expected_fields=["ts_code"],
            filter_checks=[{"field": "ts_code", "kind": "pattern"}],
            duplicate_key_fields=[],
        )
        good = audit.run_audit(
            cases=[case],
            transport=FakeTransport(FakeResponse(payload=_success_payload(fields=["ts_code"], rows=[["300001.SZ"]]))),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        self.assertEqual("pass_nonempty", good["cases"][0]["classification"])
        self.assertEqual("pass", good["cases"][0]["filter_checks"]["status"])
        bad = audit.run_audit(
            cases=[case],
            transport=FakeTransport(FakeResponse(payload=_success_payload(fields=["ts_code"], rows=[["600519.SH"]]))),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        self.assertEqual("semantic_fail", bad["cases"][0]["classification"])

    def test_legal_empty_without_total_stays_reachable_empty_and_not_primary(self):
        audit = _audit_module(self)
        case = _case(
            method="fund_company",
            nested={},
            fields="marker",
            expected_fields=["marker"],
            filter_checks=[],
            duplicate_key_fields=[],
            pagination={
                "total_field": "total",
                "limit_field": "limit",
                "offset_field": "offset",
                "total_required": True,
                "truncation_policy": "fail_if_total_exceeds_rows",
            },
        )
        response = FakeResponse(
            payload={
                "code": 0,
                "data": {"fields": ["marker"], "items": []},
            }
        )
        receipt = audit.run_audit(
            cases=[case],
            transport=FakeTransport(response),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        item = receipt["cases"][0]
        self.assertEqual("reachable_empty", item["classification"])
        self.assertEqual("unknown", item["pagination_truncation_checks"]["status"])
        self.assertFalse(item["pagination_truncation_checks"]["total_present"])
        self.assertFalse(item["primary_eligible"])

    def test_pagination_total_requirement_is_evidence_based_and_nonuniform(self):
        audit = _audit_module(self)
        probes = audit.load_probes()["methods"]
        required = []
        optional = []
        for method, entries in probes.items():
            for entry in entries:
                (required if entry["pagination"]["total_required"] else optional).append((method, entry))
        self.assertTrue(required)
        self.assertTrue(optional)
        for method, entry in optional:
            nested = entry["params"]["params"]
            with self.subTest(method=method):
                self.assertFalse(set(nested) & {"start_date", "end_date", "start_m", "end_m", "limit", "offset"})
                self.assertEqual("fail_if_total_exceeds_rows", entry["pagination"]["truncation_policy"])

    def test_packaged_manifest_anchor_rejects_self_rehashed_mutation(self):
        audit = _audit_module(self)
        changed = copy.deepcopy(audit.load_probes())
        changed["methods"]["daily"][0]["fixed_date"] = "20250929"
        changed["manifest_sha256"] = audit._manifest_hash(changed)
        with self.assertRaises(ValueError):
            audit.validate_probe_manifest(changed)

    def test_receipt_validator_rejects_current_canonical_probe_manifest_drift(self):
        audit = _audit_module(self)
        receipt = audit.run_audit(
            cases=[_case()],
            transport=FakeTransport(),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        changed = copy.deepcopy(audit.load_probes())
        changed["methods"]["daily"][0]["fixed_date"] = "20250929"
        changed["manifest_sha256"] = audit._manifest_hash(changed)
        with patch.object(audit, "load_probes", return_value=changed):
            with self.assertRaises(ValueError):
                audit.validate_receipt(receipt)

    def test_probe_manifest_covers_inventory_plus_account_diagnostic(self):
        audit = _audit_module(self)
        from ym_stock_data.providers.stocktoday_inventory import load_inventory, method_map

        inventory = load_inventory()
        probes = audit.load_probes()
        self.assertEqual(set(method_map()), set(probes["methods"]))
        self.assertEqual(245, len(probes["methods"]))
        self.assertEqual("token_info", probes["account_diagnostic"]["name"])
        self.assertNotIn("token_info", [item["name"] for item in inventory["methods"]])

    def test_manifest_uses_examples_and_explicit_fixed_parameters(self):
        audit = _audit_module(self)
        from ym_stock_data.providers.stocktoday_inventory import load_inventory

        inventory = load_inventory()
        probes = audit.load_probes()
        with self.subTest(kind="counts"):
            self.assertEqual(
                209,
                sum(
                    probes["methods"][item["name"]][0]["source"] == "vendor_example"
                    for item in inventory["methods"]
                ),
            )
            self.assertEqual(
                36,
                sum(
                    probes["methods"][item["name"]][0]["source"] == "fixed_parameters"
                    for item in inventory["methods"]
                ),
            )
        for item in inventory["methods"]:
            with self.subTest(method=item["name"]):
                first = probes["methods"][item["name"]][0]
                self.assertEqual(item["name"], first["params"]["api_name"])
                self.assertIsInstance(first["params"], dict)
                self.assertIn("fixed_date", first)
                self.assertEqual("20250930", first["fixed_date"])
                if item["has_example"]:
                    expected = dict(item["example"])
                    fields = expected.pop("fields", "")
                    self.assertEqual(expected, first["params"]["params"])
                    self.assertEqual(fields, first["params"].get("fields", ""))
                else:
                    self.assertEqual("fixed_parameters", first["source"])
                    if not first["params"].get("params") and not first["params"].get("fields"):
                        self.assertEqual([], first["expected_fields"])
                        self.assertEqual([], first["filter_checks"])
                        self.assertEqual([], first["duplicate_key_fields"])
                    else:
                        self.assertTrue(
                            first["params"].get("params")
                            or first["params"].get("fields")
                        )

    def test_manifest_is_canonical_and_token_info_is_not_a_route(self):
        audit = _audit_module(self)
        manifest_path = ROOT / "ym_stock_data" / "v3" / "stocktoday-probes.v3.json"
        self.assertTrue(manifest_path.is_file())
        self.assertFalse((ROOT / "stocktoday-probes.v3.json").exists())
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(payload, json.loads(manifest_path.read_text(encoding="utf-8")))
        self.assertRegex(payload["manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(audit.load_probes(), payload)

        from ym_stock_data.providers.stocktoday_catalog import API_PARAMS
        from ym_stock_data.routing import all_route_specs

        self.assertNotIn("token_info", API_PARAMS)
        self.assertNotIn("token_info", " ".join(spec.intent for spec in all_route_specs()))

    def test_fixed_probe_parameters_use_method_specific_business_semantics(self):
        audit = _audit_module(self)
        probes = audit.load_probes()["methods"]
        from ym_stock_data.providers.stocktoday_catalog import API_PARAMS

        expected = {
            "balancesheet_vip": {"ts_code": "600519.SH", "period": "20241231"},
            "cashflow_vip": {"ts_code": "600519.SH", "period": "20241231"},
            "dc_concept": {"trade_date": "20250930"},
            "dc_concept_cons": {"trade_date": "20250930"},
            "etf_mins": {
                "ts_code": "510330.SH",
                "freq": "1min",
                "start_date": "2025-09-30 09:30:00",
                "end_date": "2025-09-30 10:00:00",
            },
            "express_vip": {
                "ts_code": "600519.SH",
                "start_date": "20240101",
                "end_date": "20241231",
            },
            "fina_indicator_vip": {"ts_code": "600519.SH", "period": "20241231"},
            "fina_mainbz_vip": {
                "ts_code": "600519.SH",
                "start_date": "20240101",
                "end_date": "20241231",
            },
            "forecast_vip": {"ts_code": "600519.SH", "period": "20241231"},
            "fund_company": {},
            "fund_factor_pro": {
                "ts_code": "510330.SH",
                "start_date": "20250101",
                "end_date": "20250930",
            },
            "fund_sales_ratio": {"year": "2024"},
            "fund_sales_vol": {"year": "2024", "quarter": "4"},
            "fut_weekly_monthly": {
                "ts_code": "CU2507.SHF",
                "freq": "W",
                "start_date": "20250101",
                "end_date": "20250930",
                "exchange": "SHFE",
            },
            "hk_basic": {"ts_code": "00700.HK", "list_status": "L"},
            "hm_list": {},
            "idx_factor_pro": {
                "ts_code": "000300.SH",
                "start_date": "20250101",
                "end_date": "20250930",
            },
            "income_vip": {"ts_code": "600519.SH", "period": "20241231"},
            "kpl_concept": {"trade_date": "20250930"},
            "pro_bar": {
                "ts_code": "600519.SH",
                "start_date": "20250901",
                "end_date": "20250930",
                "asset": "E",
                "adj": "qfq",
                "freq": "D",
            },
            "realtime_list": {},
            "realtime_quote": {"ts_code": "600519.SH"},
            "realtime_tick": {"ts_code": "600519.SH"},
            "rt_etf_min": {"ts_code": "510330.SH", "freq": "1MIN"},
            "rt_etf_tick": {"ts_code": "510330.SH", "topic": "1"},
            "rt_hk_tick": {"ts_code": "00700.HK"},
            "rt_idx_tick": {"ts_code": "000300.SH"},
            "rt_sw_k": {"ts_code": "801010.SI"},
            "rt_sw_tick": {"ts_code": "801010.SI"},
            "rt_tick": {"ts_code": "600519.SH"},
            "st": {"ts_code": "600519.SH", "pub_date": "20250930", "imp_date": "20250930"},
            "stk_factor_pro": {
                "ts_code": "600519.SH",
                "trade_date": "20250930",
                "start_date": "20250101",
                "end_date": "20250930",
            },
            "ths_index": {"ts_code": "885001.TI", "type": "N"},
            "ths_news": {"start_date": "20250901", "end_date": "20250930", "limit": 100},
            "top_inst": {"ts_code": "600519.SH", "trade_date": "20250930"},
            "us_basic": {"ts_code": "AAPL"},
        }
        self.assertEqual(36, len(expected))
        for method, required in expected.items():
            with self.subTest(method=method):
                nested = probes[method][0]["params"]["params"]
                self.assertEqual(required, nested)
                self.assertTrue(set(nested).issubset(API_PARAMS[method]))
                for forbidden in ("comp_type", "report_type"):
                    self.assertNotIn(forbidden, nested)
                if method in {
                    "balancesheet_vip",
                    "cashflow_vip",
                    "fina_indicator_vip",
                    "income_vip",
                    "forecast_vip",
                }:
                    self.assertEqual("20241231", nested.get("period"))
                self.assertNotEqual("000001.SZ", nested.get("ts_code"))

    def test_manifest_declares_expected_fields_filter_checks_and_nonuniform_pagination(self):
        audit = _audit_module(self)
        probes = audit.load_probes()["methods"]
        pagination_shapes = set()
        for method, entries in probes.items():
            for entry in entries:
                with self.subTest(method=method, probe=entry["probe_id"]):
                    self.assertIsInstance(entry.get("expected_fields"), list)
                    if entry["expected_fields"]:
                        self.assertTrue(entry["expected_fields"])
                    else:
                        self.assertEqual([], entry["filter_checks"])
                        self.assertEqual([], entry["duplicate_key_fields"])
                    self.assertIsInstance(entry.get("filter_checks"), list)
                    self.assertIsInstance(entry["pagination"], dict)
                    self.assertIsInstance(entry["pagination"].get("total_required"), bool)
                    self.assertIsInstance(entry.get("primary_declared"), bool)
                    self.assertNotIn("primary_eligible", entry)
                    ts_code = entry["params"]["params"].get("ts_code")
                    if isinstance(ts_code, str) and any(marker in ts_code for marker in ("*", "?")):
                        self.assertIn(
                            {"field": "ts_code", "kind": "pattern"},
                            entry["filter_checks"],
                        )
                    self.assertIn(
                        entry["pagination"].get("truncation_policy"),
                        {"fail_if_total_exceeds_rows", "report_if_total_exceeds_rows"},
                    )
                    pagination_shapes.add(
                        (
                            entry["pagination"].get("total_field"),
                            entry["pagination"].get("truncation_policy"),
                        )
                    )
        self.assertGreater(len(pagination_shapes), 1)

    def test_all_primary_probes_reach_the_fake_transport(self):
        audit = _audit_module(self)
        cases = audit.default_cases()
        self.assertEqual(246, len(cases))
        self.assertEqual(1, sum(case["method"] == "token_info" for case in cases))
        transport = FakeTransport(_manifest_response)
        receipt = audit.run_audit(
            cases=cases,
            transport=transport,
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True] * 300),
            clock=FakeClock(),
        )
        called_names = [request["json"]["api_name"] for _, request in transport.calls]
        self.assertEqual(246, len(transport.calls))
        self.assertEqual(set(audit.load_probes()["methods"]), set(called_names) - {"token_info"})
        self.assertEqual(245, len(set(called_names) - {"token_info"}))
        self.assertNotIn("invalid_params", receipt["classifications"])

    def test_account_diagnostic_is_a_separate_safe_receipt_case(self):
        audit = _audit_module(self)
        case = next(case for case in audit.default_cases() if case["method"] == "token_info")
        transport = FakeTransport(_manifest_response)
        receipt = audit.run_audit(
            cases=[case],
            transport=transport,
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        self.assertEqual(1, len(transport.calls))
        url, request = transport.calls[0]
        self.assertEqual("https://tushare.citydata.club/token_info", url)
        self.assertEqual(SYNTHETIC_TOKEN, request["json"]["token"])
        self.assertFalse(request["json"]["params"].get("token"))
        item = receipt["cases"][0]
        self.assertEqual("account_diagnostic", item["category"])
        self.assertEqual("pass_nonempty", item["classification"])
        self.assertFalse(audit.primary_eligible(item["classification"], method="token_info"))
        self.assertNotIn(SYNTHETIC_TOKEN, json.dumps(receipt, ensure_ascii=False))

        diagnostic_without_total = _manifest_response(None, {"json": {"api_name": "token_info", "params": {}}})
        diagnostic_without_total._payload.pop("total")
        no_total = audit.run_audit(
            cases=[case],
            transport=FakeTransport(diagnostic_without_total),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        self.assertEqual("pass_nonempty", no_total["cases"][0]["classification"])
        self.assertEqual("not_applicable", no_total["cases"][0]["pagination_truncation_checks"]["status"])

        failed = audit.run_audit(
            cases=[case],
            transport=FakeTransport(FakeResponse(status_code=500, payload={"code": 500, "msg": "unavailable"})),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        self.assertIn(failed["cases"][0]["classification"], {"provider_error", "reachable_empty"})

    def test_temporary_second_probe_is_expanded_and_executed(self):
        audit = _audit_module(self)
        manifest = copy.deepcopy(audit.load_probes())
        second = copy.deepcopy(manifest["methods"]["daily"][0])
        second["probe_id"] = "temporary-second"
        manifest["methods"]["daily"].append(second)
        with patch.object(audit, "load_probes", return_value=manifest):
            cases = audit.default_cases()
            self.assertEqual(247, len(cases))
            self.assertEqual(2, sum(case["method"] == "daily" for case in cases))
            transport = FakeTransport(_manifest_response)
            audit.run_audit(
                cases=cases,
                transport=transport,
                token_loader=lambda: SYNTHETIC_TOKEN,
                budget=SequenceBudget([True] * 300),
                clock=FakeClock(),
            )
        daily_calls = [request for _, request in transport.calls if request["json"]["api_name"] == "daily"]
        self.assertEqual(2, len(daily_calls))

    def test_provenance_preserves_numeric_status_and_upstream_codes(self):
        audit = _audit_module(self)
        from ym_stock_data.providers.stocktoday import StockTodayProvider

        for http_status, expected_classification in ((401, "auth_denied"), (403, "auth_denied"), (429, "rate_limited")):
            with self.subTest(http_status=http_status):
                transport = FakeTransport(
                    FakeResponse(
                        status_code=http_status,
                        payload={"code": 7001, "msg": "neutral", "data": {"fields": [], "items": []}, "total": 0},
                    )
                )
                provider = StockTodayProvider(
                    token_loader=lambda: SYNTHETIC_TOKEN,
                    post=transport,
                    budget=SequenceBudget([True]),
                    clock=FakeClock(),
                )
                outcome = provider.call(
                    "stocktoday_data",
                    {"api_name": "daily", "params": {"ts_code": "000001.SZ", "trade_date": "20250930"}},
                )
                self.assertEqual(expected_classification, audit.classify({"error_code": outcome.error_code}))
                self.assertEqual(http_status, outcome.provenance["http_status"])
                self.assertEqual(7001, outcome.provenance["upstream_code"])
                self.assertFalse(outcome.provenance["total_present"])

                receipt = audit.run_audit(
                    cases=[_case()],
                    transport=transport,
                    token_loader=lambda: SYNTHETIC_TOKEN,
                    budget=SequenceBudget([True]),
                    clock=FakeClock(),
                )
                self.assertEqual(expected_classification, receipt["cases"][0]["classification"])
                self.assertEqual(7001, receipt["cases"][0]["provider_code"])

    def test_auth_and_rate_thresholds_are_counted_independently(self):
        audit = _audit_module(self)
        statuses = [401, 429, 429, 200]

        def response(url, kwargs):
            status = statuses[len(transport.calls) - 1]
            if status == 200:
                return FakeResponse(payload=_success_payload())
            return FakeResponse(status_code=status, payload={"code": 7001, "msg": "neutral"})

        transport = FakeTransport(response)
        receipt = audit.run_audit(
            cases=[_case("daily", probe_id=f"p{index}") for index in range(4)],
            transport=transport,
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True] * 4),
            clock=FakeClock(),
            stop_threshold=2,
        )
        self.assertEqual(3, len(transport.calls))
        self.assertEqual(["auth_denied", "rate_limited", "rate_limited", "not_run"], [case["classification"] for case in receipt["cases"]])
        self.assertEqual("rate_threshold", receipt["cases"][3]["not_run_reason"])

    def test_missing_total_is_explicit_and_blocks_a_pass(self):
        audit = _audit_module(self)
        payload = _success_payload()
        payload.pop("total")
        receipt = audit.run_audit(
            cases=[_case()],
            transport=FakeTransport(FakeResponse(payload=payload)),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        item = receipt["cases"][0]
        self.assertFalse(item["pagination_truncation_checks"]["total_present"])
        self.assertEqual("unknown", item["pagination_truncation_checks"]["status"])
        self.assertNotEqual("pass_nonempty", item["classification"])

    def test_requested_fields_and_nonfinite_rows_are_schema_errors(self):
        audit = _audit_module(self)
        missing = audit.run_audit(
            cases=[_case(fields="close")],
            transport=FakeTransport(
                FakeResponse(
                    payload=_success_payload(
                        fields=["ts_code", "trade_date"], rows=[["000001.SZ", "20250930"]]
                    )
                )
            ),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        self.assertEqual("schema_error", missing["cases"][0]["classification"])

        nonfinite = audit.run_audit(
            cases=[_case()],
            transport=FakeTransport(
                FakeResponse(payload=_success_payload(rows=[["000001.SZ", "20250930", float("nan")]]))
            ),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        self.assertEqual("schema_error", nonfinite["cases"][0]["classification"])

    def test_fail_closed_filter_checks_cover_scalar_values_and_missing_columns(self):
        audit = _audit_module(self)
        case = _case(
            method="limit_list_d",
            nested={"ts_code": "000001.SZ", "trade_date": "20250930", "limit_type": "U"},
            fields="ts_code,trade_date,limit_type",
        )
        mismatch = audit.run_audit(
            cases=[case],
            transport=FakeTransport(
                FakeResponse(
                    payload=_success_payload(
                        fields=["ts_code", "trade_date", "limit_type"],
                        rows=[["000001.SZ", "20250930", "D"]],
                    )
                )
            ),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        self.assertEqual("semantic_fail", mismatch["cases"][0]["classification"])
        self.assertEqual("fail", mismatch["cases"][0]["filter_checks"]["status"])

        missing = audit.run_audit(
            cases=[_case(method="limit_list_d", nested={"ts_code": "000001.SZ", "trade_date": "20250930", "limit_type": "U"}, fields="ts_code,trade_date")],
            transport=FakeTransport(
                FakeResponse(
                    payload=_success_payload(
                        fields=["ts_code", "trade_date"], rows=[["000001.SZ", "20250930"]]
                    )
                )
            ),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        self.assertEqual("semantic_fail", missing["cases"][0]["classification"])

    def test_receipt_hash_and_strict_schema_reject_tampering(self):
        audit = _audit_module(self)
        receipt = audit.run_audit(
            cases=[_case()],
            transport=FakeTransport(),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        unsigned = copy.deepcopy(receipt)
        receipt_hash = unsigned.pop("receipt_sha256")
        self.assertEqual(receipt_hash, audit._sha256(unsigned))
        for mutate in (
            lambda value: value["cases"][0].update({"classification": "auth_denied"}),
            lambda value: value.update({"unknown": True}),
            lambda value: value["cases"][0].update({"unknown": True}),
        ):
            with self.subTest(mutate=mutate):
                changed = copy.deepcopy(receipt)
                mutate(changed)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "receipt.json"
                    path.write_text(json.dumps(changed), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        audit.audit_status(path)

        auth_receipt = audit.run_audit(
            cases=[_case()],
            transport=FakeTransport(FakeResponse(status_code=401, payload={"code": 401, "msg": "neutral"})),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        promoted = copy.deepcopy(auth_receipt)
        promoted["cases"][0]["classification"] = "pass_nonempty"
        promoted["counts"] = audit._counts(promoted["cases"])
        promoted["classifications"] = sorted(status for status, count in promoted["counts"].items() if count)
        unsigned = copy.deepcopy(promoted)
        unsigned.pop("receipt_sha256")
        promoted["receipt_sha256"] = audit._sha256(unsigned)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth-promoted.json"
            path.write_text(json.dumps(promoted), encoding="utf-8")
            with self.assertRaises(ValueError):
                audit.audit_status(path)

    def test_payload_hash_never_drops_rows_and_safety_rejects_embedded_urls(self):
        audit = _audit_module(self)
        self.assertNotEqual(
            audit._payload_hash(0, ["x"], [{"x": 1}], 1),
            audit._payload_hash(0, ["x"], [{"x": 2}], 1),
        )
        with self.assertRaises((TypeError, ValueError, OverflowError)):
            audit._payload_hash(0, ["x"], [{"x": object()}], 1)
        audit.validate_receipt_safety({"business_hash": "a1" * 20})
        for bad in (
            {"note": "prefix https://example.invalid/path"},
            {"note": "x?token=hidden"},
            {"note": "x?password=hidden"},
            {"bearer": "present"},
            {"client_id": "present"},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    audit.validate_receipt_safety(bad)
        audit.validate_receipt_safety(
            {"auth_required": True, "auth_status": "ok", "auth_source": "secure_store"}
        )

    def test_real_sqlite_budget_completes_all_246_cases_with_safe_waits(self):
        audit = _audit_module(self)
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            budget_path = Path(directory) / "budget.sqlite3"
            from ym_stock_data.providers.stocktoday import RequestBudget

            budget = RequestBudget(budget_path, per_minute=60, per_day=1000)
            transport = FakeTransport(_manifest_response)
            receipt = audit.run_audit(
                transport=transport,
                token_loader=lambda: SYNTHETIC_TOKEN,
                budget=budget,
                clock=clock,
                sleeper=clock.sleep,
            )
            self.assertEqual(246, len(transport.calls))
            self.assertEqual(246, len(receipt["cases"]))
            self.assertNotIn("not_run", receipt["classifications"])
            self.assertTrue(clock.sleeps)
            self.assertLessEqual(max(clock.sleeps), 61.0)
            with sqlite3.connect(budget_path) as connection:
                self.assertEqual(246, connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0])

    def test_classifications_are_exact_and_empty_is_not_primary_eligible(self):
        audit = _audit_module(self)
        expected = {
            "pass_nonempty",
            "reachable_empty",
            "auth_denied",
            "rate_limited",
            "timeout",
            "invalid_params",
            "schema_error",
            "semantic_fail",
            "provider_error",
            "not_run",
        }
        self.assertEqual(expected, audit.AUDIT_STATUSES)
        self.assertEqual(
            "reachable_empty",
            audit.classify(
                {
                    "provider_code": 0,
                    "rows": [],
                    "columns": [],
                    "total": 0,
                    "filter_checks": {"status": "pass"},
                    "duplicate_key_checks": {"status": "not_applicable"},
                    "pagination_truncation_checks": {"status": "pass"},
                }
            ),
        )
        self.assertEqual(
            "semantic_fail",
            audit.classify(
                {
                    "provider_code": 0,
                    "rows": [{"trade_date": "20250929"}],
                    "columns": ["trade_date"],
                    "total": 1,
                    "filter_checks": {"status": "fail"},
                    "duplicate_key_checks": {"status": "not_applicable"},
                    "pagination_truncation_checks": {"status": "pass"},
                }
            ),
        )
        self.assertFalse(audit.primary_eligible("reachable_empty"))
        self.assertFalse(audit.primary_eligible("pass_nonempty"))
        self.assertTrue(
            audit.primary_eligible(
                "pass_nonempty",
                primary_declared=True,
                expected_fields=["trade_date"],
                columns=["trade_date"],
                declared_filter_checks=[
                    {"field": "trade_date", "kind": "date_equal"}
                ],
                duplicate_key_fields=[],
                filter_checks={
                    "status": "pass",
                    "checks": {
                        "trade_date": {
                            "status": "pass",
                            "expected": "20250930",
                        }
                    },
                },
                duplicate_key_checks={
                    "status": "not_applicable",
                    "key_fields": [],
                    "duplicate_count": 0,
                    "reason": None,
                },
                pagination_truncation_checks={
                    "status": "pass",
                    "total": 1,
                    "total_present": True,
                    "limit": None,
                    "offset": 0,
                    "reason": None,
                    "truncation_policy": "fail_if_total_exceeds_rows",
                },
                pagination={
                    "total_field": "total",
                    "limit_field": "limit",
                    "offset_field": "offset",
                    "total_required": True,
                    "truncation_policy": "fail_if_total_exceeds_rows",
                },
            )
        )

    def test_primary_and_receipt_checks_require_complete_declared_evidence(self):
        audit = _audit_module(self)
        declared_filters = [{"field": "trade_date", "kind": "date_equal"}]
        filter_checks = {
            "status": "pass",
            "checks": {
                "trade_date": {"status": "pass", "expected": "20250930"}
            },
        }
        duplicate = {
            "status": "not_applicable",
            "key_fields": [],
            "duplicate_count": 0,
            "reason": None,
        }
        pagination = {
            "status": "pass",
            "total": 1,
            "total_present": True,
            "limit": None,
            "offset": 0,
            "reason": None,
            "truncation_policy": "fail_if_total_exceeds_rows",
        }
        declaration = {
            "total_field": "total",
            "limit_field": "limit",
            "offset_field": "offset",
            "total_required": True,
            "truncation_policy": "fail_if_total_exceeds_rows",
        }

        invalid_primary = [
            (
                "duplicate_missing_reason",
                {key: value for key, value in duplicate.items() if key != "reason"},
            ),
            (
                "duplicate_not_checked",
                {**duplicate, "status": "not_checked"},
            ),
            ("duplicate_count_type", {**duplicate, "duplicate_count": "0"}),
            ("pagination_missing_reason", {key: value for key, value in pagination.items() if key != "reason"}),
            (
                "pagination_not_checked",
                {**pagination, "status": "not_checked"},
            ),
            (
                "pagination_policy_drift",
                {**pagination, "truncation_policy": "report_if_total_exceeds_rows"},
            ),
            (
                "filter_missing_actual_check",
                {"status": "pass", "checks": {}},
            ),
        ]
        for name, candidate in invalid_primary:
            with self.subTest(name=name):
                duplicate_candidate = candidate if name.startswith("duplicate") else duplicate
                pagination_candidate = candidate if name.startswith("pagination") else pagination
                filter_candidate = candidate if name.startswith("filter") else filter_checks
                self.assertFalse(
                    audit.primary_eligible(
                        "pass_nonempty",
                        method="daily",
                        category="测试",
                        primary_declared=True,
                        expected_fields=["trade_date"],
                        columns=["trade_date"],
                        declared_filter_checks=declared_filters,
                        filter_checks=filter_candidate,
                        duplicate_key_checks=duplicate_candidate,
                        pagination_truncation_checks=pagination_candidate,
                        pagination=declaration,
                    )
                )

        valid_receipt = audit.run_audit(
            cases=[_case()],
            transport=FakeTransport(),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )

        def resigned(mutate):
            candidate = copy.deepcopy(valid_receipt)
            mutate(candidate["cases"][0])
            unsigned = copy.deepcopy(candidate)
            unsigned.pop("receipt_sha256")
            candidate["receipt_sha256"] = audit._sha256(unsigned)
            return candidate

        invalid_receipts = (
            resigned(lambda item: item["duplicate_key_checks"].pop("reason", None)),
            resigned(lambda item: item["duplicate_key_checks"].update({"status": "not_checked"})),
            resigned(lambda item: item["pagination_truncation_checks"].pop("truncation_policy")),
            resigned(lambda item: item["pagination_truncation_checks"].update({"status": "not_checked"})),
            resigned(lambda item: item["filter_checks"].update({"checks": {}})),
        )
        for candidate in invalid_receipts:
            with self.assertRaises(ValueError):
                audit.validate_receipt(candidate)

    def test_duplicate_declaration_must_bind_to_columns_before_primary_eligibility(self):
        audit = _audit_module(self)
        case = _case(
            nested={"ts_code": "000001.SZ", "trade_date": "20250930"},
            fields="ts_code,trade_date",
            expected_fields=["ts_code", "trade_date"],
            filter_checks=[
                {"field": "ts_code", "kind": "set_equal"},
                {"field": "trade_date", "kind": "date_equal"},
            ],
            duplicate_key_fields=["ts_code"],
            pagination={
                "total_field": "total",
                "limit_field": "limit",
                "offset_field": "offset",
                "total_required": True,
                "truncation_policy": "fail_if_total_exceeds_rows",
            },
            primary_declared=True,
        )
        receipt = audit.run_audit(
            cases=[case],
            transport=FakeTransport(
                FakeResponse(
                    payload=_success_payload(
                        rows=[["000001.SZ", "20250930"]],
                        total=1,
                        fields=["ts_code", "trade_date"],
                    )
                )
            ),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        self.assertTrue(receipt["cases"][0]["primary_eligible"])

        candidate = copy.deepcopy(receipt)
        item = candidate["cases"][0]
        item["duplicate_key_fields"] = ["bogus_duplicate_key"]
        item["duplicate_key_checks"]["key_fields"] = ["bogus_duplicate_key"]
        item["case_identity"] = audit._case_identity(
            {
                "method": item["method"],
                "probe_id": item["probe_id"],
                "category": item["category"],
                "params": item["sanitized_params"],
                "fixed_date": item["fixed_date"],
                "duplicate_key_fields": item["duplicate_key_fields"],
                "expected_fields": item["expected_fields"],
                "filter_checks": item["declared_filter_checks"],
                "pagination": item["pagination"],
                "primary_declared": item["primary_declared"],
            }
        )
        unsigned = copy.deepcopy(candidate)
        unsigned.pop("receipt_sha256")
        candidate["receipt_sha256"] = audit._sha256(unsigned)

        self.assertFalse(
            audit.primary_eligible(
                item["classification"],
                method=item["method"],
                category=item["category"],
                primary_declared=item["primary_declared"],
                expected_fields=item["expected_fields"],
                columns=item["columns"],
                declared_filter_checks=item["declared_filter_checks"],
                filter_checks=item["filter_checks"],
                duplicate_key_fields=item["duplicate_key_fields"],
                duplicate_key_checks=item["duplicate_key_checks"],
                pagination_truncation_checks=item["pagination_truncation_checks"],
                pagination=item["pagination"],
            )
        )
        with self.assertRaises(ValueError):
            audit.validate_receipt(candidate)

        primary_false_candidate = copy.deepcopy(candidate)
        primary_false_candidate["cases"][0]["primary_eligible"] = False
        unsigned = copy.deepcopy(primary_false_candidate)
        unsigned.pop("receipt_sha256")
        primary_false_candidate["receipt_sha256"] = audit._sha256(unsigned)
        with self.assertRaises(ValueError):
            audit.validate_receipt(primary_false_candidate)

    def test_schema_error_allows_canonical_duplicate_declaration_without_returned_columns(self):
        audit = _audit_module(self)
        case = next(
            item for item in audit.default_cases() if item["method"] == "idx_factor_pro"
        )
        receipt = audit.run_audit(
            cases=[case],
            transport=FakeTransport(
                FakeResponse(
                    payload={
                        "code": 0,
                        "data": {
                            "fields": ["ts_code"],
                            "items": [["000300.SH"]],
                        },
                        "total": 1,
                    }
                )
            ),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        item = receipt["cases"][0]
        self.assertEqual("schema_error", item["classification"])
        self.assertEqual([], item["columns"])
        self.assertEqual(["ts_code", "trade_date"], item["duplicate_key_fields"])
        self.assertEqual("not_applicable", item["duplicate_key_checks"]["status"])
        audit.validate_receipt(receipt)

        forged = copy.deepcopy(receipt)
        forged["cases"][0]["duplicate_key_fields"] = ["bogus_duplicate_key"]
        forged["cases"][0]["duplicate_key_checks"]["key_fields"] = []
        forged.pop("receipt_sha256")
        forged["receipt_sha256"] = audit._sha256(forged)
        with self.assertRaises(ValueError):
            audit.validate_receipt(forged)

    def test_schema_verifiable_classifications_still_require_duplicate_columns(self):
        audit = _audit_module(self)
        receipt = audit.run_audit(
            cases=[
                _case(
                    expected_fields=["ts_code", "trade_date", "close"],
                    duplicate_key_fields=["ts_code", "trade_date"],
                )
            ],
            transport=FakeTransport(),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        for classification in ("pass_nonempty", "semantic_fail", "reachable_empty"):
            with self.subTest(classification=classification):
                candidate = copy.deepcopy(receipt)
                item = candidate["cases"][0]
                item["classification"] = classification
                item["provider_code"] = "INVALID_RESPONSE"
                item["columns"] = ["ts_code", "close"]
                item["duplicate_key_checks"] = {
                    "status": "not_applicable",
                    "key_fields": [],
                    "duplicate_count": 0,
                    "reason": None,
                }
                item["primary_eligible"] = False
                if classification == "semantic_fail":
                    filter_checks = copy.deepcopy(item["filter_checks"])
                    filter_checks["status"] = "fail"
                    first_check = next(iter(filter_checks["checks"].values()))
                    first_check["status"] = "fail"
                    first_check["reason"] = "fixture_semantic_failure"
                    item["filter_checks"] = filter_checks
                if classification == "reachable_empty":
                    item["row_count"] = 0
                candidate.pop("receipt_sha256")
                candidate["receipt_sha256"] = audit._sha256(candidate)
                with self.assertRaises(ValueError):
                    audit.validate_receipt(candidate)

    def test_provider_code_consistency_rejects_forged_success_classifications_after_resigning(self):
        audit = _audit_module(self)
        receipt = audit.run_audit(
            cases=[
                _case(
                    expected_fields=["ts_code", "trade_date", "close"],
                    duplicate_key_fields=["ts_code", "trade_date"],
                )
            ],
            transport=FakeTransport(),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        for classification in ("pass_nonempty", "reachable_empty", "semantic_fail"):
            for forged_code in (1, 9999, "UPSTREAM_ERROR"):
                with self.subTest(classification=classification, forged_code=forged_code):
                    candidate = copy.deepcopy(receipt)
                    item = candidate["cases"][0]
                    item["classification"] = classification
                    item["provider_code"] = forged_code
                    item["primary_eligible"] = False
                    if classification == "reachable_empty":
                        item["row_count"] = 0
                    if classification == "semantic_fail":
                        filter_checks = copy.deepcopy(item["filter_checks"])
                        filter_checks["status"] = "fail"
                        first_check = next(iter(filter_checks["checks"].values()))
                        first_check["status"] = "fail"
                        first_check["reason"] = "fixture_semantic_failure"
                        item["filter_checks"] = filter_checks
                    candidate.pop("receipt_sha256")
                    candidate["receipt_sha256"] = audit._sha256(candidate)
                    with self.assertRaises(ValueError):
                        audit.validate_receipt(candidate)

    def test_provider_code_consistency_accepts_generator_error_codes_and_rejects_cross_class_forges(self):
        audit = _audit_module(self)
        scenarios = [
            (FakeResponse(status_code=401, payload={"code": 0, "msg": "denied"}), "auth_denied", 401),
            (FakeResponse(status_code=429, payload={"code": 0, "msg": "rate"}), "rate_limited", 429),
            (requests.Timeout(SYNTHETIC_TOKEN), "timeout", "TIMEOUT"),
        ]
        for response, classification, provider_code in scenarios:
            with self.subTest(classification=classification):
                receipt = audit.run_audit(
                    cases=[_case()],
                    transport=FakeTransport(response),
                    token_loader=lambda: SYNTHETIC_TOKEN,
                    budget=SequenceBudget([True]),
                    clock=FakeClock(),
                )
                self.assertEqual(classification, receipt["cases"][0]["classification"])
                self.assertEqual(provider_code, receipt["cases"][0]["provider_code"])
                audit.validate_receipt(receipt)
                forged = copy.deepcopy(receipt)
                forged["cases"][0]["provider_code"] = 0
                if classification == "timeout":
                    forged["cases"][0]["provider_code"] = "UPSTREAM_ERROR"
                forged.pop("receipt_sha256")
                forged["receipt_sha256"] = audit._sha256(forged)
                with self.assertRaises(ValueError):
                    audit.validate_receipt(forged)

    def test_pagination_declaration_must_be_complete_and_fail_closed(self):
        audit = _audit_module(self)
        receipt = audit.run_audit(
            cases=[_case()],
            transport=FakeTransport(),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )

        invalid_declarations = (
            {},
            {"total_required": True},
            {
                "total_field": "count",
                "limit_field": "limit",
                "offset_field": "offset",
                "total_required": True,
                "truncation_policy": "fail_if_total_exceeds_rows",
            },
            {
                "total_field": "total",
                "limit_field": "limit",
                "offset_field": "offset",
                "total_required": "yes",
                "truncation_policy": "fail_if_total_exceeds_rows",
            },
            {
                "total_field": "total",
                "limit_field": "limit",
                "offset_field": "offset",
                "total_required": True,
                "truncation_policy": "unknown_policy",
            },
        )
        for declaration in invalid_declarations:
            with self.subTest(declaration=declaration):
                candidate = copy.deepcopy(receipt)
                item = candidate["cases"][0]
                item["pagination"] = copy.deepcopy(declaration)
                item["case_identity"] = audit._case_identity(
                    {
                        "method": item["method"],
                        "probe_id": item["probe_id"],
                        "category": item["category"],
                        "params": item["sanitized_params"],
                        "fixed_date": item["fixed_date"],
                        "duplicate_key_fields": item["duplicate_key_fields"],
                        "expected_fields": item["expected_fields"],
                        "filter_checks": item["declared_filter_checks"],
                        "pagination": item["pagination"],
                        "primary_declared": item["primary_declared"],
                    }
                )
                unsigned = copy.deepcopy(candidate)
                unsigned.pop("receipt_sha256")
                candidate["receipt_sha256"] = audit._sha256(unsigned)
                with self.assertRaises(ValueError):
                    audit.validate_receipt(candidate)

    def test_audit_receipt_contains_only_sanitized_case_evidence(self):
        audit = _audit_module(self)
        clock = FakeClock()
        transport = FakeTransport(FakeResponse(payload=_success_payload()))
        receipt = audit.run_audit(
            cases=[_case()],
            transport=transport,
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=clock,
            sleeper=clock.sleep,
        )
        self.assertEqual(1, len(transport.calls))
        url, request = transport.calls[0]
        self.assertEqual("https://tushare.citydata.club/daily", url)
        self.assertEqual(SYNTHETIC_TOKEN, request["json"]["token"])
        case = receipt["cases"][0]
        for key in {
            "method",
            "category",
            "probe_id",
            "fixed_date",
            "sanitized_params",
            "requested_fields",
            "start",
            "end",
            "latency_ms",
            "row_count",
            "columns",
            "provider_code",
            "classification",
            "filter_checks",
            "duplicate_key_checks",
            "pagination_truncation_checks",
            "payload_hash",
            "payload_sha256",
        }:
            self.assertIn(key, case)
        self.assertEqual("20250930", case["fixed_date"])
        self.assertEqual(case["payload_hash"], case["payload_sha256"])
        self.assertEqual("pass_nonempty", case["classification"])
        self.assertEqual(1, case["row_count"])
        self.assertNotIn(SYNTHETIC_TOKEN, json.dumps(receipt, ensure_ascii=False))
        self.assertNotIn("rows", case)
        self.assertNotIn("message", case)
        self.assertNotIn("exception", case)
        self.assertNotIn("endpoint", case)
        audit.validate_receipt_safety(receipt)

    def test_runner_maps_provider_outcomes_to_all_non_not_run_classifications(self):
        audit = _audit_module(self)
        scenarios = [
            ("empty", FakeResponse(payload=_success_payload(rows=[], total=0)), "reachable_empty"),
            ("auth", FakeResponse(status_code=401, payload={"code": 401, "msg": SYNTHETIC_TOKEN}), "auth_denied"),
            ("rate", FakeResponse(status_code=429, payload={"code": 429, "msg": SYNTHETIC_TOKEN}), "rate_limited"),
            ("timeout", requests.Timeout(SYNTHETIC_TOKEN), "timeout"),
            ("schema", FakeResponse(payload={"code": 0, "data": {"fields": ["ts_code"], "items": [["000001.SZ", 1]]}}), "schema_error"),
            ("semantic", FakeResponse(payload=_success_payload(rows=[["000001.SZ", "20250929", 10.0]])), "semantic_fail"),
            ("provider", FakeResponse(status_code=500, payload={"code": 500, "msg": SYNTHETIC_TOKEN}), "provider_error"),
            ("invalid", None, "invalid_params"),
            ("pass", FakeResponse(payload=_success_payload()), "pass_nonempty"),
        ]
        for label, response, expected in scenarios:
            with self.subTest(label=label):
                transport = FakeTransport(response)
                clock = FakeClock()
                current_case = _case()
                if label == "invalid":
                    current_case["params"]["params"]["unsafe"] = "x"
                receipt = audit.run_audit(
                    cases=[current_case],
                    transport=transport,
                    token_loader=lambda: SYNTHETIC_TOKEN,
                    budget=SequenceBudget([True]),
                    clock=clock,
                    sleeper=clock.sleep,
                )
                self.assertEqual(expected, receipt["cases"][0]["classification"])
                self.assertNotIn(SYNTHETIC_TOKEN, json.dumps(receipt, ensure_ascii=False))

    def test_audit_never_repeats_completed_case_on_resume(self):
        audit = _audit_module(self)
        cases = [_case("daily", probe_id="one"), _case("stock_basic", probe_id="two", nested={})]
        first = audit.run_audit(
            cases=cases,
            transport=FakeTransport(FakeResponse(payload=_success_payload())),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True, True]),
            clock=FakeClock(),
        )
        second_transport = FakeTransport(response=AssertionError("completed case was rerun"))
        second = audit.run_audit(
            cases=cases,
            resume=first,
            transport=second_transport,
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True, True]),
            clock=FakeClock(),
        )
        self.assertEqual(first["cases"], second["cases"])
        self.assertEqual([], second_transport.calls)

    def test_not_run_case_is_retried_on_resume(self):
        audit = _audit_module(self)
        cases = [_case("daily", probe_id="p0"), _case("stock_basic", probe_id="p1", nested={})]
        stopped_transport = FakeTransport(
            FakeResponse(status_code=401, payload={"code": 401, "msg": SYNTHETIC_TOKEN})
        )
        first = audit.run_audit(
            cases=cases,
            transport=stopped_transport,
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True, True]),
            clock=FakeClock(),
            stop_threshold=1,
        )
        self.assertEqual("not_run", first["cases"][1]["classification"])
        resumed_transport = FakeTransport(FakeResponse(payload=_success_payload()))
        second = audit.run_audit(
            cases=cases,
            resume=first,
            transport=resumed_transport,
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        self.assertEqual(1, len(resumed_transport.calls))
        self.assertEqual("auth_denied", second["cases"][0]["classification"])
        self.assertEqual("pass_nonempty", second["cases"][1]["classification"])

    def test_resume_rejects_inventory_or_manifest_hash_mismatch_and_case_identity_drift(self):
        audit = _audit_module(self)
        base = audit.run_audit(
            cases=[_case()],
            transport=FakeTransport(),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        for field in ("inventory_sha256", "probe_manifest_sha256"):
            with self.subTest(field=field):
                changed = copy.deepcopy(base)
                changed[field] = "0" * 64
                with self.assertRaises(ValueError):
                    audit.run_audit(
                        cases=[_case()],
                        resume=changed,
                        transport=FakeTransport(),
                        token_loader=lambda: SYNTHETIC_TOKEN,
                        budget=SequenceBudget([True]),
                        clock=FakeClock(),
                    )
        changed = copy.deepcopy(base)
        changed["cases"][0]["probe_id"] = "changed"
        with self.assertRaises(ValueError):
            audit.run_audit(
                cases=[_case()],
                resume=changed,
                transport=FakeTransport(),
                token_loader=lambda: SYNTHETIC_TOKEN,
                budget=SequenceBudget([True]),
                clock=FakeClock(),
            )

    def test_atomic_checkpoint_is_mode_0600_and_leaves_no_temp_file(self):
        audit = _audit_module(self)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            with patch.object(audit, "APPROVED_OUTPUT_ROOT", Path(directory)), patch.object(
                audit, "APPROVED_SHARED_AUDIT_ROOT", Path(directory) / "shared"
            ):
                audit.run_audit(
                    cases=[_case()],
                    output=output,
                    transport=FakeTransport(),
                    token_loader=lambda: SYNTHETIC_TOKEN,
                    budget=SequenceBudget([True]),
                    clock=FakeClock(),
                )
            self.assertTrue(output.is_file())
            self.assertEqual(0o600, stat.S_IMODE(output.stat().st_mode))
            self.assertEqual([], list(Path(directory).glob("*.tmp*")))
            audit.validate_receipt_safety(json.loads(output.read_text(encoding="utf-8")))

    def test_atomic_checkpoint_stays_on_open_directory_when_parent_is_replaced(self):
        audit = _audit_module(self)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            approved = root / "approved"
            approved.mkdir()
            moved = root / "moved"
            outside = root / "outside"
            outside.mkdir()
            target = approved / "receipt.json"
            real_replace = os.replace

            def swap_parent(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
                temp_name = Path(src).name
                os.rename(approved, moved)
                os.rename(moved / temp_name, outside / temp_name)
                approved.symlink_to(outside, target_is_directory=True)
                return real_replace(
                    src,
                    dst,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with patch.object(audit, "APPROVED_OUTPUT_ROOT", approved), patch.object(
                audit, "APPROVED_SHARED_AUDIT_ROOT", root / "shared"
            ), patch.object(audit.os, "replace", side_effect=swap_parent):
                with self.assertRaises(FileNotFoundError):
                    audit.atomic_write_json(target, {"x": 1})
            self.assertFalse((outside / "receipt.json").exists())

    def test_atomic_checkpoint_rejects_intermediate_directory_symlink_toctou(self):
        audit = _audit_module(self)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            approved = root / "approved"
            intermediate = approved / "nested"
            target_parent = intermediate / "leaf"
            target_parent.mkdir(parents=True)
            outside = root / "outside"
            outside_leaf = outside / "leaf"
            outside_leaf.mkdir(parents=True)
            moved = root / "moved"
            target = target_parent / "receipt.json"
            real_open = audit.os.open
            swapped = False
            external_fd_opened = False

            def race_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped, external_fd_opened
                if not swapped:
                    os.rename(intermediate, moved)
                    intermediate.symlink_to(outside, target_is_directory=True)
                    swapped = True
                fd = real_open(path, flags, mode, dir_fd=dir_fd)
                if (
                    swapped
                    and dir_fd is None
                    and Path(path) == target_parent
                    and target_parent.resolve() == outside_leaf.resolve()
                ):
                    external_fd_opened = True
                return fd

            with patch.object(audit, "APPROVED_OUTPUT_ROOT", approved), patch.object(
                audit, "APPROVED_SHARED_AUDIT_ROOT", root / "shared"
            ), patch.object(audit.os, "open", side_effect=race_open):
                with self.assertRaises((OSError, ValueError)):
                    audit.atomic_write_json(target, {"x": 1})
            self.assertFalse(external_fd_opened)
            self.assertFalse((outside_leaf / "receipt.json").exists())

    def test_consecutive_auth_or_rate_limit_threshold_stops_and_marks_stable_not_run(self):
        audit = _audit_module(self)
        transport = FakeTransport(FakeResponse(status_code=401, payload={"code": 401, "msg": SYNTHETIC_TOKEN}))
        cases = [_case("daily", probe_id=f"p{index}") for index in range(4)]
        receipt = audit.run_audit(
            cases=cases,
            transport=transport,
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True] * 4),
            clock=FakeClock(),
            stop_threshold=2,
        )
        self.assertEqual(2, len(transport.calls))
        self.assertEqual(["auth_denied", "auth_denied", "not_run", "not_run"], [case["classification"] for case in receipt["cases"]])
        self.assertEqual({"auth_threshold"}, {case["not_run_reason"] for case in receipt["cases"][2:]})

    def test_budget_exhaustion_waits_without_busy_loop(self):
        audit = _audit_module(self)
        clock = FakeClock()
        budget = SequenceBudget([False, True], wait_seconds=7.0)
        transport = FakeTransport()
        receipt = audit.run_audit(
            cases=[_case()],
            transport=transport,
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=budget,
            clock=clock,
            sleeper=clock.sleep,
        )
        self.assertEqual([7.0], clock.sleeps)
        self.assertEqual(2, budget.acquires)
        self.assertEqual(1, len(transport.calls))
        self.assertEqual("pass_nonempty", receipt["cases"][0]["classification"])

    def test_filter_duplicate_and_pagination_checks_are_recorded_and_fail_closed(self):
        audit = _audit_module(self)
        rows = [
            ["000001.SZ", "20250929", 10.0],
            ["000001.SZ", "20250929", 10.1],
        ]
        case = _case(
            nested={"ts_code": "000001.SZ", "trade_date": "20250930", "limit": 1},
            expected_fields=["ts_code", "trade_date", "close"],
            duplicate_key_fields=["ts_code", "trade_date"],
        )
        result = audit.run_audit(
            cases=[case],
            transport=FakeTransport(FakeResponse(payload=_success_payload(rows=rows, total=3))),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )
        item = result["cases"][0]
        self.assertEqual("semantic_fail", item["classification"])
        self.assertEqual("fail", item["filter_checks"]["status"])
        self.assertEqual("fail", item["duplicate_key_checks"]["status"])
        self.assertEqual("truncated", item["pagination_truncation_checks"]["status"])

    def test_recursive_secret_token_and_url_validator_rejects_fail_closed(self):
        audit = _audit_module(self)
        for bad in (
            {"token": SYNTHETIC_TOKEN},
            {"nested": [{"secret": "synthetic"}]},
            {"params": {"host_override": "https://other.example"}},
            {"params": {"url": "https://user:password@example.invalid"}},
            {"nested": [SYNTHETIC_TOKEN]},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    audit.validate_receipt_safety(bad)

    def test_unexpected_transport_errors_are_sanitized_and_builtin_timeout_is_classified(self):
        audit = _audit_module(self)
        for raised, expected in (
            (RuntimeError(SYNTHETIC_TOKEN), "provider_error"),
            (TimeoutError(SYNTHETIC_TOKEN), "timeout"),
        ):
            with self.subTest(expected=expected):
                result = audit.run_audit(
                    cases=[_case()],
                    transport=FakeTransport(raised),
                    token_loader=lambda: SYNTHETIC_TOKEN,
                    budget=SequenceBudget([True]),
                    clock=FakeClock(),
                )
                self.assertEqual(expected, result["cases"][0]["classification"])
                self.assertNotIn(SYNTHETIC_TOKEN, json.dumps(result, ensure_ascii=False))

    def test_output_path_rejects_outside_targets_and_symlink_components(self):
        audit = _audit_module(self)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "outputs").mkdir()
            with patch.object(audit, "APPROVED_OUTPUT_ROOT", root / "outputs"), patch.object(
                audit, "APPROVED_SHARED_AUDIT_ROOT", root / "shared"
            ):
                self.assertEqual(root / "outputs" / "audit.json", audit.validate_output_path(root / "outputs" / "audit.json", repo_root=root / "attacker"))
                with self.assertRaises(ValueError):
                    audit.validate_output_path(root / "outside.json", repo_root=root)
                (root / "outputs" / "link").symlink_to(root / "outside", target_is_directory=True)
                with self.assertRaises(ValueError):
                    audit.validate_output_path(root / "outputs" / "link" / "audit.json", repo_root=root)
                (root / "outputs" / "audit-link.json").symlink_to(root / "outputs" / "real.json")
                with self.assertRaises(ValueError):
                    audit.validate_output_path(root / "outputs" / "audit-link.json", repo_root=root)

    def test_receipt_timestamps_are_timezone_aware_ordered_and_nested(self):
        audit = _audit_module(self)
        receipt = audit.run_audit(
            cases=[_case()],
            transport=FakeTransport(),
            token_loader=lambda: SYNTHETIC_TOKEN,
            budget=SequenceBudget([True]),
            clock=FakeClock(),
        )

        def resigned(changed):
            candidate = copy.deepcopy(receipt)
            changed(candidate)
            unsigned = copy.deepcopy(candidate)
            unsigned.pop("receipt_sha256")
            candidate["receipt_sha256"] = audit._sha256(unsigned)
            return candidate

        invalid = [
            resigned(lambda item: item.update({"started": "not-a-timestamp"})),
            resigned(
                lambda item: item.update(
                    {"started": "2026-09-22T12:00:02+08:00", "ended": "2026-09-22T12:00:01+08:00"}
                )
            ),
            resigned(lambda item: item["cases"][0].update({"start": "2026-09-22T12:00:00"})),
            resigned(
                lambda item: item["cases"][0].update(
                    {"start": "2026-09-22T12:00:01+08:00", "end": "2026-09-22T12:00:00+08:00"}
                )
            ),
            resigned(
                lambda item: item["cases"][0].update(
                    {"end": "2026-09-22T12:00:02+08:00"}
                )
            ),
        ]
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    audit.validate_receipt(candidate)

    def test_cli_requires_live_and_status_stdout_contains_counts_only(self):
        audit = _audit_module(self)
        from ym_stock_data.__main__ import main

        with patch("ym_stock_data.__main__.run_stocktoday_audit", side_effect=AssertionError("network must not run"), create=True):
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(2, main(["stocktoday", "audit", "--output", "outputs/audit.json"]))
            self.assertIn("--live", output.getvalue())

        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(0, main(["stocktoday", "inventory", "--json"]))
            inventory_stdout = output.getvalue()
        inventory_report = json.loads(inventory_stdout)
        self.assertIn("method_count", inventory_report)
        self.assertNotIn("methods", inventory_report)

        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(
                0,
                main(["stocktoday", "catalog", "ths_hot", "--json"]),
            )
        catalog_report = json.loads(output.getvalue())
        self.assertEqual(1, catalog_report["method_count"])
        self.assertEqual("ths_hot", catalog_report["methods"][0]["name"])
        self.assertIn("market", catalog_report["methods"][0]["allowed_params"])
        self.assertEqual("热股", catalog_report["methods"][0]["example"]["market"])

        with tempfile.TemporaryDirectory() as directory:
            receipt_path = Path(directory) / "receipt.json"
            receipt = audit.run_audit(
                cases=[_case()],
                transport=FakeTransport(),
                token_loader=lambda: SYNTHETIC_TOKEN,
                budget=SequenceBudget([True]),
                clock=FakeClock(),
            )
            with patch.object(audit, "APPROVED_OUTPUT_ROOT", receipt_path.parent), patch.object(
                audit, "APPROVED_SHARED_AUDIT_ROOT", receipt_path.parent / "shared"
            ):
                audit.atomic_write_json(receipt_path, receipt)
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(0, main(["stocktoday", "audit-status", str(receipt_path)]))
            status_stdout = output.getvalue()
            self.assertNotIn("sanitized_params", status_stdout)
            self.assertNotIn(SYNTHETIC_TOKEN, status_stdout)
            status = json.loads(status_stdout)
            self.assertIn("counts", status)
            self.assertIn("classifications", status)
            self.assertNotIn("cases", status)

    def test_wheel_install_smoke_loads_manifest_without_source_tree(self):
        _audit_module(self)
        uv = None
        for candidate in (
            os.environ.get("YM_DATA_UV_BIN"),
            "/opt/homebrew/bin/uv",
            shutil.which("uv"),
        ):
            if not candidate or not Path(candidate).is_file():
                continue
            try:
                probe = subprocess.run([candidate, "--version"], capture_output=True, text=True)
            except OSError:
                continue
            if probe.returncode == 0:
                uv = candidate
                break
        if uv is None:
            self.skipTest("uv is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            wheel_dir = root / "wheel"
            target = root / "target"
            outside = root / "outside"
            wheel_dir.mkdir()
            target.mkdir()
            outside.mkdir()
            shutil.copytree(
                ROOT,
                source,
                ignore=shutil.ignore_patterns("__pycache__", "build", "*.egg-info"),
            )
            build = subprocess.run(
                [uv, "build", "--wheel", "--out-dir", str(wheel_dir)],
                cwd=source,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, build.returncode, build.stdout + build.stderr)
            wheels = sorted(wheel_dir.glob("*.whl"))
            self.assertEqual(1, len(wheels), build.stdout + build.stderr)
            install = subprocess.run(
                [uv, "pip", "install", "--python", sys.executable, "--no-deps", "--target", str(target), str(wheels[0])],
                cwd=outside,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, install.returncode, install.stdout + install.stderr)
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(target)
            smoke = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from ym_stock_data.stocktoday_audit import load_probes; from ym_stock_data.provider_smoke_v3 import load_probe_manifest, RUNNER_VERSION; p=load_probes(); m=load_probe_manifest(); assert len(p['methods']) == 245; assert p['account_diagnostic']['name'] == 'token_info'; assert len(m['providers']) == 53; assert RUNNER_VERSION.startswith('provider-smoke-runner.v3.'); print('wheel-smoke-ok')",
                ],
                cwd=outside,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, smoke.returncode, smoke.stdout + smoke.stderr)
            self.assertEqual("wheel-smoke-ok\n", smoke.stdout)


if __name__ == "__main__":
    unittest.main()
