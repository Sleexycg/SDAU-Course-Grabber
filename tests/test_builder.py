from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main.builder import build_onefile


class BuilderTests(unittest.TestCase):
    def _build(self, builtin_config: dict[str, str] | None) -> tuple[Path, list[str], dict]:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            observed_command: list[str] = []
            observed_options: dict = {}

            def fake_run(command, **options):
                observed_command.extend(command)
                observed_options.update(options)
                name = command[command.index("--name") + 1]
                suffix = ".exe" if __import__("os").name == "nt" else ""
                output = root / "dist" / f"{name}{suffix}"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.touch()
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch("main.builder.ensure_pyinstaller"),
                patch("main.builder.config_dir", return_value=root),
                patch("main.builder.subprocess.run", side_effect=fake_run),
            ):
                output = build_onefile(builtin_config)
                return Path(output.name), observed_command, observed_options

    def test_blank_build_uses_fixed_name_and_hides_pyinstaller_output(self) -> None:
        output, command, options = self._build(None)
        self.assertEqual(output.name, "course-grabber.exe")
        self.assertEqual(command[command.index("--name") + 1], "course-grabber")
        self.assertTrue(options["capture_output"])
        self.assertIn("ERROR", command)
        self.assertEqual(command[command.index("--optimize") + 1], "2")
        excluded = {
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--exclude-module"
        }
        self.assertEqual(
            excluded,
            {"main.builder", "cryptography", "cffi", "_cffi_backend"},
        )

    def test_configured_build_uses_student_id(self) -> None:
        output, command, _options = self._build(
            {"STUDENT_ID": "2024000001", "PASSWORD": "secret"}
        )
        self.assertEqual(output.name, "2024000001.exe")
        self.assertEqual(command[command.index("--name") + 1], "2024000001")


if __name__ == "__main__":
    unittest.main()
