import json
import multiprocessing
import stat
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

from ym_stock_data.providers.tdx_auth import (
    DEFAULT_RESOURCE_METADATA_URL,
    READ_SCOPE,
    FileCredentialStore,
    KeychainCredentialStore,
    LocalhostCallbackReceiver,
    TdxAuthExpired,
    TdxAuthMissing,
    TdxLoginCancelled,
    TdxLoginTimeout,
    TdxOwnedAuth,
    TdxScopeError,
    TdxStateMismatch,
    default_credential_store,
    generate_pkce,
    pkce_challenge,
)


RESOURCE_URL = "https://mcp.example.test/tdx"
RESOURCE_METADATA_URL = (
    "https://mcp.example.test/.well-known/oauth-protected-resource/tdx"
)
ISSUER = "https://auth.example.test"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/token"
REGISTRATION_ENDPOINT = f"{ISSUER}/register"


def token_bundle(*, expires_at_ms=None, refresh_token="REFRESH_SENTINEL"):
    return {
        "schema_version": "1",
        "client_id": "CLIENT_SENTINEL",
        "access_token": "ACCESS_SENTINEL",
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "scope": READ_SCOPE,
        "expires_at_ms": expires_at_ms
        if expires_at_ms is not None
        else int(time.time() * 1000) + 3_600_000,
        "issuer": ISSUER,
        "token_endpoint": TOKEN_ENDPOINT,
    }


class FakeKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, value):
        self.values[(service, username)] = value


class FakeCallback:
    redirect_uri = "http://127.0.0.1:43123/callback"

    def __init__(self, result=None, error=None):
        self.result = result or {}
        self.error = error

    def wait(self, timeout):
        if self.error:
            raise self.error
        return dict(self.result)


class FakeAuthorizationServer:
    def __init__(self, callback, *, token_scope=READ_SCOPE):
        self.callback = callback
        self.token_scope = token_scope
        self.calls = []

    def request_json(
        self,
        method,
        url,
        *,
        json_body=None,
        form=None,
        headers=None,
        timeout=None,
    ):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "json_body": json_body,
                "form": form,
            }
        )
        if url == RESOURCE_METADATA_URL:
            return {
                "resource": RESOURCE_URL,
                "authorization_servers": [ISSUER],
                "scopes_supported": [READ_SCOPE],
            }
        if url == f"{ISSUER}/.well-known/oauth-authorization-server":
            return {
                "issuer": ISSUER,
                "authorization_endpoint": AUTHORIZATION_ENDPOINT,
                "token_endpoint": TOKEN_ENDPOINT,
                "registration_endpoint": REGISTRATION_ENDPOINT,
                "code_challenge_methods_supported": ["S256"],
                "scopes_supported": [READ_SCOPE],
            }
        if url == REGISTRATION_ENDPOINT:
            return {"client_id": "CLIENT_SENTINEL"}
        if url == TOKEN_ENDPOINT and form["grant_type"] == "authorization_code":
            return {
                "access_token": "ACCESS_SENTINEL",
                "refresh_token": "REFRESH_SENTINEL",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": self.token_scope,
            }
        if url == TOKEN_ENDPOINT and form["grant_type"] == "refresh_token":
            return {
                "access_token": "ROTATED_ACCESS_SENTINEL",
                "refresh_token": "ROTATED_REFRESH_SENTINEL",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": self.token_scope,
            }
        raise AssertionError((method, url, json_body, form, headers, timeout))


