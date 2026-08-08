from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from main.crypto import (
    ENCRYPTION_PREFIX,
    SecretDecryptionError,
    SecretEncryptionError,
    decrypt_secret,
    decrypt_secret_or_raise,
    encrypt_secret,
    is_encrypted,
)


_LEGACY_VECTOR = (
    "enc:v1:AAECAwQFBgcICQoL.CPS900iyhBlVv9OpABxumQ."
    "99vPQDor4PmlG8pwC2nk8wM7fB9Kjw"
)
_LEGACY_NUL_VECTOR = (
    "enc:v1:AAECAwQFBgcICQoL.ftWZzZdHpOWsFftYQ4xaNA."
    "fSkPxPbUKryLsH2x-Rr28hU"
)
_LEGACY_EMPTY_VECTOR = "enc:v1:AAECAwQFBgcICQoL.kISrB5vO3rz1vXfmjm8BfA."


class CryptoTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "requires Windows DPAPI")
    def test_dpapi_round_trip(self) -> None:
        token = encrypt_secret("复杂 Password.123")
        self.assertTrue(token.startswith(ENCRYPTION_PREFIX))
        self.assertTrue(is_encrypted(token))
        self.assertEqual(decrypt_secret(token), "复杂 Password.123")

    @unittest.skipUnless(sys.platform == "win32", "requires Windows DPAPI")
    def test_dpapi_round_trip_supports_empty_and_unicode(self) -> None:
        for plaintext in ("", "密码🔐 — café\n第二行"):
            with self.subTest(plaintext=plaintext):
                self.assertEqual(decrypt_secret(encrypt_secret(plaintext)), plaintext)

    @unittest.skipUnless(sys.platform == "win32", "requires Windows DPAPI")
    def test_dpapi_tampering_is_rejected(self) -> None:
        token = encrypt_secret("do-not-change")
        payload = token[len(ENCRYPTION_PREFIX) :]
        index = len(payload) // 2
        replacement = "A" if payload[index] != "A" else "B"
        tampered = f"{ENCRYPTION_PREFIX}{payload[:index]}{replacement}{payload[index + 1:]}"
        self.assertTrue(is_encrypted(tampered))
        self.assertIsNone(decrypt_secret(tampered))

    @unittest.skipUnless(sys.platform == "win32", "requires Windows CNG")
    def test_fixed_legacy_node_vector_is_supported(self) -> None:
        with patch("main.crypto.socket.gethostname", return_value="TEST-HOST"):
            self.assertEqual(decrypt_secret(_LEGACY_VECTOR), "旧密码-Password.123")
            self.assertEqual(
                decrypt_secret(_LEGACY_VECTOR.removeprefix("enc:v1:")),
                "旧密码-Password.123",
            )
            self.assertEqual(decrypt_secret(_LEGACY_NUL_VECTOR), "legacy-密码\0end")
            self.assertEqual(decrypt_secret(_LEGACY_EMPTY_VECTOR), "")

    @unittest.skipUnless(sys.platform == "win32", "requires Windows CNG")
    def test_legacy_authentication_tag_tampering_is_rejected(self) -> None:
        tampered = _LEGACY_VECTOR.replace("CPS900", "DPS900")
        with patch("main.crypto.socket.gethostname", return_value="TEST-HOST"):
            self.assertIsNone(decrypt_secret(tampered))

    def test_arbitrary_dotted_password_is_not_misclassified(self) -> None:
        self.assertFalse(is_encrypted("abc.def.ghi"))

    def test_damaged_versioned_tokens_remain_classified_as_encrypted(self) -> None:
        for token in (
            "enc:v2:dpapi:",
            "enc:v2:dpapi:not.valid",
            "enc:v1:broken",
            "enc:v99:future-format",
        ):
            with self.subTest(token=token):
                self.assertTrue(is_encrypted(token))
                self.assertIsNone(decrypt_secret(token))

    def test_non_windows_never_falls_back_to_plaintext(self) -> None:
        with patch("main.crypto._IS_WINDOWS", False):
            with self.assertRaises(SecretEncryptionError):
                encrypt_secret("plain password")
            self.assertIsNone(decrypt_secret(_LEGACY_VECTOR))
            with self.assertRaises(SecretDecryptionError):
                decrypt_secret_or_raise(_LEGACY_VECTOR)

    def test_non_string_plaintext_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            encrypt_secret(None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
