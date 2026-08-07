from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from main.cli import run_query
from main.config import Settings
from main.models import EnrolledCourse, EnrolledCourseResult, EnrolledSummary


class _QuerySelector:
    def __init__(self, result: EnrolledCourseResult) -> None:
        self.result = result
        self.requested_term = ""

    def query_enrolled_courses(self, term: str) -> EnrolledCourseResult:
        self.requested_term = term
        return self.result


class CliQueryOutputTests(unittest.TestCase):
    def test_query_logs_progress_and_summary_before_grouped_result(self) -> None:
        result = EnrolledCourseResult(
            term="2099-2100-2",
            courses=(
                EnrolledCourse(
                    name="虚构测试选修课",
                    teacher="测试教师",
                    credit="1",
                    course_type="测试类别",
                ),
            ),
            summary=EnrolledSummary(total_courses=1, total_credits=1),
        )
        selector = _QuerySelector(result)
        settings = Settings(
            student_id="TEST-STUDENT-0001",
            password="TEST-ONLY-NOT-A-REAL-PASSWORD",
        )
        output = io.StringIO()

        with (
            patch("main.cli._login", return_value=True),
            patch("main.cli._make_services", return_value=(object(), selector)),
            patch(
                "main.ui._log_timestamp",
                side_effect=["2099/1/2 03:04:05", "2099/1/2 03:04:06"],
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(run_query(settings, term="2099-2100-2"), 0)

        rendered = output.getvalue()
        self.assertEqual(selector.requested_term, "2099-2100-2")
        self.assertIn(
            "[2099/1/2 03:04:05] [INFO] "
            "正在查询 2099-2100-2 的选课结果...\n",
            rendered,
        )
        self.assertIn(
            "[2099/1/2 03:04:06] [SUCCESS] "
            "查询成功：共 1 门课程，1.0 学分\n",
            rendered,
        )
        self.assertNotIn("查询成功：共 1 门课程，1.0 学分 ✅", rendered)
        self.assertIn("测试类别（1门-1.0学分）", rendered)


if __name__ == "__main__":
    unittest.main()