def _process_refresh_worker(store_path, counter_path, start_event, output_queue):
    store = FileCredentialStore(store_path)

    def request_json(method, url, **kwargs):
        if kwargs["form"]["grant_type"] != "refresh_token":
            raise AssertionError((method, url, kwargs))
        with open(counter_path, "a", encoding="utf-8") as handle:
            handle.write("refresh\n")
            handle.flush()
        time.sleep(0.1)
        return {
            "access_token": "PROCESS_ROTATED_ACCESS",
            "refresh_token": "PROCESS_ROTATED_REFRESH",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": READ_SCOPE,
        }

    auth = TdxOwnedAuth(
        resource_url=RESOURCE_URL,
        resource_metadata_url=RESOURCE_METADATA_URL,
        store=store,
        request_json=request_json,
        browser_open=lambda _url: None,
        callback_factory=lambda: None,
    )
    start_event.wait(2)
    try:
        output_queue.put(("ok", auth.authorization()))
    except Exception as exc:  # pragma: no cover - assertion aid
        output_queue.put(("error", type(exc).__name__))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def test_file_store_is_private_atomic_and_missing_is_sanitized(self):
        path = self.root / "auth" / "tdx.json"
        store = FileCredentialStore(path)

        self.assertEqual("auth_missing", store.probe())
        with self.assertRaisesRegex(TdxAuthMissing, "credentials are missing"):
            store.load()

        store.save(token_bundle())

        self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(store.lock_path.stat().st_mode))
        self.assertEqual("configured_unverified", store.probe())
        self.assertEqual("ACCESS_SENTINEL", store.load()["access_token"])
        self.assertEqual([], list(path.parent.glob(".tdx-auth-*")))

    def test_legacy_token_without_owned_read_scope_fails_closed(self):
        path = self.root / "auth" / "tdx.json"
        store = FileCredentialStore(path)
        legacy = token_bundle()
        legacy.pop("scope")
        store.save(legacy)

        self.assertEqual("auth_expired", store.probe())
        auth = TdxOwnedAuth(
            resource_url=RESOURCE_URL,
            resource_metadata_url=RESOURCE_METADATA_URL,
            store=store,
            request_json=lambda *_args, **_kwargs: {},
            browser_open=lambda _url: None,
            callback_factory=lambda: None,
        )
        with self.assertRaises(TdxScopeError):
            auth.authorization()

    def test_keychain_store_uses_backend_without_secret_in_identifier(self):
        backend = FakeKeyring()
        store = KeychainCredentialStore(
            backend=backend,
            lock_path=self.root / "locks" / "tdx.lock",
        )

        self.assertEqual("auth_missing", store.probe())
        store.save(token_bundle())

        self.assertEqual("configured_unverified", store.probe())
        self.assertEqual("ACCESS_SENTINEL", store.load()["access_token"])
        rendered_keys = " ".join(" ".join(key) for key in backend.values)
        self.assertNotIn("ACCESS_SENTINEL", rendered_keys)
        self.assertNotIn("REFRESH_SENTINEL", rendered_keys)

    def test_keychain_is_default_and_file_fallback_must_be_explicit(self):
        backend = FakeKeyring()
        default = default_credential_store(
            backend=backend,
            lock_path=self.root / "default.lock",
        )
        fallback_path = self.root / "fallback" / "tdx.json"
        fallback = default_credential_store(mode="file", file_path=fallback_path)

        self.assertIsInstance(default, KeychainCredentialStore)
        self.assertIsInstance(fallback, FileCredentialStore)
        self.assertEqual(fallback_path, fallback.path)


class OwnedOAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.store = FileCredentialStore(self.root / "auth" / "tdx.json")

    def make_auth(self, callback, server=None, opened=None):
        server = server or FakeAuthorizationServer(callback)
        opened = opened if opened is not None else []
        auth = TdxOwnedAuth(
            resource_url=RESOURCE_URL,
            resource_metadata_url=RESOURCE_METADATA_URL,
            store=self.store,
            request_json=server.request_json,
            browser_open=opened.append,
            callback_factory=lambda: callback,
        )
        return auth, server, opened

    def test_pkce_is_s256_and_has_no_plain_fallback(self):
        verifier, challenge = generate_pkce()

        self.assertGreaterEqual(len(verifier), 43)
        self.assertLessEqual(len(verifier), 128)
        self.assertNotEqual(verifier, challenge)
        self.assertNotIn("=", challenge)

    def test_default_protected_resource_metadata_endpoint_has_no_path_suffix(self):
        self.assertEqual(
            "https://txmcp.tdx.com.cn:3001/.well-known/oauth-protected-resource",
            DEFAULT_RESOURCE_METADATA_URL,
        )

    def test_localhost_callback_binds_loopback_and_returns_only_query_fields(self):
        receiver = LocalhostCallbackReceiver()
        result = {}

        def wait_for_callback():
            result.update(receiver.wait(timeout=1))

        thread = threading.Thread(target=wait_for_callback)
        thread.start()
        callback_url = (
            f"{receiver.redirect_uri}?code=CODE_SENTINEL&state=STATE_SENTINEL"
            "&ignored=SECRET_SENTINEL"
        )
        with urllib.request.urlopen(callback_url, timeout=1) as response:
            self.assertEqual(200, response.status)
        thread.join(timeout=2)

        self.assertTrue(receiver.redirect_uri.startswith("http://127.0.0.1:"))
        self.assertEqual(
            {"code": "CODE_SENTINEL", "state": "STATE_SENTINEL"},
            result,
        )

    def test_login_discovers_registers_and_exchanges_code_with_pkce_and_state(self):
        callback = FakeCallback()
        auth, server, opened = self.make_auth(callback)
        state_holder = {}

        def browser_open(url):
            opened.append(url)
            state_holder.update(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query))
            callback.result = {
                "code": "AUTH_CODE_SENTINEL",
                "state": state_holder["state"][0],
            }

        auth.browser_open = browser_open

        status = auth.login(timeout=1)

        self.assertEqual("configured_unverified", status)
        self.assertEqual(1, len(opened))
        authorize_query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(opened[0]).query
        )
        self.assertEqual([READ_SCOPE], authorize_query["scope"])
        self.assertEqual(["S256"], authorize_query["code_challenge_method"])
        self.assertEqual([FakeCallback.redirect_uri], authorize_query["redirect_uri"])
        registration = next(
            call for call in server.calls if call["url"] == REGISTRATION_ENDPOINT
        )
        self.assertEqual(READ_SCOPE, registration["json_body"]["scope"])
        self.assertEqual("none", registration["json_body"]["token_endpoint_auth_method"])
        exchange = next(
            call
            for call in server.calls
            if call["url"] == TOKEN_ENDPOINT
            and call["form"]["grant_type"] == "authorization_code"
        )
        self.assertEqual("AUTH_CODE_SENTINEL", exchange["form"]["code"])
        self.assertEqual(
            authorize_query["code_challenge"][0],
            pkce_challenge(exchange["form"]["code_verifier"]),
        )
        self.assertEqual(READ_SCOPE, self.store.load()["scope"])

    def test_login_fails_closed_on_wrong_state_cancel_timeout_or_write_scope(self):
        cases = (
            (
                FakeCallback(result={"code": "CODE", "state": "wrong"}),
                None,
                TdxStateMismatch,
            ),
            (
                FakeCallback(result={"error": "access_denied", "state": "unused"}),
                None,
                TdxLoginCancelled,
            ),
            (FakeCallback(error=TimeoutError()), None, TdxLoginTimeout),
            (
                FakeCallback(result={"code": "CODE", "state": "wrong"}),
                "mcp.read mcp.write",
                TdxScopeError,
            ),
        )
        for callback, token_scope, expected in cases:
            with self.subTest(expected=expected.__name__, token_scope=token_scope):
                server = FakeAuthorizationServer(
                    callback,
                    token_scope=token_scope or READ_SCOPE,
                )
                auth, _server, _opened = self.make_auth(callback, server=server)
                if token_scope or expected is TdxLoginCancelled:
                    def browser_open(url):
                        state = urllib.parse.parse_qs(
                            urllib.parse.urlsplit(url).query
                        )["state"][0]
                        callback.result["state"] = state

                    auth.browser_open = browser_open
                with self.assertRaises(expected):
                    auth.login(timeout=0.01)
                self.assertEqual("auth_missing", self.store.probe())

    def test_refresh_rotates_tokens_and_concurrent_callers_refresh_once(self):
        self.store.save(token_bundle(expires_at_ms=1))
        callback = FakeCallback()
        server = FakeAuthorizationServer(callback)
        auth, _server, _opened = self.make_auth(callback, server=server)
        barrier = threading.Barrier(8)
        results = []
        failures = []

        def worker():
            try:
                barrier.wait()
                results.append(auth.authorization())
            except Exception as exc:  # pragma: no cover - assertion aid
                failures.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual([], failures)
        self.assertEqual(8, len(results))
        self.assertEqual({"Bearer ROTATED_ACCESS_SENTINEL"}, set(results))
        refreshes = [
            call
            for call in server.calls
            if call["url"] == TOKEN_ENDPOINT
            and call["form"]["grant_type"] == "refresh_token"
        ]
        self.assertEqual(1, len(refreshes))
        stored = self.store.load()
        self.assertEqual("ROTATED_ACCESS_SENTINEL", stored["access_token"])
        self.assertEqual("ROTATED_REFRESH_SENTINEL", stored["refresh_token"])

    def test_concurrent_forced_refresh_skips_when_rejected_token_already_rotated(self):
        self.store.save(token_bundle())
        callback = FakeCallback()
        server = FakeAuthorizationServer(callback)
        auth, _server, _opened = self.make_auth(callback, server=server)
        barrier = threading.Barrier(8)
        results = []

        def worker():
            barrier.wait()
            results.append(
                auth.authorization(
                    force_refresh=True,
                    rejected_authorization="Bearer ACCESS_SENTINEL",
                )
            )

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(8, len(results))
        refreshes = [
            call
            for call in server.calls
            if call["url"] == TOKEN_ENDPOINT
            and call["form"]["grant_type"] == "refresh_token"
        ]
        self.assertEqual(1, len(refreshes))

    def test_cross_process_refresh_lock_allows_only_one_refresh(self):
        self.store.save(token_bundle(expires_at_ms=1))
        counter_path = self.root / "refresh-count.txt"
        context = multiprocessing.get_context("spawn")
        start_event = context.Event()
        output_queue = context.Queue()
        processes = [
            context.Process(
                target=_process_refresh_worker,
                args=(self.store.path, counter_path, start_event, output_queue),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        start_event.set()
        results = [output_queue.get(timeout=5) for _ in processes]
        for process in processes:
            process.join(timeout=5)

        self.assertEqual(
            [("ok", "Bearer PROCESS_ROTATED_ACCESS")] * 2,
            sorted(results),
        )
        self.assertEqual("refresh\n", counter_path.read_text(encoding="utf-8"))

    def test_missing_refresh_or_write_scope_is_sanitized_expired(self):
        for bundle, expected in (
            (token_bundle(expires_at_ms=1, refresh_token=None), TdxAuthExpired),
            ({**token_bundle(expires_at_ms=1), "scope": "mcp.write"}, TdxScopeError),
        ):
            with self.subTest(expected=expected.__name__):
                self.store.save(bundle)
                auth, _server, _opened = self.make_auth(FakeCallback())
                with self.assertRaises(expected) as caught:
                    auth.authorization()
                rendered = str(caught.exception)
                self.assertNotIn("ACCESS_SENTINEL", rendered)
                self.assertNotIn("REFRESH_SENTINEL", rendered)


if __name__ == "__main__":
    unittest.main()
