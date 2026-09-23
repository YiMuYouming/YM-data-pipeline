import copy
import io
import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from urllib.parse import quote
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SOURCE_URL = "https://stocktoday.cn/api/tools/methods"


def _artifact_snapshot(path):
    path = Path(path)
    if not path.exists():
        return None
    files = []
    for item in sorted(path.rglob("*")):
        if item.is_file():
            files.append(
                (
                    str(item.relative_to(path)),
                    item.stat().st_mtime_ns,
                    hashlib.sha256(item.read_bytes()).hexdigest(),
                )
            )
    return tuple(files)


def _inventory_module(testcase):
    try:
        return importlib.import_module("ym_stock_data.providers.stocktoday_inventory")
    except ModuleNotFoundError as exc:
        if exc.name == "ym_stock_data.providers.stocktoday_inventory":
            testcase.fail("stocktoday_inventory module is not implemented yet")
        raise


def _refresh_module(testcase):
    script = ROOT / "scripts" / "refresh_stocktoday_inventory.py"
    if not script.is_file():
        testcase.fail("refresh_stocktoday_inventory.py is not implemented yet")
    spec = importlib.util.spec_from_file_location("refresh_stocktoday_inventory", script)
    if spec is None or spec.loader is None:
        testcase.fail("cannot load refresh_stocktoday_inventory.py")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as exc:
        if exc.name == "ym_stock_data.providers.stocktoday_inventory":
            testcase.fail("stocktoday_inventory module is not implemented yet")
        raise
    return module


def _raw_payload():
    return {
        "code": 0,
        "total_apis": 2,
        "level1_order": ["category-b", "category-a"],
        "data": {
            "category-b": {
                "subcategory-b": [
                    {
                        "desc": "Z method",
                        "example": {},
                        "has_example": False,
                        "name": "zeta",
                        "params": [{"default": "", "name": "ts_code"}],
                    }
                ]
            },
            "category-a": {
                "subcategory-a": [
                    {
                        "desc": "A method",
                        "example": {"ts_code": "600519.SH"},
                        "has_example": True,
                        "name": "alpha",
                        "params": [
                            {"default": "", "name": "ts_code"},
                            {"default": "", "name": "fields"},
                        ],
                    }
                ]
            },
        },
    }


