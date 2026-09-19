from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac
import secrets


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[DATA_BLOB, object]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


_crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
_kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
_crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), wt.LPCWSTR, ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(DATA_BLOB),
]
_crypt32.CryptProtectData.restype = wt.BOOL
_crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wt.LPWSTR), ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(DATA_BLOB),
]
_crypt32.CryptUnprotectData.restype = wt.BOOL
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p


def protect_secret(value: str) -> str:
    if not value:
        return ""
    source, source_buffer = _blob(value.encode("utf-8"))
    output = DATA_BLOB()
    prefix = "dpapi:"
    if not _crypt32.CryptProtectData(ctypes.byref(source), "StockMonitor", None, None, None, 1, ctypes.byref(output)):
        output = DATA_BLOB()
        prefix = "dpapi-machine:"
        if not _crypt32.CryptProtectData(ctypes.byref(source), "StockMonitor", None, None, None, 5, ctypes.byref(output)):
            raise OSError(ctypes.get_last_error(), "Windows DPAPI 加密失败")
    try:
        encrypted = ctypes.string_at(output.pbData, output.cbData)
        return prefix + base64.urlsafe_b64encode(encrypted).decode("ascii")
    finally:
        _kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))
        _ = source_buffer


def unprotect_secret(value: str) -> str:
    if not value:
        return ""
    if value.startswith("dpapi-machine:"):
        payload = value[len("dpapi-machine:"):]
    elif value.startswith("dpapi:"):
        payload = value[len("dpapi:"):]
    else:
        return value
    encrypted = base64.urlsafe_b64decode(payload.encode("ascii"))
    source, source_buffer = _blob(encrypted)
    output = DATA_BLOB()
    if not _crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise OSError(ctypes.get_last_error(), "Windows DPAPI 解密失败")
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        _kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))
        _ = source_buffer


def hash_password(password: str, iterations: int = 260_000) -> str:
    if len(password) < 8:
        raise ValueError("网页登录密码至少需要8位")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text)
        expected = base64.urlsafe_b64decode(digest_text)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def new_access_token() -> str:
    return secrets.token_urlsafe(32)
