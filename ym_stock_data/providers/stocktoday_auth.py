"""StockToday-owned token; never reuse official Tushare credentials."""

from __future__ import annotations

import os
import re
import threading

SERVICE = "ym-stock-data/stocktoday"
ACCOUNT = "api-token"
_CACHED_TOKEN: str | None = None
_TOKEN_LOCK = threading.Lock()


def _keyring():
    # Explicit OS backend: a missing/unavailable keychain must not fall back to files.
    from keyring.backends.macOS import Keyring
    return Keyring()


def _validate(token: str) -> str:
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.:+/=-]{16,2048}", token):
        raise ValueError("StockToday token must be a single plain token")
    return token


def _security_token() -> str | None:
    """Read the same macOS Keychain item without a Python package dependency."""

    import ctypes
    from ctypes import byref, c_int32, c_uint32, c_void_p
    from ctypes.util import find_library

    security_path = find_library("Security")
    foundation_path = find_library("Foundation")
    if not security_path or not foundation_path:
        raise RuntimeError("StockToday keychain unavailable")
    security = ctypes.CDLL(security_path)
    foundation = ctypes.CDLL(foundation_path)

    cf_string = foundation.CFStringCreateWithCString
    cf_string.restype = c_void_p
    cf_string.argtypes = [c_void_p, c_void_p, c_uint32]
    cf_number = foundation.CFNumberCreate
    cf_number.restype = c_void_p
    cf_number.argtypes = [c_void_p, c_uint32, c_void_p]
    cf_dictionary = foundation.CFDictionaryCreate
    cf_dictionary.restype = c_void_p
    cf_dictionary.argtypes = (
        c_void_p, c_void_p, c_void_p, c_int32, c_void_p, c_void_p
    )
    copy_matching = security.SecItemCopyMatching
    copy_matching.restype = c_int32
    copy_matching.argtypes = (c_void_p, c_void_p)
    data_pointer = foundation.CFDataGetBytePtr
    data_pointer.restype = c_void_p
    data_pointer.argtypes = (c_void_p,)
    data_length = foundation.CFDataGetLength
    data_length.restype = c_int32
    data_length.argtypes = (c_void_p,)

    def key(name: str) -> c_void_p:
        return c_void_p.in_dll(security, name)

    def string(value: str) -> c_void_p:
        return cf_string(None, value.encode("utf-8"), 0x08000100)

    truth = cf_number(None, 0x9, byref(c_int32(1)))
    keys = (c_void_p * 5)(
        key("kSecClass"),
        key("kSecMatchLimit"),
        key("kSecAttrService"),
        key("kSecAttrAccount"),
        key("kSecReturnData"),
    )
    values = (c_void_p * 5)(
        key("kSecClassGenericPassword"),
        key("kSecMatchLimitOne"),
        string(SERVICE),
        string(ACCOUNT),
        truth,
    )
    query = cf_dictionary(
        None,
        keys,
        values,
        5,
        foundation.kCFTypeDictionaryKeyCallBacks,
        foundation.kCFTypeDictionaryValueCallBacks,
    )
    data = c_void_p()
    status = copy_matching(query, byref(data))
    if status == -25300:
        return None
    if status != 0 or not data.value:
        raise RuntimeError("StockToday keychain unavailable")
    value = ctypes.string_at(data_pointer(data), data_length(data)).decode("utf-8")
    return _validate(value) if value else None


def load_token() -> str | None:
    global _CACHED_TOKEN
    value = os.environ.get("STOCKTODAY_TOKEN")
    if value:
        return _validate(value.strip())
    if _CACHED_TOKEN is not None:
        return _CACHED_TOKEN
    with _TOKEN_LOCK:
        if _CACHED_TOKEN is not None:
            return _CACHED_TOKEN
        try:
            keyring = _keyring()
        except ModuleNotFoundError:
            value = _security_token()
        except Exception:
            raise RuntimeError("StockToday keychain unavailable") from None
        else:
            try:
                value = keyring.get_password(SERVICE, ACCOUNT)
            except Exception:
                raise RuntimeError("StockToday keychain unavailable") from None
        if value:
            _CACHED_TOKEN = _validate(value)
        return _CACHED_TOKEN


def save_token(token: str) -> None:
    global _CACHED_TOKEN
    value = _validate(token.strip())
    try:
        _keyring().set_password(SERVICE, ACCOUNT, value)
    except Exception:
        raise RuntimeError("StockToday keychain unavailable") from None
    with _TOKEN_LOCK:
        _CACHED_TOKEN = value