class StockTodayInventoryTests(unittest.TestCase):
    def test_frozen_inventory_is_complete_and_unique(self):
        inventory_module = _inventory_module(self)
        inventory = inventory_module.load_inventory()
        methods = inventory["methods"]
        names = [item["name"] for item in methods]

        self.assertEqual(245, len(names))
        self.assertEqual(245, len(set(names)))
        self.assertEqual(sorted(names), names)
        source_orders = [item["source_order"] for item in methods]
        self.assertEqual(list(range(245)), sorted(source_orders))
        self.assertEqual(0, next(item["source_order"] for item in methods if item["name"] == "margin"))
        self.assertEqual(SOURCE_URL, inventory["source_url"])
        self.assertEqual("3.0", inventory["schema_version"])
        self.assertRegex(inventory["payload_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("token_info", names)
        self.assertEqual(209, sum(item["has_example"] for item in methods))
        self.assertEqual(36, sum(not item["has_example"] for item in methods))

        for item in methods:
            self.assertEqual(item["has_example"], bool(item["example"]))
            self.assertIsInstance(item["params"], list)
            self.assertEqual(
                len(item["params"]),
                len({parameter["name"] for parameter in item["params"]}),
            )
            for parameter in item["params"]:
                self.assertIn("default", parameter)

    def test_inventory_rejects_duplicate_or_unsafe_method_names(self):
        inventory_module = _inventory_module(self)
        with self.assertRaises(ValueError):
            inventory_module.validate_inventory(
                {"methods": [{"name": "daily"}, {"name": "daily"}]}
            )
        with self.assertRaises(ValueError):
            inventory_module.validate_inventory({"methods": [{"name": "../daily"}]})
        with self.assertRaises(ValueError):
            inventory_module.validate_inventory({"methods": [{"name": "token_info"}]})

    def test_inventory_rejects_invalid_or_non_bijective_source_order(self):
        inventory_module = _inventory_module(self)

        def method(name, source_order):
            return {
                "name": name,
                "source_order": source_order,
                "category": "category",
                "subcategory": "subcategory",
                "desc": name,
                "example": {},
                "has_example": False,
                "params": [],
            }

        for invalid_order in (True, -1, 1.0):
            with self.subTest(source_order=invalid_order):
                with self.assertRaisesRegex(ValueError, "source_order"):
                    inventory_module.validate_inventory(
                        {"methods": [method("daily", invalid_order)]}
                    )
        with self.assertRaisesRegex(ValueError, "source_order"):
            inventory_module.validate_inventory(
                {"methods": [method("alpha", 0), method("beta", 0)]}
            )
        with self.assertRaisesRegex(ValueError, "source_order"):
            inventory_module.validate_inventory({"methods": [method("daily", 1)]})

    def test_method_names_and_endpoint_params_are_consistent_with_api_params(self):
        inventory_module = _inventory_module(self)
        inventory = inventory_module.load_inventory()
        expected_methods = {item["name"]: item for item in inventory["methods"]}
        self.assertEqual(expected_methods, inventory_module.method_map())

        from ym_stock_data.providers.stocktoday_catalog import API_PARAMS

        self.assertEqual(set(expected_methods), set(API_PARAMS))
        for name, item in expected_methods.items():
            endpoint_params = {
                parameter["name"] for parameter in item["params"] if parameter["name"] != "fields"
            }
            self.assertTrue(endpoint_params <= API_PARAMS[name], name)

        catalog = importlib.import_module("ym_stock_data.providers.stocktoday_catalog")
        self.assertEqual(set(expected_methods), set(catalog.PARAMETER_AUDIT))
        self.assertTrue(
            all(not item["endpoint_only"] for item in catalog.PARAMETER_AUDIT.values())
        )
        self.assertTrue(
            any(item["catalog_only"] for item in catalog.PARAMETER_AUDIT.values())
        )
        self.assertEqual(
            {
                "method_count": 245,
                "methods_with_any_difference": 116,
                "methods_with_documented_fields": 59,
                "web_only_non_fields": 0,
                "catalog_only_parameter_count": 167,
            },
            catalog.PARAMETER_AUDIT_SUMMARY,
        )

    def test_refresh_rejects_malformed_upstream_envelopes(self):
        refresh = _refresh_module(self)

        cases = []

        def add_case(label, mutate):
            payload = copy.deepcopy(_raw_payload())
            mutate(payload)
            cases.append((label, payload))

        add_case("unknown top-level field", lambda payload: payload.update({"extra": 1}))
        add_case("missing top-level field", lambda payload: payload.pop("code"))
        add_case("boolean code", lambda payload: payload.update({"code": True}))
        add_case("nonzero code", lambda payload: payload.update({"code": 1}))
        add_case("boolean total", lambda payload: payload.update({"total_apis": False}))
        add_case("negative total", lambda payload: payload.update({"total_apis": -1}))
        add_case("total mismatch", lambda payload: payload.update({"total_apis": 1}))
        add_case("missing level1 order", lambda payload: payload.pop("level1_order"))
        add_case(
            "duplicate level1 order",
            lambda payload: payload.update({"level1_order": ["category-b", "category-b"]}),
        )
        add_case(
            "inconsistent level1 order",
            lambda payload: payload.update({"level1_order": ["category-b", "other"]}),
        )
        add_case(
            "unknown method field",
            lambda payload: payload["data"]["category-a"]["subcategory-a"][0].update(
                {"extra": 1}
            ),
        )
        add_case(
            "missing method field",
            lambda payload: payload["data"]["category-a"]["subcategory-a"][0].pop("desc"),
        )
        add_case(
            "unknown parameter field",
            lambda payload: payload["data"]["category-a"]["subcategory-a"][0]["params"][0].update(
                {"extra": 1}
            ),
        )
        add_case(
            "missing parameter field",
            lambda payload: payload["data"]["category-a"]["subcategory-a"][0]["params"][0].pop(
                "default"
            ),
        )
        add_case(
            "invalid category structure",
            lambda payload: payload["data"].update({"category-b": []}),
        )
        add_case(
            "invalid subcategory structure",
            lambda payload: payload["data"]["category-a"].update({"subcategory-a": {}}),
        )

        for label, payload in cases:
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    refresh.flatten_methods(payload)

    def test_fetch_projects_known_level1_order_into_strict_envelope(self):
        refresh = _refresh_module(self)
        response = Mock(status_code=200)
        response.json.return_value = {
            **_raw_payload(),
            "level1_order": ["category-b", "category-a"],
        }
        with patch.object(refresh.requests, "get", return_value=response):
            self.assertEqual(_raw_payload(), refresh.fetch_payload())

        response.json.return_value = {**_raw_payload(), "unexpected": []}
        with patch.object(refresh.requests, "get", return_value=response):
            with self.assertRaises(ValueError):
                refresh.fetch_payload()

    def test_inventory_schema_is_exact_and_credential_free(self):
        inventory_module = _inventory_module(self)
        refresh = _refresh_module(self)
        inventory = inventory_module.load_inventory()
        self.assertEqual(
            {
                "schema_version",
                "source_url",
                "fetched_at",
                "payload_sha256",
                "methods",
            },
            set(inventory),
        )
        for method in inventory["methods"]:
            self.assertEqual(
                {
                    "name",
                    "source_order",
                    "category",
                    "subcategory",
                    "desc",
                    "example",
                    "has_example",
                    "params",
                },
                set(method),
            )
            for parameter in method["params"]:
                self.assertEqual({"name", "default"}, set(parameter))

        def rehash(payload):
            payload["payload_sha256"] = inventory_module.method_content_sha256(
                payload["methods"]
            )
            return payload

        cases = []
        payload = copy.deepcopy(inventory)
        payload["unexpected"] = "field"
        cases.append(("top-level unknown", payload))
        payload = copy.deepcopy(inventory)
        payload["methods"][0]["unexpected"] = "field"
        cases.append(("method unknown", rehash(payload)))
        payload = copy.deepcopy(inventory)
        payload["methods"][0]["params"][0]["unexpected"] = "field"
        cases.append(("parameter unknown", rehash(payload)))
        payload = copy.deepcopy(inventory)
        payload["methods"][0]["example"] = {"API_Key": "SYNTHETIC_TOKEN_VALUE_1234"}
        payload["methods"][0]["has_example"] = True
        cases.append(("sensitive example key", rehash(payload)))
        payload = copy.deepcopy(inventory)
        payload["methods"][0]["params"][0]["default"] = {
            "credential": "SYNTHETIC_TOKEN_VALUE_1234"
        }
        cases.append(("sensitive parameter default key", rehash(payload)))
        payload = copy.deepcopy(inventory)
        payload["methods"][0]["example"] = {
            "note": "sk-testSyntheticCredential_1234567890"
        }
        payload["methods"][0]["has_example"] = True
        cases.append(("token-shaped example string", rehash(payload)))

        for label, payload in cases:
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    inventory_module.validate_inventory(payload)

        safe = copy.deepcopy(inventory)
        safe["methods"][0]["example"] = {
            "trade_date": "20260922",
            "ts_code": "600519.SH",
            "desc": "正常中文描述",
        }
        safe["methods"][0]["has_example"] = True
        safe["methods"][0]["params"][0]["default"] = "20260922"
        self.assertIs(inventory_module.validate_inventory(rehash(safe)), safe)

    def test_sensitive_composite_keys_and_parameter_names_are_rejected(self):
        inventory_module = _inventory_module(self)
        inventory = inventory_module.load_inventory()

        def rehash(payload):
            payload["payload_sha256"] = inventory_module.method_content_sha256(
                payload["methods"]
            )
            return payload

        for key in (
            "client_secret",
            "refreshToken",
            "token_value",
            "accessToken",
            "idToken",
            "clientSecret",
            "apiKey",
            "privateKey",
            "authHeader",
        ):
            payload = copy.deepcopy(inventory)
            payload["methods"][0]["example"] = {"nested": {key: "synthetic"}}
            payload["methods"][0]["has_example"] = True
            with self.subTest(example_key=key):
                with self.assertRaises(ValueError):
                    inventory_module.validate_inventory(rehash(payload))

        for parameter_name in ("token", "refresh_token"):
            payload = copy.deepcopy(inventory)
            payload["methods"][0]["params"][0] = {
                "name": parameter_name,
                "default": "",
            }
            with self.subTest(parameter_name=parameter_name):
                with self.assertRaises(ValueError):
                    inventory_module.validate_inventory(rehash(payload))

        safe = copy.deepcopy(inventory)
        safe["methods"][0]["example"] = {
            "secretary": "Alice",
            "secretary_name": "Alice",
            "tokenization": "enabled",
        }
        safe["methods"][0]["has_example"] = True
        safe["methods"][0]["params"][0] = {
            "name": "external_id",
            "default": "EXT-2026-ALPHA-00000000000000000001",
        }
        self.assertIs(inventory_module.validate_inventory(rehash(safe)), safe)

    def test_token_shaped_values_use_explicit_credential_rules(self):
        inventory_module = _inventory_module(self)
        inventory = inventory_module.load_inventory()

        def rehash(payload):
            payload["payload_sha256"] = inventory_module.method_content_sha256(
                payload["methods"]
            )
            return payload

        safe = copy.deepcopy(inventory)
        safe["methods"][0]["example"] = {
            "url": "https://example.com/public/path?ref=2026",
            "uuid": "550e8400-e29b-41d4-a716-446655440000",
            "external_id": "EXT-2026-ALPHA-00000000000000000001",
        }
        safe["methods"][0]["has_example"] = True
        self.assertIs(inventory_module.validate_inventory(rehash(safe)), safe)

        credential_values = (
            "Bearer SYNTHETIC_CREDENTIAL_VALUE",
            "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0In0.SYNTHETIC_SIGNATURE",
            "sk-testSyntheticCredential_1234567890",
            "pk-testSyntheticCredential_1234567890",
            "ghp_testSyntheticCredential_1234567890",
            "github_pat_testSyntheticCredential_1234567890",
            "xoxb-testSyntheticCredential_1234567890",
            "AKIA_SYNTHETIC_EXAMPLE_123456",
            "AIzaSyntheticExampleKey_123456",
            "aB3dE5gH7jK9mN1pQ3rS5tU7vW9xY1zA",
        )
        for value in credential_values:
            payload = copy.deepcopy(inventory)
            payload["methods"][0]["example"] = {"opaque": value}
            payload["methods"][0]["has_example"] = True
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    inventory_module.validate_inventory(rehash(payload))
                self.assertNotIn(value, str(caught.exception))

    def test_urls_reject_userinfo_and_query_credentials_without_echo(self):
        inventory_module = _inventory_module(self)
        inventory = inventory_module.load_inventory()

        def rehash(payload):
            payload["payload_sha256"] = inventory_module.method_content_sha256(
                payload["methods"]
            )
            return payload

        for url in (
            "https://example.com/public?ref=2026&lang=en#section-2",
            "http://example.com/public?code=abc#top",
        ):
            payload = copy.deepcopy(inventory)
            payload["methods"][0]["example"] = {"url": url}
            payload["methods"][0]["has_example"] = True
            with self.subTest(safe_url=url):
                self.assertIs(inventory_module.validate_inventory(rehash(payload)), payload)

        bad_urls = (
            "https://synthetic-user@example.com/public",
            "https://synthetic-user:synthetic-password@example.com/public",
            "https://example.com/public?token=",
            "https://example.com/public?api_key=",
            "https://example.com/public?access_token=",
            "https://example.com/public#access_token=",
            "https://example.com/public?q=Bearer%20SYNTHETIC_CREDENTIAL_VALUE",
            "https://example.com/public?q=eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0In0.SYNTHETIC_SIGNATURE",
            "https://example.com/public?sk-testSyntheticCredential_1234567890=ok",
            "https://example.com/public#Bearer%20SYNTHETIC_CREDENTIAL_VALUE",
            "https://example.com/public#eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0In0.SYNTHETIC_SIGNATURE",
            "https://example.com/public#aB3dE5gH7jK9mN1pQ3rS5tU7vW9xY1zA",
        )
        for url in bad_urls:
            payload = copy.deepcopy(inventory)
            payload["methods"][0]["example"] = {"url": url}
            payload["methods"][0]["has_example"] = True
            with self.subTest(bad_url=url):
                with self.assertRaises(ValueError) as caught:
                    inventory_module.validate_inventory(rehash(payload))
                self.assertNotIn(url, str(caught.exception))
                self.assertNotIn("SYNTHETIC", str(caught.exception))

    def test_untrusted_example_keys_never_enter_credential_errors(self):
        inventory_module = _inventory_module(self)
        inventory = inventory_module.load_inventory()

        def rehash(payload):
            payload["payload_sha256"] = inventory_module.method_content_sha256(
                payload["methods"]
            )
            return payload

        cases = (
            (
                "aB3dE5gH7jK9mN1pQ3rS5tU7vW9xY1zA",
                "Bearer SYNTHETIC_CREDENTIAL_VALUE",
            ),
            ("ordinary_field", "Bearer SYNTHETIC_CREDENTIAL_VALUE"),
        )
        for key, value in cases:
            payload = copy.deepcopy(inventory)
            payload["methods"][0]["example"] = {"nested": {key: value}}
            payload["methods"][0]["has_example"] = True
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as caught:
                    inventory_module.validate_inventory(rehash(payload))
                message = str(caught.exception)
                self.assertNotIn(key, message)
                self.assertNotIn(value, message)

    def test_url_query_values_recurse_and_depth_is_bounded(self):
        inventory_module = _inventory_module(self)
        inventory = inventory_module.load_inventory()

        def rehash(payload):
            payload["payload_sha256"] = inventory_module.method_content_sha256(
                payload["methods"]
            )
            return payload

        def validate_url(url):
            payload = copy.deepcopy(inventory)
            payload["methods"][0]["example"] = {"redirect": url}
            payload["methods"][0]["has_example"] = True
            return inventory_module.validate_inventory(rehash(payload))

        public_id = "EXT-2026-ALPHA-00000000000000000001"
        for url in (
            f"https://example.com/public?id={public_id}",
            f"https://example.com/public#id={public_id}",
            f"https://example.com/public?redirect=https://public.example/path?id={public_id}",
        ):
            with self.subTest(safe_url=url):
                try:
                    validated = validate_url(url)
                except ValueError as exc:
                    self.fail(f"public URL should remain valid: {exc}")
                self.assertIsInstance(validated, dict)

        bad_urls = (
            "https://example.com/public?redirect=https://user:pass@example.com/path",
            "https://example.com/public?redirect=https://host/?token=",
            "https://example.com/public?redirect=https://host/?q=eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0In0.SYNTHETIC_SIGNATURE",
        )
        for url in bad_urls:
            with self.subTest(bad_url=url):
                with self.assertRaises(ValueError) as caught:
                    validate_url(url)
                self.assertNotIn(url, str(caught.exception))
                self.assertNotIn("SYNTHETIC", str(caught.exception))

        nested = "https://public.example/path?id=" + public_id
        for _ in range(9):
            nested = "https://public.example/path?redirect=" + quote(nested, safe="")
        with self.assertRaises(ValueError) as caught:
            validate_url(nested)
        self.assertNotIn(nested, str(caught.exception))
        self.assertNotIn(public_id, str(caught.exception))

    def test_wheel_smoke_does_not_touch_root_build_artifacts(self):
        uv = None
        for candidate in [shutil.which("uv"), "/opt/homebrew/bin/uv", "/usr/local/bin/uv"]:
            if not candidate:
                continue
            try:
                probe = subprocess.run(
                    [candidate, "--version"], capture_output=True, text=True
                )
            except OSError:
                continue
            if probe.returncode == 0:
                uv = candidate
                break
        self.assertIsNotNone(uv)
        artifact_paths = [ROOT / "build", ROOT / "ym_stock_data.egg-info"]
        before = {path: _artifact_snapshot(path) for path in artifact_paths}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel_dir = root / "wheel"
            target = root / "target"
            outside = root / "outside"
            source = root / "source"
            for path in (wheel_dir, target, outside, source):
                path.mkdir()
            shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
            shutil.copytree(
                ROOT / "ym_stock_data",
                source / "ym_stock_data",
                ignore=shutil.ignore_patterns("__pycache__", "build", "*.egg-info"),
            )
            self.assertFalse((source / "build").exists())
            self.assertEqual([], list(source.glob("*.egg-info")))
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
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    sys.executable,
                    "--no-deps",
                    "--target",
                    str(target),
                    str(wheels[0]),
                ],
                cwd=outside,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, install.returncode, install.stdout + install.stderr)

            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(target)
            smoke_code = textwrap.dedent(
                """
                import importlib.util
                import os
                from pathlib import Path

                target = Path(os.environ["PYTHONPATH"]).resolve()
                module_path = target / "ym_stock_data/providers/stocktoday_inventory.py"
                inventory_path = target / "ym_stock_data/providers/stocktoday_methods.v3.json"
                assert module_path.is_file()
                assert inventory_path.is_file()
                spec = importlib.util.spec_from_file_location("installed_inventory", module_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                assert Path(module.__file__).resolve().is_relative_to(target)
                assert Path(module.INVENTORY_PATH).resolve().is_relative_to(target)
                assert len(module.load_inventory()["methods"]) == 245
                """
            )
            smoke = subprocess.run(
                [sys.executable, "-c", smoke_code],
                cwd=outside,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, smoke.returncode, smoke.stdout + smoke.stderr)
        after = {path: _artifact_snapshot(path) for path in artifact_paths}
        self.assertEqual(before, after)

    def test_api_params_values_have_the_frozen_canonical_hash(self):
        from ym_stock_data.providers.stocktoday_catalog import API_PARAMS

        canonical = json.dumps(
            {key: sorted(value) for key, value in sorted(API_PARAMS.items())},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(
            "5305b0e0af502c658c53417acda289b20f8e08a3ee2d7181388d1625c2104fc4",
            hashlib.sha256(canonical).hexdigest(),
        )

    def test_package_import_fails_closed_when_inventory_is_missing_or_corrupt(self):
        for state in ("missing", "corrupt"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                package_root = root / "ym_stock_data"
                shutil.copytree(ROOT / "ym_stock_data", package_root)
                inventory_path = package_root / "providers" / "stocktoday_methods.v3.json"
                if state == "missing":
                    inventory_path.unlink()
                else:
                    inventory_path.write_text("{", encoding="utf-8")

                environment = os.environ.copy()
                environment["PYTHONPATH"] = str(root)
                result = subprocess.run(
                    [sys.executable, "-c", "import ym_stock_data"],
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(0, result.returncode)

    def test_refresh_bootstraps_without_a_frozen_file(self):
        refresh_source = ROOT / "scripts" / "refresh_stocktoday_inventory.py"
        inventory_source = ROOT / "ym_stock_data" / "providers" / "stocktoday_inventory.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "ym_stock_data" / "providers").mkdir(parents=True)
            shutil.copy2(refresh_source, root / "scripts" / refresh_source.name)
            shutil.copy2(
                inventory_source,
                root / "ym_stock_data" / "providers" / inventory_source.name,
            )
            spec = importlib.util.spec_from_file_location(
                "stocktoday_refresh_bootstrap_fixture",
                root / "scripts" / refresh_source.name,
            )
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            refresh = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(refresh)
            self.assertFalse(refresh.INVENTORY_PATH.exists())

            with patch.object(refresh, "fetch_payload", return_value=_raw_payload()):
                self.assertEqual(0, refresh.main([]))
            self.assertTrue(refresh.INVENTORY_PATH.is_file())
            loaded = refresh.load_inventory(refresh.INVENTORY_PATH)
            self.assertEqual(2, len(loaded["methods"]))

    def test_loader_rejects_inventory_hash_drift(self):
        inventory_module = _inventory_module(self)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.json"
            payload = {
                "schema_version": "3.0",
                "source_url": SOURCE_URL,
                "fetched_at": "2026-09-22T00:00:00+00:00",
                "payload_sha256": "0" * 64,
                "methods": [
                    {
                        "name": "daily",
                        "source_order": 0,
                        "category": "股票数据",
                        "subcategory": "日线行情",
                        "desc": "日线行情",
                        "example": {},
                        "has_example": False,
                        "params": [],
                    }
                ],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                inventory_module.load_inventory(path)

    def test_refresh_flattens_groups_preserves_fields_and_sorts_names(self):
        refresh = _refresh_module(self)
        methods = refresh.flatten_methods(_raw_payload())
        self.assertEqual(["alpha", "zeta"], [item["name"] for item in methods])
        self.assertEqual([1, 0], [item["source_order"] for item in methods])
        self.assertEqual("category-a", methods[0]["category"])
        self.assertEqual("subcategory-a", methods[0]["subcategory"])
        self.assertEqual([{"default": "", "name": "ts_code"}, {"default": "", "name": "fields"}], methods[0]["params"])
        self.assertEqual({}, methods[1]["example"])
        self.assertFalse(methods[1]["has_example"])

    def test_refresh_uses_fixed_https_request_and_no_custom_url(self):
        refresh = _refresh_module(self)
        response = Mock(status_code=200)
        response.json.return_value = _raw_payload()
        with patch.object(refresh.requests, "get", return_value=response) as get:
            self.assertEqual(_raw_payload(), refresh.fetch_payload())
        get.assert_called_once_with(
            SOURCE_URL,
            timeout=(4, 20),
            allow_redirects=False,
            headers={"Accept": "application/json"},
        )

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            refresh.main(["--url", "https://example.invalid/methods"])
        self.assertEqual(2, raised.exception.code)

    def test_check_reports_drift_without_writing_the_frozen_file(self):
        refresh = _refresh_module(self)
        inventory_module = _inventory_module(self)
        raw = _raw_payload()
        built = refresh.build_inventory(raw, fetched_at="2026-09-22T00:00:00+00:00")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.json"
            refresh.write_inventory(path, built)
            original = path.read_bytes()
            with patch.object(refresh, "INVENTORY_PATH", path), patch.object(
                refresh, "fetch_payload", return_value=raw
            ):
                self.assertEqual(0, refresh.main(["--check"]))
            self.assertEqual(original, path.read_bytes())

            drifted = json.loads(json.dumps(raw))
            drifted["data"]["category-a"]["subcategory-a"][0]["desc"] = "changed"
            with patch.object(refresh, "INVENTORY_PATH", path), patch.object(
                refresh, "fetch_payload", return_value=drifted
            ):
                self.assertEqual(1, refresh.main(["--check"]))
            self.assertEqual(original, path.read_bytes())
            inventory_module.load_inventory(path)

    def test_inventory_hash_is_sha256_of_canonical_sorted_methods(self):
        refresh = _refresh_module(self)
        built = refresh.build_inventory(_raw_payload(), fetched_at="2026-09-22T00:00:00+00:00")
        canonical = json.dumps(
            built["methods"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), built["payload_sha256"])


if __name__ == "__main__":
    unittest.main()
