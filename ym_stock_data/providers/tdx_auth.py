"""Pipeline-owned OAuth 2.0 authorization for the read-only TDX MCP.

This module deliberately owns OAuth lifecycle and credential persistence while
leaving MCP protocol transport to the official MCP SDK.  It never discovers or
imports credentials from another application's storage.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import http.server
import json
import os
import secrets
import stat
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable, Protocol


READ_SCOPE = "mcp.read"
REFRESH_SKEW_MS = 5 * 60 * 1000
DEFAULT_RESOURCE_URL = "https://txmcp.tdx.com.cn:3001/txmcp"
DEFAULT_RESOURCE_METADATA_URL = (
    "https://txmcp.tdx.com.cn:3001/.well-known/oauth-protected-resource"
)
DEFAULT_FILE_PATH = Path.home() / ".ym-stock-data" / "auth" / "tdx.json"
DEFAULT_LOCK_PATH = Path.home() / ".ym-stock-data" / "auth" / "tdx-refresh.lock"
DEFAULT_SELECTOR_PATH = Path.home() / ".ym-stock-data" / "auth" / "tdx-store.json"
KEYCHAIN_SERVICE = "ym-stock-data/tdx-oauth"
KEYCHAIN_USERNAME = "owned-read-only"


class TdxAuthError(RuntimeError):
    """Base class for sanitized TDX authorization failures."""


class TdxAuthMissing(TdxAuthError):
    """No pipeline-owned credentials exist."""


class TdxAuthExpired(TdxAuthError):
    """Stored authorization cannot be refreshed."""


class TdxScopeError(TdxAuthError):
    """The authorization server did not preserve the read-only scope."""


class TdxStateMismatch(TdxAuthError):
    """The OAuth callback state did not match the login request."""


class TdxLoginCancelled(TdxAuthError):
    """The user cancelled the authorization request."""


class TdxLoginTimeout(TdxAuthError):
    """The localhost callback did not arrive in time."""


class TdxOAuthProtocolError(TdxAuthError):
    """OAuth discovery, registration, or token exchange was invalid."""


class CredentialStore(Protocol):
    lock_path: Path

    def load(self) -> dict: ...

    def save(self, payload: dict) -> None: ...

    def probe(self) -> str: ...


class InvalidSelectorCredentialStore:
    """Fail-closed store used when the persisted selector is not trustworthy."""

    report_mode = "selected"

    def __init__(self, selector_path: str | Path):
        self.lock_path = Path(selector_path).expanduser()

    @staticmethod
    def _raise() -> None:
        raise TdxAuthExpired("TDX credential selector is invalid")

    def load(self) -> dict:
        self._raise()

    def save(self, _payload: dict) -> None:
        self._raise()

    def probe(self) -> str:
        return "auth_expired"

    def preflight_login(self) -> None:
        self._raise()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _private_stat(value: os.stat_result, *, kind: str, mode: int) -> None:
    expected_kind = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    if (
        not expected_kind(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_IMODE(value.st_mode) != mode
    ):
        raise TdxAuthExpired(f"TDX credential {kind} is not private")


def _ensure_private_directory(path: Path) -> None:
    try:
        value = path.lstat()
    except FileNotFoundError:
        parent = path.parent
        if parent == path:
            raise TdxAuthExpired("TDX credential directory is unavailable") from None
        if not parent.exists():
            _ensure_private_directory(parent)
        else:
            try:
                parent_value = parent.lstat()
            except OSError:
                raise TdxAuthExpired("TDX credential directory is unavailable") from None
            if not stat.S_ISDIR(parent_value.st_mode):
                raise TdxAuthExpired("TDX credential directory is unavailable")
        try:
            path.mkdir(mode=0o700)
        except OSError:
            raise TdxAuthExpired("TDX credential directory is unavailable") from None
        value = path.lstat()
    except OSError:
        raise TdxAuthExpired("TDX credential directory is unavailable") from None
    _private_stat(value, kind="directory", mode=0o700)


def _validate_private_regular(path: Path) -> None:
    try:
        value = path.lstat()
    except OSError:
        raise TdxAuthExpired("TDX credential file is invalid") from None
    _private_stat(value, kind="file", mode=0o600)


def _ensure_private_file(path: Path) -> None:
    _ensure_private_directory(path.parent)
    try:
        _validate_private_regular(path)
        return
    except TdxAuthExpired:
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        else:
            raise
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise TdxAuthExpired("TDX credential file is invalid") from None
    try:
        os.fchmod(descriptor, 0o600)
        _private_stat(os.fstat(descriptor), kind="file", mode=0o600)
    finally:
        os.close(descriptor)


def _read_private_json(path: Path) -> dict:
    _ensure_private_directory(path.parent)
    _validate_private_regular(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            _private_stat(os.fstat(handle.fileno()), kind="file", mode=0o600)
            payload = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise TdxAuthExpired("owned TDX credentials are invalid") from None
    return _validate_payload(payload)


def _atomic_private_json(path: Path, payload: dict) -> None:
    _ensure_private_directory(path.parent)
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        raise TdxAuthExpired("TDX credential file is invalid") from None
    else:
        _validate_private_regular(path)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".tdx-auth-",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _validate_private_regular(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except TdxAuthError:
        raise
    except OSError:
        raise TdxAuthExpired("TDX credential write failed") from None
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _validate_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise TdxAuthExpired("owned TDX credentials are invalid")
    return payload


def _probe_payload(payload: dict) -> str:
    try:
        _validate_scope(payload.get("scope"), allow_missing=False)
    except TdxScopeError:
        return "auth_expired"
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    client_id = payload.get("client_id")
    issuer = payload.get("issuer")
    token_endpoint = payload.get("token_endpoint")
    expires_at_ms = payload.get("expires_at_ms")
    if payload.get("token_type") != "Bearer":
        return "auth_expired"
    if not all(
        isinstance(value, str) and value
        for value in (client_id, refresh_token, issuer, token_endpoint)
    ):
        return "auth_expired"
    try:
        _validate_https_url(issuer, "issuer")
        _validate_https_url(token_endpoint, "token endpoint")
    except TdxOAuthProtocolError:
        return "auth_expired"
    if not isinstance(expires_at_ms, int):
        return "auth_expired"
    if expires_at_ms <= _now_ms() + REFRESH_SKEW_MS:
        if not isinstance(refresh_token, str) or not refresh_token:
            return "auth_expired"
    elif not isinstance(access_token, str) or not access_token:
        return "auth_expired"
    return "configured_unverified"


class FileCredentialStore:
    """Explicit file fallback with private permissions and atomic writes."""

    def __init__(self, path: str | Path = DEFAULT_FILE_PATH):
        self.path = Path(path).expanduser()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def load(self) -> dict:
        try:
            self.path.lstat()
        except FileNotFoundError:
            raise TdxAuthMissing("owned TDX credentials are missing")
        except OSError:
            raise TdxAuthExpired("owned TDX credentials are invalid") from None
        return _read_private_json(self.path)

    def save(self, payload: dict) -> None:
        value = _validate_payload(payload)
        _ensure_private_file(self.lock_path)
        _atomic_private_json(self.path, value)

    def probe(self) -> str:
        try:
            return _probe_payload(self.load())
        except TdxAuthMissing:
            return "auth_missing"
        except TdxAuthExpired:
            return "auth_expired"


class KeychainCredentialStore:
    """macOS Keychain-backed store; secret JSON never enters process argv."""

    def __init__(
        self,
        *,
        backend=None,
        service: str = KEYCHAIN_SERVICE,
        username: str = KEYCHAIN_USERNAME,
        lock_path: str | Path = DEFAULT_LOCK_PATH,
    ):
        if backend is None:
            import keyring

            backend = keyring
        self.backend = backend
        self.service = service
        self.username = username
        self.lock_path = Path(lock_path).expanduser()

    def load(self) -> dict:
        try:
            raw = self.backend.get_password(self.service, self.username)
        except Exception:
            raise TdxAuthExpired("TDX Keychain access failed") from None
        if raw is None:
            raise TdxAuthMissing("owned TDX credentials are missing")
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise TdxAuthExpired("owned TDX credentials are invalid") from None
        return _validate_payload(payload)

    def save(self, payload: dict) -> None:
        value = json.dumps(
            _validate_payload(payload), ensure_ascii=False, sort_keys=True
        )
        _ensure_private_file(self.lock_path)
        try:
            self.backend.set_password(self.service, self.username, value)
        except Exception:
            raise TdxAuthExpired("TDX Keychain write failed") from None

    def probe(self) -> str:
        try:
            return _probe_payload(self.load())
        except TdxAuthMissing:
            return "auth_missing"
        except TdxAuthExpired:
            return "auth_expired"


class CredentialStoreSelector:
    """Persist the non-secret active credential-store selection."""

    def __init__(self, path: str | Path = DEFAULT_SELECTOR_PATH):
        self.path = Path(path).expanduser()

    def load(self) -> dict:
        try:
            self.path.lstat()
        except FileNotFoundError:
            return {"schema_version": "1", "mode": "keychain"}
        except OSError:
            raise TdxAuthExpired("TDX credential selector is invalid") from None
        payload = _read_private_json(self.path)
        mode = payload.get("mode")
        if payload.get("schema_version") != "1" or mode not in {"keychain", "file"}:
            raise TdxAuthExpired("TDX credential selector is invalid")
        if mode == "keychain":
            if set(payload) != {"schema_version", "mode"}:
                raise TdxAuthExpired("TDX credential selector is invalid")
            return payload
        file_path = payload.get("file_path")
        if (
            set(payload) != {"schema_version", "mode", "file_path"}
            or not isinstance(file_path, str)
            or not file_path
            or not Path(file_path).is_absolute()
        ):
            raise TdxAuthExpired("TDX credential selector is invalid")
        return payload

    def save(self, mode: str, *, file_path: str | Path | None = None) -> None:
        if mode == "keychain" and file_path is None:
            payload = {"schema_version": "1", "mode": "keychain"}
        elif mode == "file" and file_path is not None:
            resolved = Path(file_path).expanduser().absolute()
            payload = {
                "schema_version": "1",
                "mode": "file",
                "file_path": str(resolved),
            }
        else:
            raise ValueError("file credential store requires an explicit path")
        _atomic_private_json(self.path, payload)


def default_credential_store(
    *,
    mode: str | None = None,
    backend=None,
    file_path: str | Path = DEFAULT_FILE_PATH,
    lock_path: str | Path = DEFAULT_LOCK_PATH,
    selector_path: str | Path | None = None,
) -> CredentialStore:
    """Resolve the selected store; file persistence requires explicit setup."""

    if mode is None:
        selected_path = selector_path or DEFAULT_SELECTOR_PATH
        try:
            selector = CredentialStoreSelector(selected_path).load()
        except TdxAuthExpired:
            return InvalidSelectorCredentialStore(selected_path)
        mode = selector["mode"]
        if mode == "file":
            file_path = selector["file_path"]

    if mode == "keychain":
        return KeychainCredentialStore(backend=backend, lock_path=lock_path)
    if mode == "file":
        return FileCredentialStore(file_path)
    raise ValueError("TDX credential store mode must be 'keychain' or 'file'")


def persist_credential_store_selection(
    mode: str,
    *,
    file_path: str | Path | None = None,
    selector_path: str | Path | None = None,
) -> None:
    CredentialStoreSelector(selector_path or DEFAULT_SELECTOR_PATH).save(
        mode,
        file_path=file_path,
    )


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class RefreshLock:
    """Serialize refresh across both threads and local processes."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self._handle = None
        self._thread_lock = None

    def __enter__(self):
        key = str(self.path.resolve())
        with _THREAD_LOCKS_GUARD:
            self._thread_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
        self._thread_lock.acquire()
        try:
            _ensure_private_file(self.path)
            self._handle = self.path.open("r+", encoding="utf-8")
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        except Exception:
            self._thread_lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self._handle is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                self._handle.close()
        finally:
            if self._thread_lock is not None:
                self._thread_lock.release()


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    return verifier, pkce_challenge(verifier)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server_version = "ym-data-oauth-callback"

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        split = urllib.parse.urlsplit(self.path)
        if split.path != "/callback":
            self.send_error(404)
            return
        query = urllib.parse.parse_qs(split.query)
        self.server.oauth_result = {
            key: query[key][0]
            for key in ("code", "state", "error")
            if key in query and query[key]
        }
        body = b"TDX authorization received. You may close this window."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *args):
        return


