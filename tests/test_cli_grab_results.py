from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from main.cli import _display_grab_results
from main.models import GrabTaskState, GrabTaskStatus


class CliGrabResultTests(unittest.TestCase):
    def test_already_enrolled_counts_as_satisfied_target(self) -> None:
        states = (
            GrabTaskState("CLASS-A", "课程A", GrabTaskStatus.ALREADY_ENROLLED, 1),
            GrabTaskState("CLASS-B", "课程B", GrabTaskStatus.SUCCESS, 1),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            patch("main.ui._log_timestamp", return_value="2026/8/7 22:00:00"),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            satisfied = _display_grab_results(states, ())

        self.assertEqual(satisfied, 2)
        self.assertIn("课程A（系统已选）", stdout.getvalue())
        self.assertIn("课程B", stdout.getvalue())
        self.assertNotIn("未计入", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
