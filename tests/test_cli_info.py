from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from main.cli import run_info
from main.config import Settings


class _ProfileResponse:
    text = (
        "<span>学院：虚构测试学院</span>"
        "<span>班级：虚构测试班</span>"
    )


class _ProfileSession:
    def request(self, _path: str) -> _ProfileResponse:
        return _ProfileResponse()


class CliInfoTests(unittest.TestCase):
    def test_personal_information_matches_aligned_layout(self) -> None:
        settings = Settings(
            student_id="TEST-STUDENT-0001",
            password="TEST-ONLY-NOT-A-REAL-PASSWORD",
            target_course_ids=("TEST-CLASS-001", "TEST-CLASS-002", "TEST-CLASS-003"),
            target_course_names=("虚构课程甲", "虚构课程乙", "虚构课程丙"),
        )
        output = io.StringIO()

        with (
            patch("main.cli.clear_screen"),
            patch("main.cli.print_box"),
            patch("main.cli._login", return_value=True),
            patch("main.cli._make_services", return_value=(_ProfileSession(), object())),
            redirect_stdout(output),
        ):
            self.assertEqual(run_info(settings), 0)

        rendered = output.getvalue()
        expected = (
            "  学号：       TEST-STUDENT-0001\n"
            "  学院：       虚构测试学院\n"
            "  班级：       虚构测试班\n"
            "  目标课程：\n"
            "    1. 虚构课程甲（TEST-CLASS-001）\n"
            "    2. 虚构课程乙（TEST-CLASS-002）\n"
            "    3. 虚构课程丙（TEST-CLASS-003）\n"
        )
        self.assertIn(expected, rendered)
        self.assertNotIn("当前默认学期", rendered)
        self.assertNotIn(settings.password, rendered)


if __name__ == "__main__":
    unittest.main()