class LocalhostCallbackReceiver:
    """One-shot authorization callback bound only to IPv4 loopback."""

    def __init__(self):
        self._server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        self._server.oauth_result = None
        host, port = self._server.server_address
        self.redirect_uri = f"http://{host}:{port}/callback"

    def wait(self, timeout: float) -> dict:
        self._server.timeout = timeout
        try:
            self._server.handle_request()
            result = self._server.oauth_result
        finally:
            self.close()
        if not isinstance(result, dict):
            raise TimeoutError
        return result

    def close(self) -> None:
        self._server.server_close()


JsonRequester = Callable[..., dict]


def _request_json(
    method: str,
    url: str,
    *,
    json_body: dict | None = None,
    form: dict | None = None,
    headers: dict | None = None,
    timeout: float = 30,
) -> dict:
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if json_body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(json_body).encode("utf-8")
    elif form is not None:
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(1_000_001).decode("utf-8"))
    except Exception:
        raise TdxOAuthProtocolError("TDX OAuth request failed") from None
    if not isinstance(payload, dict):
        raise TdxOAuthProtocolError("TDX OAuth response is invalid")
    return payload


def _validate_https_url(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TdxOAuthProtocolError(f"TDX OAuth {label} is invalid")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.fragment:
        raise TdxOAuthProtocolError(f"TDX OAuth {label} is invalid")
    return value


def _validate_scope(value: object, *, allow_missing: bool = False) -> str:
    if value is None and allow_missing:
        return READ_SCOPE
    if not isinstance(value, str):
        raise TdxScopeError("TDX OAuth scope is not read-only")
    scopes = set(value.split())
    if scopes != {READ_SCOPE}:
        raise TdxScopeError("TDX OAuth scope is not read-only")
    return READ_SCOPE


class TdxOwnedAuth:
    """Own discovery, DCR, authorization-code PKCE, and refresh rotation."""

    def __init__(
        self,
        *,
        store: CredentialStore,
        resource_url: str = DEFAULT_RESOURCE_URL,
        resource_metadata_url: str = DEFAULT_RESOURCE_METADATA_URL,
        request_json: JsonRequester = _request_json,
        browser_open: Callable[[str], object] = webbrowser.open,
        callback_factory: Callable[[], object] = LocalhostCallbackReceiver,
    ):
        self.resource_url = _validate_https_url(resource_url, "resource URL")
        self.resource_metadata_url = _validate_https_url(
            resource_metadata_url, "resource metadata URL"
        )
        self.store = store
        self.request_json = request_json
        self.browser_open = browser_open
        self.callback_factory = callback_factory

    def probe(self) -> str:
        return self.store.probe()

    def _discover(self) -> dict:
        resource = self.request_json("GET", self.resource_metadata_url)
        if resource.get("resource") != self.resource_url:
            raise TdxOAuthProtocolError("TDX OAuth resource metadata mismatch")
        servers = resource.get("authorization_servers")
        if not isinstance(servers, list) or len(servers) != 1:
            raise TdxOAuthProtocolError("TDX OAuth authorization server is ambiguous")
        issuer = _validate_https_url(servers[0], "issuer")
        _validate_scope_from_supported(resource.get("scopes_supported"))
        metadata_url = f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server"
        metadata = self.request_json("GET", metadata_url)
        if metadata.get("issuer") != issuer:
            raise TdxOAuthProtocolError("TDX OAuth issuer metadata mismatch")
        _validate_scope_from_supported(metadata.get("scopes_supported"))
        methods = metadata.get("code_challenge_methods_supported")
        if not isinstance(methods, list) or "S256" not in methods:
            raise TdxOAuthProtocolError("TDX OAuth PKCE S256 is unavailable")
        return {
            "issuer": issuer,
            "authorization_endpoint": _validate_https_url(
                metadata.get("authorization_endpoint"), "authorization endpoint"
            ),
            "token_endpoint": _validate_https_url(
                metadata.get("token_endpoint"), "token endpoint"
            ),
            "registration_endpoint": _validate_https_url(
                metadata.get("registration_endpoint"), "registration endpoint"
            ),
        }

    def login(self, *, timeout: float = 180) -> str:
        preflight = getattr(self.store, "preflight_login", None)
        if callable(preflight):
            preflight()
        callback = self.callback_factory()
        try:
            return self._login_with_callback(callback, timeout)
        finally:
            close = getattr(callback, "close", None)
            if callable(close):
                close()

    def _login_with_callback(self, callback, timeout: float) -> str:
        redirect_uri = callback.redirect_uri
        discovery = self._discover()
        registration = self.request_json(
            "POST",
            discovery["registration_endpoint"],
            json_body={
                "client_name": "ym-stock-data",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": READ_SCOPE,
            },
        )
        client_id = registration.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise TdxOAuthProtocolError("TDX OAuth client registration failed")
        verifier, challenge = generate_pkce()
        state = secrets.token_urlsafe(32)
        authorization_url = discovery["authorization_endpoint"] + "?" + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": READ_SCOPE,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": self.resource_url,
            }
        )
        self.browser_open(authorization_url)
        try:
            callback_result = callback.wait(timeout)
        except TimeoutError:
            raise TdxLoginTimeout("TDX OAuth login timed out") from None
        if callback_result.get("state") != state:
            raise TdxStateMismatch("TDX OAuth callback state mismatch")
        if callback_result.get("error"):
            raise TdxLoginCancelled("TDX OAuth login was cancelled")
        code = callback_result.get("code")
        if not isinstance(code, str) or not code:
            raise TdxOAuthProtocolError("TDX OAuth callback is invalid")
        token = self.request_json(
            "POST",
            discovery["token_endpoint"],
            form={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
                "resource": self.resource_url,
            },
        )
        payload = self._token_payload(
            token,
            client_id=client_id,
            issuer=discovery["issuer"],
            token_endpoint=discovery["token_endpoint"],
        )
        self.store.save(payload)
        return "configured_unverified"

    def authorization(
        self,
        *,
        force_refresh: bool = False,
        rejected_authorization: str | None = None,
    ) -> str:
        payload = self.store.load()
        _validate_scope(payload.get("scope"), allow_missing=False)
        if not force_refresh and self._fresh(payload):
            return self._authorization_value(payload)
        with RefreshLock(self.store.lock_path):
            payload = self.store.load()
            _validate_scope(payload.get("scope"), allow_missing=False)
            if (
                force_refresh
                and rejected_authorization is not None
                and self._fresh(payload)
                and self._authorization_value(payload) != rejected_authorization
            ):
                return self._authorization_value(payload)
            if not force_refresh and self._fresh(payload):
                return self._authorization_value(payload)
            refresh_token = payload.get("refresh_token")
            client_id = payload.get("client_id")
            token_endpoint = payload.get("token_endpoint")
            issuer = payload.get("issuer")
            if not all(
                isinstance(value, str) and value
                for value in (refresh_token, client_id, token_endpoint, issuer)
            ):
                raise TdxAuthExpired("TDX OAuth refresh is unavailable")
            try:
                _validate_https_url(issuer, "issuer")
                _validate_https_url(token_endpoint, "token endpoint")
                token = self.request_json(
                    "POST",
                    token_endpoint,
                    form={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": client_id,
                        "scope": READ_SCOPE,
                        "resource": self.resource_url,
                    },
                )
                updated = self._token_payload(
                    token,
                    client_id=client_id,
                    issuer=issuer,
                    token_endpoint=token_endpoint,
                    previous_refresh_token=refresh_token,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                raise TdxAuthExpired("TDX OAuth refresh failed") from None
            self.store.save(updated)
            return self._authorization_value(updated)

    @staticmethod
    def _fresh(payload: dict) -> bool:
        return (
            isinstance(payload.get("access_token"), str)
            and bool(payload["access_token"])
            and isinstance(payload.get("expires_at_ms"), int)
            and payload["expires_at_ms"] > _now_ms() + REFRESH_SKEW_MS
        )

    @staticmethod
    def _authorization_value(payload: dict) -> str:
        if payload.get("token_type") != "Bearer":
            raise TdxAuthExpired("TDX OAuth token type is invalid")
        return f"Bearer {payload['access_token']}"

    @staticmethod
    def _token_payload(
        token: dict,
        *,
        client_id: str,
        issuer: str,
        token_endpoint: str,
        previous_refresh_token: str | None = None,
    ) -> dict:
        access_token = token.get("access_token") if isinstance(token, dict) else None
        refresh_token = token.get("refresh_token") or previous_refresh_token
        try:
            expires_in = int(token.get("expires_in"))
        except (AttributeError, TypeError, ValueError):
            expires_in = 0
        _validate_scope(token.get("scope"), allow_missing=False)
        if token.get("token_type") != "Bearer":
            raise TdxOAuthProtocolError("TDX OAuth token type is invalid")
        if not isinstance(access_token, str) or not access_token or expires_in <= 0:
            raise TdxOAuthProtocolError("TDX OAuth token response is invalid")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise TdxOAuthProtocolError("TDX OAuth refresh token is missing")
        return {
            "schema_version": "1",
            "client_id": client_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "scope": READ_SCOPE,
            "expires_at_ms": _now_ms() + expires_in * 1000,
            "issuer": issuer,
            "token_endpoint": token_endpoint,
        }


def _validate_scope_from_supported(value: object) -> None:
    if not isinstance(value, list) or READ_SCOPE not in value:
        raise TdxScopeError("TDX OAuth read scope is unavailable")
