from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main.builder import configured_values
from main.cli import _make_services
from main.config import (
    ConfigError,
    Settings,
    load_course_list,
    parse_env_file,
    save_settings,
    settings_from_mapping,
)
from main.http import DEFAULT_BASE_URL


class ConfigTests(unittest.TestCase):
    def test_rejects_invalid_polling_values(self) -> None:
        with self.assertRaises(ConfigError):
            settings_from_mapping({"POLL_INTERVAL_MS": "299"})

    def test_legacy_unused_keys_are_not_persisted(self) -> None:
        settings = settings_from_mapping(
            {
                "POLL_INTERVAL_MS": "800",
                "MIN_INTERVAL_MS": "300",
                "MAX_INTERVAL_MS": "5000",
                "SETUP_DONE": "true",
                "TERM": "2025-9999-1",
                "JW_BASE_URL": "not-a-valid-url",
            }
        )
        values = settings.to_env(encrypt_password=False)
        self.assertNotIn("MIN_INTERVAL_MS", values)
        self.assertNotIn("MAX_INTERVAL_MS", values)
        self.assertNotIn("SETUP_DONE", values)
        self.assertNotIn("TERM", values)
        self.assertNotIn("JW_BASE_URL", values)

    def test_rejects_course_name_alignment_errors(self) -> None:
        with self.assertRaises(ConfigError):
            settings_from_mapping(
                {
                    "TARGET_COURSE_IDS": "A,B,C",
                    "TARGET_COURSE_NAMES": "课程A,课程C",
                }
            )

    def test_save_encrypts_password_and_can_be_loaded_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env.local"
            original = Settings(
                student_id="2024000001",
                password="Password.123",
            )
            save_settings(original, path)
            raw = parse_env_file(path)
            self.assertTrue(raw["PASSWORD"].startswith("enc:v2:dpapi:"))
            loaded = settings_from_mapping(raw)
            self.assertEqual(loaded.password, original.password)

    def test_loaded_v1_password_is_saved_as_v2_dpapi(self) -> None:
        legacy_token = "enc:v1:legacy.token.value"
        current_token = "enc:v2:dpapi:migrated-token"

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env.local"
            with (
                patch(
                    "main.config.decrypt_secret_or_raise",
                    return_value="legacy-password",
                ) as decrypt,
                patch("main.config.encrypt_secret", return_value=current_token) as encrypt,
            ):
                loaded = settings_from_mapping({"PASSWORD": legacy_token})
                save_settings(loaded, path)

            raw = parse_env_file(path)
            self.assertEqual(raw["PASSWORD"], current_token)
            decrypt.assert_called_once_with(legacy_token)
            encrypt.assert_called_once_with("legacy-password")

    def test_to_env_migrates_direct_legacy_tokens(self) -> None:
        current_token = "enc:v2:dpapi:migrated-token"
        legacy_tokens = (
            "enc:v1:legacy.token.value",
            "AAAAAAAAAAAAAAAA.AAAAAAAAAAAAAAAAAAAAAA.AA",
        )

        for legacy_token in legacy_tokens:
            with self.subTest(legacy_token=legacy_token):
                with (
                    patch(
                        "main.config.decrypt_secret_or_raise",
                        return_value="legacy-password",
                    ) as decrypt,
                    patch(
                        "main.config.encrypt_secret", return_value=current_token
                    ) as encrypt,
                ):
                    values = Settings(password=legacy_token).to_env()

                self.assertEqual(values["PASSWORD"], current_token)
                decrypt.assert_called_once_with(legacy_token)
                encrypt.assert_called_once_with("legacy-password")

    def test_to_env_keeps_current_dpapi_token(self) -> None:
        current_token = "enc:v2:dpapi:current-token"

        with (
            patch("main.config.decrypt_secret_or_raise") as decrypt,
            patch("main.config.encrypt_secret") as encrypt,
        ):
            values = Settings(password=current_token).to_env()

        self.assertEqual(values["PASSWORD"], current_token)
        decrypt.assert_not_called()
        encrypt.assert_not_called()

    def test_duplicate_course_id_with_different_names_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "courses.json"
            path.write_text(
                json.dumps(
                    [
                        {"id": "SAME", "name": "课程A"},
                        {"id": "SAME", "name": "课程B"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_course_list(path)

    def test_configured_build_keeps_complete_config_and_plaintext_password(self) -> None:
        original = Settings(
            student_id="2024000001",
            password="build-password",
            jw_user_agent="test-agent",
        )
        builtin = configured_values(original.to_env(encrypt_password=True))
        self.assertEqual(builtin["PASSWORD"], "build-password")
        self.assertEqual(builtin["JW_USER_AGENT"], "test-agent")
        self.assertNotIn("JW_BASE_URL", builtin)

    def test_cli_services_always_use_builtin_base_url(self) -> None:
        session, _selector = _make_services(
            Settings(student_id="TEST-STUDENT", password="test-password")
        )

        self.assertEqual(session.client.base_url, DEFAULT_BASE_URL)


if __name__ == "__main__":
    unittest.main()
