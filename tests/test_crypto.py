from __future__ import annotations

import unittest

from main.crypto import decrypt_secret, encrypt_secret, is_encrypted


class CryptoTests(unittest.TestCase):
    def test_new_format_round_trip(self) -> None:
        token = encrypt_secret("复杂 Password.123")
        self.assertTrue(token.startswith("enc:v1:"))
        self.assertTrue(is_encrypted(token))
        self.assertEqual(decrypt_secret(token), "复杂 Password.123")

    def test_legacy_node_format_is_supported(self) -> None:
        token = encrypt_secret("legacy-password")
        legacy = token.removeprefix("enc:v1:")
        self.assertTrue(is_encrypted(legacy))
        self.assertEqual(decrypt_secret(legacy), "legacy-password")

    def test_arbitrary_dotted_password_is_not_misclassified(self) -> None:
        self.assertFalse(is_encrypted("abc.def.ghi"))

    def test_corrupt_ciphertext_returns_none(self) -> None:
        self.assertIsNone(decrypt_secret("enc:v1:broken.token.value"))


if __name__ == "__main__":
    unittest.main()
