from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main.models import (
    CourseMeeting,
    EnrolledCourse,
    EnrolledCourseResult,
    EnrolledSummary,
)
from main.timetable import (
    TimetableExportError,
    TimetableSlot,
    build_timetable_plan,
    calculate_timetable_layout,
    export_timetable_png,
)


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def course(
    name: str,
    *meetings: CourseMeeting,
    teacher: str = "张老师",
) -> EnrolledCourse:
    return EnrolledCourse(
        name=name,
        teacher=teacher,
        credit="2",
        course_type="必修",
        meetings=tuple(meetings),
    )


def result_with_arrangements() -> EnrolledCourseResult:
    courses = (
        course(
            "数据库",
            CourseMeeting("周一 1-2节", "1号楼101"),
            CourseMeeting("周六 3-4节", "实验室"),
        ),
        course("大学英语", CourseMeeting("周一 1-2节", "2号楼202"), teacher="李老师"),
        course("地点待定课", CourseMeeting("周三 5-6节", "")),
        course("完全待定课", teacher=""),
        course("仅地点待定课", CourseMeeting("", "临时教室")),
        course("原始时间课", CourseMeeting("安排另行通知", "待通知")),
    )
    return EnrolledCourseResult(
        term="2026-2027-1",
        courses=courses,
        summary=EnrolledSummary(total_courses=len(courses), total_credits=12),
    )


class TimetablePlanningTests(unittest.TestCase):
    def test_maps_multiple_arrangements_conflicts_weekend_and_pending(self) -> None:
        plan = build_timetable_plan(result_with_arrangements())

        self.assertEqual(plan.weekdays, (1, 2, 3, 4, 5, 6, 7))
        self.assertEqual(
            plan.slots,
            (TimetableSlot(1, 2), TimetableSlot(3, 4), TimetableSlot(5, 6)),
        )
        monday = plan.entries_at(TimetableSlot(1, 2), 1)
        self.assertEqual([entry.course_name for entry in monday], ["数据库", "大学英语"])
        self.assertEqual([entry.location for entry in monday], ["1号楼101", "2号楼202"])
        saturday = plan.entries_at(TimetableSlot(3, 4), 6)
        self.assertEqual([entry.course_name for entry in saturday], ["数据库"])

        self.assertEqual(
            [entry.course_name for entry in plan.pending],
            ["完全待定课", "仅地点待定课", "原始时间课"],
        )
        self.assertEqual(plan.pending[0].time_label, "时间待定")
        self.assertEqual(plan.pending[1].location, "临时教室")
        self.assertEqual(plan.pending[2].time_label, "安排另行通知")

    def test_workday_only_plan_does_not_add_empty_weekend_columns(self) -> None:
        result = EnrolledCourseResult(
            term="2026-2027-1",
            courses=(course("高等数学", CourseMeeting("周五 9-10节", "教室")),),
            summary=EnrolledSummary(1, 2),
        )
        plan = build_timetable_plan(result)
        self.assertEqual(plan.weekdays, (1, 2, 3, 4, 5))

    def test_layout_is_sorted_bounded_and_expands_for_same_cell_entries(self) -> None:
        plan = build_timetable_plan(result_with_arrangements())
        layout = calculate_timetable_layout(plan)

        self.assertGreater(layout.width, 1000)
        self.assertLess(layout.height, 30_000)
        self.assertEqual([row.slot for row in layout.rows], list(plan.slots))
        self.assertEqual([row.top for row in layout.rows], sorted(row.top for row in layout.rows))
        # The 1-2 cell has two complete course entries and therefore needs more
        # vertical room than the single-entry 3-4 cell.
        self.assertGreater(layout.rows[0].height, layout.rows[1].height)
        self.assertEqual(len(layout.pending_rows), 3)

    def test_invalid_or_reverse_section_range_is_kept_in_pending_area(self) -> None:
        result = EnrolledCourseResult(
            term="2026-2027-1",
            courses=(course("异常安排", CourseMeeting("周二 8-3节", "教室")),),
            summary=EnrolledSummary(1, 2),
        )
        plan = build_timetable_plan(result)
        self.assertFalse(plan.slots)
        self.assertEqual(plan.pending[0].course_name, "异常安排")
        self.assertEqual(plan.pending[0].time_label, "周二 8-3节")


class TimetableExportTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "requires Windows GDI+")
    def test_exports_real_png_with_planned_dimensions(self) -> None:
        result = result_with_arrangements()
        expected = calculate_timetable_layout(build_timetable_plan(result))
        with tempfile.TemporaryDirectory() as temporary_dir:
            output = export_timetable_png(result, output_dir=temporary_dir)
            data = output.read_bytes()

        self.assertEqual(output.name, "课程表-2026-2027-1.png")
        self.assertTrue(data.startswith(PNG_SIGNATURE))
        self.assertGreater(len(data), 1_000)
        self.assertEqual(data[12:16], b"IHDR")
        self.assertEqual(struct.unpack(">II", data[16:24]), (expected.width, expected.height))

    @unittest.skipUnless(sys.platform == "win32", "requires Windows GDI+")
    def test_existing_output_is_not_overwritten(self) -> None:
        result = result_with_arrangements()
        with tempfile.TemporaryDirectory() as temporary_dir:
            requested = Path(temporary_dir) / "my-table.png"
            first = export_timetable_png(result, requested)
            first_bytes = first.read_bytes()
            second = export_timetable_png(result, requested)

            self.assertEqual(first, requested)
            self.assertEqual(second.name, "my-table-2.png")
            self.assertEqual(first.read_bytes(), first_bytes)
            self.assertTrue(second.read_bytes().startswith(PNG_SIGNATURE))

    def test_render_failure_removes_partial_temporary_file(self) -> None:
        result = result_with_arrangements()
        with tempfile.TemporaryDirectory() as temporary_dir:
            requested = Path(temporary_dir) / "failed.png"

            def fail_render(_plan, _layout, temporary: Path) -> None:
                temporary.write_bytes(b"partial")
                raise TimetableExportError("simulated failure")

            with (
                patch("main.timetable._IS_WINDOWS", True),
                patch("main.timetable._render_plan_to_png", side_effect=fail_render),
                self.assertRaisesRegex(TimetableExportError, "simulated failure"),
            ):
                export_timetable_png(result, requested)

            self.assertFalse(requested.exists())
            self.assertEqual(list(Path(temporary_dir).iterdir()), [])

    def test_non_windows_fails_before_creating_plaintext_or_output(self) -> None:
        result = result_with_arrangements()
        with tempfile.TemporaryDirectory() as temporary_dir:
            requested = Path(temporary_dir) / "table.png"
            with (
                patch("main.timetable._IS_WINDOWS", False),
                self.assertRaisesRegex(TimetableExportError, "Windows GDI"),
            ):
                export_timetable_png(result, requested)
            self.assertFalse(requested.exists())

    def test_output_path_and_output_dir_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            with self.assertRaises(ValueError):
                export_timetable_png(
                    result_with_arrangements(),
                    Path(temporary_dir) / "table.png",
                    output_dir=temporary_dir,
                )


if __name__ == "__main__":
    unittest.main()
