"""Protect passwords with Windows DPAPI and migrate legacy AES-GCM tokens."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import re
import socket
import sys
from functools import lru_cache
from typing import Any, Final


ENCRYPTION_PREFIX: Final = "enc:v2:dpapi:"
_LEGACY_ENCRYPTION_PREFIX: Final = "enc:v1:"
_KEY_SALT: Final = "WeSDAU@2026!SecureConfig#SDAU"
_THREE_PART_TOKEN = re.compile(
    r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$"
)
_IS_WINDOWS = sys.platform == "win32"

_BYTE = ctypes.c_ubyte
_DWORD = ctypes.c_uint32
_ULONG = ctypes.c_uint32
_ULONGLONG = ctypes.c_uint64
_NTSTATUS = ctypes.c_int32
_HANDLE = ctypes.c_void_p
_PBYTE = ctypes.POINTER(_BYTE)

_CRYPTPROTECT_UI_FORBIDDEN: Final = 0x00000001
_BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO_VERSION: Final = 1


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", _DWORD), ("pbData", _PBYTE)]


class _BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", _ULONG),
        ("dwInfoVersion", _ULONG),
        ("pbNonce", _PBYTE),
        ("cbNonce", _ULONG),
        ("pbAuthData", _PBYTE),
        ("cbAuthData", _ULONG),
        ("pbTag", _PBYTE),
        ("cbTag", _ULONG),
        ("pbMacContext", _PBYTE),
        ("cbMacContext", _ULONG),
        ("cbAAD", _ULONG),
        ("cbData", _ULONGLONG),
        ("dwFlags", _ULONG),
    ]


class SecretProtectionError(ValueError):
    """Base class for password protection failures."""


class SecretEncryptionError(SecretProtectionError):
    """Raised when a password cannot be protected safely."""


class SecretDecryptionError(SecretProtectionError):
    """Raised when an encrypted value cannot be decrypted on this machine."""


class _WindowsCryptoError(OSError):
    """Internal error raised by a Windows cryptography API."""


def _derive_key(hostname: str | None = None) -> bytes:
    """Reproduce Node's sha256(`${hostname}-${salt}`) key derivation."""

    machine_name = hostname if hostname is not None else socket.gethostname()
    return hashlib.sha256(f"{machine_name}-{_KEY_SALT}".encode("utf-8")).digest()


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _byte_buffer(value: bytes) -> Any:
    """Return a non-null ctypes buffer, including for an empty byte string."""

    buffer = (_BYTE * max(1, len(value)))()
    if value:
        ctypes.memmove(buffer, value, len(value))
    return buffer


@lru_cache(maxsize=1)
def _dpapi_functions() -> tuple[Any, Any, Any]:
    if not _IS_WINDOWS:
        raise _WindowsCryptoError("Windows DPAPI is unavailable on this platform")

    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as exc:
        raise _WindowsCryptoError("Windows DPAPI could not be loaded") from exc

    protect = crypt32.CryptProtectData
    protect.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_wchar_p,
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        _DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    protect.restype = ctypes.c_int

    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        _DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    unprotect.restype = ctypes.c_int

    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    return protect, unprotect, local_free


def _winerror(api_name: str) -> _WindowsCryptoError:
    error_code = ctypes.get_last_error()
    try:
        detail = ctypes.FormatError(error_code).strip()
    except (AttributeError, OSError):
        detail = "unknown Windows error"
    return _WindowsCryptoError(f"{api_name} failed ({error_code}): {detail}")


def _data_blob(value: bytes) -> tuple[_DATA_BLOB, Any]:
    buffer = _byte_buffer(value)
    blob = _DATA_BLOB(len(value), ctypes.cast(buffer, _PBYTE))
    return blob, buffer


def _copy_and_free_blob(
    blob: _DATA_BLOB, local_free: Any, *, clear_before_free: bool = False
) -> bytes:
    pointer = ctypes.cast(blob.pbData, ctypes.c_void_p)
    try:
        if blob.cbData and not pointer.value:
            raise _WindowsCryptoError("Windows returned an invalid data blob")
        return ctypes.string_at(pointer, blob.cbData) if blob.cbData else b""
    finally:
        if pointer.value:
            if clear_before_free and blob.cbData:
                ctypes.memset(pointer, 0, blob.cbData)
            # LocalFree returns NULL when the allocation was released.
            remaining = local_free(pointer)
            if remaining:
                raise _WindowsCryptoError("LocalFree failed to release a DPAPI buffer")


def _protect_dpapi(plaintext: bytes) -> bytes:
    protect, _, local_free = _dpapi_functions()
    input_blob, input_buffer = _data_blob(plaintext)
    output_blob = _DATA_BLOB()
    try:
        if not protect(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        ):
            raise _winerror("CryptProtectData")
        return _copy_and_free_blob(output_blob, local_free)
    finally:
        ctypes.memset(input_buffer, 0, len(input_buffer))


