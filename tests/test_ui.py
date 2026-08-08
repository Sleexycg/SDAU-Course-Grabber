from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from main.ui import clear_screen, info, select_term, success, wait_key


class StatusOutputTests(unittest.TestCase):
    def test_info_and_success_use_timestamped_format(self) -> None:
        output = io.StringIO()
        with (
            patch(
                "main.ui._log_timestamp",
                side_effect=["2026/8/7 21:17:43", "2026/8/7 21:17:44"],
            ),
            redirect_stdout(output),
        ):
            info("正在登录教务系统...")
            success("登录成功", check=True)

        self.assertEqual(
            output.getvalue(),
            "[2026/8/7 21:17:43] [INFO] 正在登录教务系统...\n"
            "[2026/8/7 21:17:44] [SUCCESS] 登录成功 ✅\n",
        )


class ScreenClearingTests(unittest.TestCase):
    def test_wait_key_clears_before_returning_to_menu(self) -> None:
        with (
            patch("main.ui.sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value=""),
            patch("main.ui.clear_screen") as mocked_clear,
        ):
            wait_key()

        mocked_clear.assert_called_once_with(force=True)

    def test_noninteractive_wait_does_not_prompt_or_clear(self) -> None:
        with (
            patch("main.ui.sys.stdin.isatty", return_value=False),
            patch("builtins.input") as mocked_input,
            patch("main.ui.clear_screen") as mocked_clear,
        ):
            wait_key()

        mocked_input.assert_not_called()
        mocked_clear.assert_not_called()

    def test_forced_non_windows_clear_erases_scrollback_and_screen(self) -> None:
        output = io.StringIO()
        with (
            patch("main.ui.os.name", "posix"),
            redirect_stdout(output),
        ):
            clear_screen(force=True)

        self.assertEqual(output.getvalue(), "\033[3J\033[2J\033[H")

    def test_windows_terminal_uses_cls(self) -> None:
        with (
            patch("main.ui.os.name", "nt"),
            patch("main.ui.sys.stdout.isatty", return_value=True),
            patch("main.ui.os.system", return_value=0) as mocked_system,
        ):
            clear_screen()

        mocked_system.assert_called_once_with("cls")


class TermSelectorTests(unittest.TestCase):
    @patch("main.ui._read_navigation_key", side_effect=["up", "enter"])
    @patch("main.ui._supports_key_navigation", return_value=True)
    def test_arrow_up_selects_previous_term(self, _supports, _read_key) -> None:
        with redirect_stdout(io.StringIO()):
            selected = select_term("2026-2027-1")
        self.assertEqual(selected, "2025-2026-2")

    @patch("main.ui._read_navigation_key", side_effect=["down", "escape"])
    @patch("main.ui._supports_key_navigation", return_value=True)
    def test_escape_restores_default_term(self, _supports, _read_key) -> None:
        with redirect_stdout(io.StringIO()):
            selected = select_term("2026-2027-1")
        self.assertEqual(selected, "2026-2027-1")

    @patch("main.ui._read_navigation_key", return_value="enter")
    @patch("main.ui._supports_key_navigation", return_value=True)
    def test_only_current_term_is_rendered(self, _supports, _read_key) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            selected = select_term("2026-2027-1")

        self.assertEqual(selected, "2026-2027-1")
        self.assertIn(
            "当前学期：2026-2027-1（通过↑/↓键切换,Enter确认）",
            output.getvalue(),
        )
        self.assertNotIn("2025-2026-2", output.getvalue())
        self.assertNotIn("2026-2027-2", output.getvalue())


if __name__ == "__main__":
    unittest.main()
