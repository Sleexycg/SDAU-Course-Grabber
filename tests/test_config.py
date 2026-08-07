from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from main.builder import configured_values
from main.config import (
    ConfigError,
    Settings,
    load_course_list,
    parse_env_file,
    save_settings,
    settings_from_mapping,
)


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
            }
        )
        values = settings.to_env(encrypt_password=False)
        self.assertNotIn("MIN_INTERVAL_MS", values)
        self.assertNotIn("MAX_INTERVAL_MS", values)
        self.assertNotIn("SETUP_DONE", values)
        self.assertNotIn("TERM", values)

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
            self.assertTrue(raw["PASSWORD"].startswith("enc:v1:"))
            loaded = settings_from_mapping(raw)
            self.assertEqual(loaded.password, original.password)

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


if __name__ == "__main__":
    unittest.main()