def _unprotect_dpapi(protected: bytes) -> bytes:
    _, unprotect, local_free = _dpapi_functions()
    input_blob, input_buffer = _data_blob(protected)
    output_blob = _DATA_BLOB()
    if not unprotect(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise _winerror("CryptUnprotectData")
    # Wipe DPAPI's LocalAlloc plaintext buffer before releasing it.
    return _copy_and_free_blob(output_blob, local_free, clear_before_free=True)


@lru_cache(maxsize=1)
def _bcrypt_functions() -> tuple[Any, ...]:
    if not _IS_WINDOWS:
        raise _WindowsCryptoError("Windows CNG is unavailable on this platform")

    try:
        bcrypt = ctypes.WinDLL("bcrypt", use_last_error=True)
    except (AttributeError, OSError) as exc:
        raise _WindowsCryptoError("Windows CNG could not be loaded") from exc

    open_provider = bcrypt.BCryptOpenAlgorithmProvider
    open_provider.argtypes = [
        ctypes.POINTER(_HANDLE),
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        _ULONG,
    ]
    open_provider.restype = _NTSTATUS

    close_provider = bcrypt.BCryptCloseAlgorithmProvider
    close_provider.argtypes = [_HANDLE, _ULONG]
    close_provider.restype = _NTSTATUS

    set_property = bcrypt.BCryptSetProperty
    set_property.argtypes = [_HANDLE, ctypes.c_wchar_p, _PBYTE, _ULONG, _ULONG]
    set_property.restype = _NTSTATUS

    get_property = bcrypt.BCryptGetProperty
    get_property.argtypes = [
        _HANDLE,
        ctypes.c_wchar_p,
        _PBYTE,
        _ULONG,
        ctypes.POINTER(_ULONG),
        _ULONG,
    ]
    get_property.restype = _NTSTATUS

    generate_key = bcrypt.BCryptGenerateSymmetricKey
    generate_key.argtypes = [
        _HANDLE,
        ctypes.POINTER(_HANDLE),
        _PBYTE,
        _ULONG,
        _PBYTE,
        _ULONG,
        _ULONG,
    ]
    generate_key.restype = _NTSTATUS

    destroy_key = bcrypt.BCryptDestroyKey
    destroy_key.argtypes = [_HANDLE]
    destroy_key.restype = _NTSTATUS

    decrypt = bcrypt.BCryptDecrypt
    decrypt.argtypes = [
        _HANDLE,
        _PBYTE,
        _ULONG,
        ctypes.c_void_p,
        _PBYTE,
        _ULONG,
        _PBYTE,
        _ULONG,
        ctypes.POINTER(_ULONG),
        _ULONG,
    ]
    decrypt.restype = _NTSTATUS
    return (
        open_provider,
        close_provider,
        set_property,
        get_property,
        generate_key,
        destroy_key,
        decrypt,
    )


def _check_ntstatus(status: int, api_name: str) -> None:
    if status:
        unsigned_status = ctypes.c_uint32(status).value
        raise _WindowsCryptoError(f"{api_name} failed (NTSTATUS 0x{unsigned_status:08X})")


def _decrypt_aes_gcm(
    ciphertext: bytes, auth_tag: bytes, nonce: bytes, key: bytes
) -> bytes:
    (
        open_provider,
        close_provider,
        set_property,
        get_property,
        generate_key,
        destroy_key,
        decrypt,
    ) = _bcrypt_functions()

    algorithm = _HANDLE()
    key_handle = _HANDLE()
    key_object: Any = None
    key_buffer = _byte_buffer(key)
    plaintext_buffer = _byte_buffer(b"\0" * len(ciphertext))
    try:
        _check_ntstatus(
            open_provider(ctypes.byref(algorithm), "AES", None, 0),
            "BCryptOpenAlgorithmProvider",
        )

        chaining_mode = ctypes.create_unicode_buffer("ChainingModeGCM")
        _check_ntstatus(
            set_property(
                algorithm,
                "ChainingMode",
                ctypes.cast(chaining_mode, _PBYTE),
                ctypes.sizeof(chaining_mode),
                0,
            ),
            "BCryptSetProperty",
        )

        object_length = _ULONG()
        result_length = _ULONG()
        _check_ntstatus(
            get_property(
                algorithm,
                "ObjectLength",
                ctypes.cast(ctypes.byref(object_length), _PBYTE),
                ctypes.sizeof(object_length),
                ctypes.byref(result_length),
                0,
            ),
            "BCryptGetProperty",
        )
        if result_length.value != ctypes.sizeof(object_length) or not object_length.value:
            raise _WindowsCryptoError("BCryptGetProperty returned an invalid key size")

        key_object = (_BYTE * object_length.value)()
        _check_ntstatus(
            generate_key(
                algorithm,
                ctypes.byref(key_handle),
                ctypes.cast(key_object, _PBYTE),
                object_length.value,
                ctypes.cast(key_buffer, _PBYTE),
                len(key),
                0,
            ),
            "BCryptGenerateSymmetricKey",
        )

        nonce_buffer = _byte_buffer(nonce)
        tag_buffer = _byte_buffer(auth_tag)
        ciphertext_buffer = _byte_buffer(ciphertext)
        auth_info = _BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO()
        auth_info.cbSize = ctypes.sizeof(auth_info)
        auth_info.dwInfoVersion = _BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO_VERSION
        auth_info.pbNonce = ctypes.cast(nonce_buffer, _PBYTE)
        auth_info.cbNonce = len(nonce)
        auth_info.pbTag = ctypes.cast(tag_buffer, _PBYTE)
        auth_info.cbTag = len(auth_tag)

        plaintext_length = _ULONG()
        _check_ntstatus(
            decrypt(
                key_handle,
                ctypes.cast(ciphertext_buffer, _PBYTE),
                len(ciphertext),
                ctypes.byref(auth_info),
                None,
                0,
                ctypes.cast(plaintext_buffer, _PBYTE),
                len(ciphertext),
                ctypes.byref(plaintext_length),
                0,
            ),
            "BCryptDecrypt",
        )
        if plaintext_length.value != len(ciphertext):
            raise _WindowsCryptoError("BCryptDecrypt returned an invalid plaintext size")
        return bytes(plaintext_buffer[: plaintext_length.value])
    finally:
        if key_handle.value:
            destroy_key(key_handle)
        if algorithm.value:
            close_provider(algorithm, 0)
        ctypes.memset(key_buffer, 0, len(key_buffer))
        ctypes.memset(plaintext_buffer, 0, len(plaintext_buffer))
        if key_object is not None:
            ctypes.memset(key_object, 0, len(key_object))


def _decode_legacy_payload(payload: str) -> tuple[bytes, bytes, bytes]:
    iv_raw, auth_tag_raw, ciphertext_raw = payload.split(".")
    iv = _b64url_decode(iv_raw)
    auth_tag = _b64url_decode(auth_tag_raw)
    ciphertext = _b64url_decode(ciphertext_raw)
    if len(iv) != 12 or len(auth_tag) != 16:
        raise ValueError("invalid legacy AES-GCM token")
    return iv, auth_tag, ciphertext


def is_encrypted(value: str) -> bool:
    """Return whether *value* is a supported encrypted token.

    Damaged versioned values deliberately remain classified as encrypted so a
    caller never protects the corrupt token as if it were a plaintext password.
    """

    if not isinstance(value, str):
        return False
    # Unknown versioned formats must never fall through as plaintext.  A future
    # or corrupt ``enc:v*`` token can only be handled by an explicit decoder.
    if value.startswith("enc:v"):
        return True
    if not _THREE_PART_TOKEN.fullmatch(value):
        return False
    try:
        _decode_legacy_payload(value)
    except (ValueError, TypeError):
        return False
    return True


def encrypt_secret(plaintext: str) -> str:
    """Protect text with current-user Windows DPAPI.

    ``CRYPTPROTECT_UI_FORBIDDEN`` prevents background CLI or frozen builds from
    displaying an unexpected prompt.  The machine-wide DPAPI flag is never used.
    """

    if not isinstance(plaintext, str):
        raise TypeError("plaintext must be a string")
    if not _IS_WINDOWS:
        raise SecretEncryptionError(
            "Password encryption requires Windows DPAPI; plaintext was not saved."
        )
    try:
        protected = _protect_dpapi(plaintext.encode("utf-8"))
    except (OSError, ValueError) as exc:
        raise SecretEncryptionError(f"Unable to protect the password: {exc}") from exc
    return f"{ENCRYPTION_PREFIX}{_b64url_encode(protected)}"


def decrypt_secret(token: str) -> str | None:
    """Decrypt a DPAPI or legacy token, returning ``None`` on any failure."""

    if not is_encrypted(token) or not _IS_WINDOWS:
        return None

    try:
        if token.startswith(ENCRYPTION_PREFIX):
            protected = _b64url_decode(token[len(ENCRYPTION_PREFIX) :])
            plaintext = _unprotect_dpapi(protected)
        else:
            payload = (
                token[len(_LEGACY_ENCRYPTION_PREFIX) :]
                if token.startswith(_LEGACY_ENCRYPTION_PREFIX)
                else token
            )
            iv, auth_tag, ciphertext = _decode_legacy_payload(payload)
            plaintext = _decrypt_aes_gcm(ciphertext, auth_tag, iv, _derive_key())
        return plaintext.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return None


def decrypt_secret_or_raise(token: str) -> str:
    """Decrypt *token* or raise a user-facing configuration error."""

    plaintext = decrypt_secret(token)
    if plaintext is None:
        raise SecretDecryptionError(
            "密码解密失败。配置可能来自其他 Windows 用户/电脑或已经损坏，"
            "请运行 config 重新设置密码。"
        )
    return plaintext


__all__ = [
    "ENCRYPTION_PREFIX",
    "SecretDecryptionError",
    "SecretEncryptionError",
    "SecretProtectionError",
    "decrypt_secret",
    "decrypt_secret_or_raise",
    "encrypt_secret",
    "is_encrypted",
]
