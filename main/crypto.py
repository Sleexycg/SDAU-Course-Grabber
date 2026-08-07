"""Machine-bound password encryption compatible with the former Node.js app."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ENCRYPTION_PREFIX: Final = "enc:v1:"
_KEY_SALT: Final = "WeSDAU@2026!SecureConfig#SDAU"
_THREE_PART_TOKEN = re.compile(
    r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$"
)


class SecretDecryptionError(ValueError):
    """Raised when an encrypted value cannot be decrypted on this machine."""


def _derive_key(hostname: str | None = None) -> bytes:
    """Reproduce Node's sha256(`${hostname}-${salt}`) key derivation."""

    machine_name = hostname if hostname is not None else socket.gethostname()
    return hashlib.sha256(f"{machine_name}-{_KEY_SALT}".encode("utf-8")).digest()


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def is_encrypted(value: str) -> bool:
    """Return whether *value* is a supported encrypted token.

    Both the new ``enc:v1:`` token and the legacy unprefixed Node.js three-part
    AES-256-GCM token are accepted.
    """

    if not isinstance(value, str):
        return False
    if value.startswith(ENCRYPTION_PREFIX):
        # A damaged versioned value is still encrypted data. Treating it as
        # plaintext would silently encrypt the corruption a second time.
        return True
    if not _THREE_PART_TOKEN.fullmatch(value):
        return False
    try:
        iv_raw, auth_tag_raw, ciphertext_raw = value.split(".")
        return (
            len(_b64url_decode(iv_raw)) == 12
            and len(_b64url_decode(auth_tag_raw)) == 16
            and isinstance(_b64url_decode(ciphertext_raw), bytes)
        )
    except (ValueError, TypeError):
        return False


def encrypt_secret(plaintext: str) -> str:
    """Encrypt text using AES-256-GCM and return a versioned token.

    The payload remains machine-bound, matching the behavior of the Node.js
    implementation. The three components are IV, authentication tag and
    ciphertext, all encoded using unpadded base64url.
    """

    if not isinstance(plaintext, str):
        raise TypeError("plaintext must be a string")

    iv = os.urandom(12)
    encrypted_and_tag = AESGCM(_derive_key()).encrypt(iv, plaintext.encode("utf-8"), None)
    ciphertext, auth_tag = encrypted_and_tag[:-16], encrypted_and_tag[-16:]
    payload = ".".join(
        (_b64url_encode(iv), _b64url_encode(auth_tag), _b64url_encode(ciphertext))
    )
    return f"{ENCRYPTION_PREFIX}{payload}"


def decrypt_secret(token: str) -> str | None:
    """Decrypt a new or legacy token, returning ``None`` on any failure."""

    if not is_encrypted(token):
        return None

    payload = token[len(ENCRYPTION_PREFIX) :] if token.startswith(ENCRYPTION_PREFIX) else token
    try:
        iv_raw, auth_tag_raw, ciphertext_raw = payload.split(".")
        iv = _b64url_decode(iv_raw)
        auth_tag = _b64url_decode(auth_tag_raw)
        ciphertext = _b64url_decode(ciphertext_raw)
        if len(iv) != 12 or len(auth_tag) != 16:
            return None
        plaintext = AESGCM(_derive_key()).decrypt(iv, ciphertext + auth_tag, None)
        return plaintext.decode("utf-8")
    except (InvalidTag, UnicodeDecodeError, ValueError, TypeError):
        return None


def decrypt_secret_or_raise(token: str) -> str:
    """Decrypt *token* or raise a user-facing configuration error."""

    plaintext = decrypt_secret(token)
    if plaintext is None:
        raise SecretDecryptionError(
            "密码解密失败。配置可能来自另一台电脑或已损坏，请运行 config 重新设置密码。"
        )
    return plaintext


__all__ = [
    "ENCRYPTION_PREFIX",
    "SecretDecryptionError",
    "decrypt_secret",
    "decrypt_secret_or_raise",
    "encrypt_secret",
    "is_encrypted",
]
