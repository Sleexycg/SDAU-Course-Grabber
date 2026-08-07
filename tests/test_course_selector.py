from __future__ import annotations

import json
from contextlib import contextmanager
import threading
import unittest
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from main.course_selector import CourseSelector, format_enrolled_result
from main.errors import (
    BadRequestError,
    JwUnavailableError,
    RateLimitedError,
    RegistrationOutcomeUnknown,
    ResponseFormatError,
    SelectionPeriodNotFoundError,
    UnauthorizedError,
)
from main.http import HttpResponse
from main.models import (
    CourseMeeting,
    EnrolledCourse,
    EnrolledCourseResult,
    EnrolledSummary,
    RegisterResultCode,
)
from main.ui import visual_width


PERIOD_HTML = """
<table>
  <tr>
    <td>2026-2027学年 第1学期</td>
    <td><a href="/jsxsd/xsxk/enter?id=PERIOD-1">进入选课</a></td>
  </tr>
</table>
"""


@dataclass
class FakeClient:
    base_url: str = "https://jw.sdau.edu.cn"


class FakeSession:
    def __init__(self, bodies: list[object]) -> None:
        self.client = FakeClient()
        self.auth_generation = 1
        self.bodies = bodies
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.refreshes = 0
        self._auth_lock = threading.RLock()

    @contextmanager
    def auth_transaction(self):  # type: ignore[no-untyped-def]
        with self._auth_lock:
            yield

    def request(self, path: str, **kwargs: Any) -> HttpResponse:
        with self._auth_lock:
            self.calls.append((path, kwargs))
            body = self.bodies.pop(0)
            if isinstance(body, BaseException):
                raise body
            if isinstance(body, HttpResponse):
                return body
            assert isinstance(body, str)
            return HttpResponse(200, body, urljoin(f"{self.client.base_url}/", path))

    def refresh_if_generation(self, observed_generation: int) -> int:
        with self._auth_lock:
            if self.auth_generation == observed_generation:
                self.refreshes += 1
                self.auth_generation += 1
            return self.auth_generation


class CoordinatedFakeSession(FakeSession):
    """Expose the return-time window after entering the first period."""

    def __init__(self, bodies: list[object]) -> None:
        super().__init__(bodies)
        self.enter_response_ready = threading.Event()
        self.allow_enter_return = threading.Event()
        self.refresh_attempted = threading.Event()
        self.refresh_completed = threading.Event()
        self._pause_first_enter = True

    def request(self, path: str, **kwargs: Any) -> HttpResponse:
        response = super().request(path, **kwargs)
        if self._pause_first_enter and path.startswith("/jsxsd/xsxk/enter?"):
            self._pause_first_enter = False
            self.enter_response_ready.set()
            if not self.allow_enter_return.wait(2):
                raise TimeoutError("test did not release the period response")
        return response

    def refresh_if_generation(self, observed_generation: int) -> int:
        self.refresh_attempted.set()
        result = super().refresh_if_generation(observed_generation)
        self.refresh_completed.set()
        return result


def enrolled(
    *,
    weekday: int = 1,
    name: str = "同名课程",
    teacher: str = "同一教师",
    location: str = "教室",
    credit: str = "2",
    course_type: str = "任选",
    meetings: tuple[CourseMeeting, ...] | None = None,
) -> EnrolledCourse:
    resolved_meetings = meetings
    if resolved_meetings is None:
        weekday_labels = ("", "周一", "周二", "周三", "周四", "周五", "周六", "周日")
        resolved_meetings = (
            (CourseMeeting(f"{weekday_labels[weekday]} 1-2节" if weekday else "", location),)
            if weekday or location
            else ()
        )
    return EnrolledCourse(
        name=name,
        teacher=teacher,
        credit=credit,
        course_type=course_type,
        meetings=resolved_meetings,
    )


class CourseSelectorTests(unittest.TestCase):
    def test_query_enrolled_parses_schedule_suffix(self) -> None:
        payload = {
            "code": "0",
            "data": [
                {
                    "kc_mc": "数据库",
                    "xm": "张老师",
                    "xf": "3.5",
                    "sksj": "星期一 0102节(1-16周)\n星期四 0506节",
                    "skdd": "1号楼101<br>1号楼101",
                    "kclb_mc": "必修",
                }
            ],
        }
        selector = CourseSelector(FakeSession([json.dumps(payload, ensure_ascii=False)]))
        result = selector.query_enrolled_courses("2026-2027-1")
        self.assertEqual(result.summary.total_credits, 3.5)
        meetings = result.courses[0].meetings
        self.assertEqual(len(meetings), 2)
        self.assertEqual([meeting.time for meeting in meetings], ["周一 1-2节", "周四 5-6节"])
        self.assertEqual([meeting.location for meeting in meetings], ["1号楼101"] * 2)

    def test_meeting_pairing_preserves_positions_and_broadcasts_one_location(self) -> None:
        payload = {
            "code": 0,
            "data": [
                {
                    "kc_mc": "同地点课程",
                    "sksj": "星期二 0910节\n星期四 0910节",
                    "skdd": "S123",
                },
                {
                    "kc_mc": "保留空行课程",
                    "sksj": "星期一 0102节\n\n星期三 0304节",
                    "skdd": "A101\nB202\nC303",
                },
                {"kc_mc": "仅有地点", "skdd": "D404"},
                {"kc_mc": "安排待定"},
            ],
        }
        selector = CourseSelector(FakeSession([json.dumps(payload, ensure_ascii=False)]))
        courses = selector.query_enrolled_courses("2026-2027-1").courses

        self.assertEqual(
            [(meeting.time, meeting.location) for meeting in courses[0].meetings],
            [("周二 9-10节", "S123"), ("周四 9-10节", "S123")],
        )
        self.assertEqual(
            [(meeting.time, meeting.location) for meeting in courses[1].meetings],
            [("周一 1-2节", "A101"), ("", "B202"), ("周三 3-4节", "C303")],
        )
        self.assertEqual(
            [(meeting.time, meeting.location) for meeting in courses[2].meetings],
            [("", "D404")],
        )
        self.assertEqual(courses[3].meetings, ())

    def test_register_requires_period_and_disables_all_automatic_replay(self) -> None:
        session = FakeSession(
            [
                PERIOD_HTML,
                "<html>选课页面</html>",
                json.dumps(
                    {"success": False, "message": "已选择其它教学班"},
                    ensure_ascii=False,
                ),
            ]
        )
        selector = CourseSelector(session)  # type: ignore[arg-type]
        selector.prepare_selection("2026-2027-1")
        result = selector.register_course("CLASS-1")
        self.assertFalse(result.success)
        self.assertEqual(result.code, RegisterResultCode.ALREADY_ENROLLED)
        self.assertFalse(session.calls[-1][1]["retry_unauthorized"])
        self.assertFalse(session.calls[-1][1]["replay_safe"])
        self.assertEqual(session.calls[-1][1]["expected_generation"], 1)

    def test_code_200_with_full_message_is_not_success(self) -> None:
        session = FakeSession(
            [
                PERIOD_HTML,
                "<html>选课页面</html>",
                json.dumps({"code": 200, "message": "人数已满"}, ensure_ascii=False),
            ]
        )
        selector = CourseSelector(session)  # type: ignore[arg-type]
        selector.prepare_selection("2026-2027-1")
        result = selector.register_course("CLASS-1")
        self.assertFalse(result.success)
        self.assertEqual(result.code, RegisterResultCode.COURSE_FULL)

    def test_new_generation_reenters_original_period_only_once(self) -> None:
        success_body = json.dumps(
            {"success": True, "message": "选课成功"}, ensure_ascii=False
        )
        session = FakeSession(
            [
                PERIOD_HTML,
                "<html>选课页面：首次进入</html>",
                PERIOD_HTML,
                "<html>选课页面：重新登录后再次进入</html>",
                success_body,
                success_body,
            ]
        )
        selector = CourseSelector(session)  # type: ignore[arg-type]
        selector.prepare_selection("2026-2027-1")
        session.auth_generation += 1
        self.assertTrue(selector.register_course("CLASS-1").success)
        self.assertTrue(selector.register_course("CLASS-2").success)

        paths = [path for path, _ in session.calls]
        self.assertEqual(paths.count("/jsxsd/xsxk/xklc_list"), 2)
        self.assertEqual(paths.count("/jsxsd/xsxk/enter?id=PERIOD-1"), 2)

    def test_register_with_refresh_reenters_period_and_retries_once(self) -> None:
        success_body = json.dumps(
            {"success": True, "message": "选课成功"}, ensure_ascii=False
        )
        session = FakeSession(
            [
                PERIOD_HTML,
                "<html>选课页面</html>",
                UnauthorizedError("expired"),
                PERIOD_HTML,
                "<html>选课页面</html>",
                success_body,
            ]
        )
        selector = CourseSelector(session)  # type: ignore[arg-type]
        selector.prepare_selection("2026-2027-1")

        result = selector.register_course_with_refresh("CLASS-1")

        self.assertTrue(result.success)
        self.assertEqual(session.refreshes, 1)
        register_calls = [
            kwargs
            for path, kwargs in session.calls
            if path.startswith("/xsxk/newXsxkzx?")
        ]
        self.assertEqual(len(register_calls), 2)
        self.assertEqual(
            [call["expected_generation"] for call in register_calls],
            [1, 2],
        )
        self.assertTrue(all(not call["retry_unauthorized"] for call in register_calls))
        self.assertTrue(all(not call["replay_safe"] for call in register_calls))

    def test_refresh_cannot_split_period_entry_from_generation_recording(self) -> None:
        success_body = json.dumps(
            {"success": True, "message": "选课成功"}, ensure_ascii=False
        )
        session = CoordinatedFakeSession(
            [
                PERIOD_HTML,
                "<html>选课页面：首次进入</html>",
                PERIOD_HTML,
                "<html>选课页面：刷新后重新进入</html>",
                success_body,
            ]
        )
        selector = CourseSelector(session)  # type: ignore[arg-type]
        prepare_errors: list[BaseException] = []

        def prepare() -> None:
            try:
                selector.prepare_selection("2026-2027-1")
            except BaseException as error:  # pragma: no cover - surfaced below
                prepare_errors.append(error)

        prepare_thread = threading.Thread(target=prepare)
        prepare_thread.start()
        self.assertTrue(session.enter_response_ready.wait(1))

        refresh_thread = threading.Thread(
            target=session.refresh_if_generation,
            args=(1,),
        )
        refresh_thread.start()
        self.assertTrue(session.refresh_attempted.wait(1))
        self.assertFalse(
            session.refresh_completed.wait(0.05),
            "refresh entered between period response and generation recording",
        )

        session.allow_enter_return.set()
        prepare_thread.join(1)
        refresh_thread.join(1)
        self.assertFalse(prepare_thread.is_alive())
        self.assertFalse(refresh_thread.is_alive())
        self.assertEqual(prepare_errors, [])
        self.assertTrue(session.refresh_completed.is_set())
        self.assertEqual(session.auth_generation, 2)

        result = selector.register_course("CLASS-1")
        self.assertTrue(result.success)
        paths = [path for path, _ in session.calls]
        self.assertEqual(paths.count("/jsxsd/xsxk/xklc_list"), 2)
        self.assertEqual(paths.count("/jsxsd/xsxk/enter?id=PERIOD-1"), 2)
        register_kwargs = next(
            kwargs
            for path, kwargs in session.calls
            if path.startswith("/xsxk/newXsxkzx?")
        )
        self.assertEqual(register_kwargs["expected_generation"], 2)
        self.assertFalse(register_kwargs["retry_unauthorized"])
        self.assertFalse(register_kwargs["replay_safe"])

    def test_register_with_refresh_propagates_second_unauthorized(self) -> None:
        session = FakeSession(
            [
                PERIOD_HTML,
                "<html>选课页面</html>",
                UnauthorizedError("first"),
                PERIOD_HTML,
                "<html>选课页面</html>",
                UnauthorizedError("second"),
            ]
        )
        selector = CourseSelector(session)  # type: ignore[arg-type]
        selector.prepare_selection("2026-2027-1")

        with self.assertRaises(UnauthorizedError):
            selector.register_course_with_refresh("CLASS-1")

        self.assertEqual(session.refreshes, 1)
        self.assertEqual(
            sum(path.startswith("/xsxk/newXsxkzx?") for path, _ in session.calls),
            2,
        )

    def test_registration_transport_failures_have_unknown_outcome(self) -> None:
        failures = (
            JwUnavailableError("offline"),
            RateLimitedError(),
            BadRequestError("cross-origin redirect"),
            BadRequestError("request timeout", status=408),
            RuntimeError("response body interrupted"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                session = FakeSession(
                    [PERIOD_HTML, "<html>选课页面</html>", failure]
                )
                selector = CourseSelector(session)  # type: ignore[arg-type]
                selector.prepare_selection("2026-2027-1")
                with self.assertRaises(RegistrationOutcomeUnknown):
                    selector.register_course("CLASS-1")

    def test_registration_definite_client_error_is_not_outcome_unknown(self) -> None:
        session = FakeSession(
            [
                PERIOD_HTML,
                "<html>选课页面</html>",
                BadRequestError("invalid course", status=400),
            ]
        )
        selector = CourseSelector(session)  # type: ignore[arg-type]
        selector.prepare_selection("2026-2027-1")

        with self.assertRaises(BadRequestError) as raised:
            selector.register_course("CLASS-1")

        self.assertEqual(raised.exception.status, 400)

    def test_registration_unrecognized_responses_have_unknown_outcome(self) -> None:
        responses = (
            "not json",
            "[]",
            "{}",
            '{"message":"接口已变化"}',
            '{"success":true,"message":"人数已满"}',
            '{"code":"MAYBE","message":"选课成功"}',
        )
        for response in responses:
            with self.subTest(response=response):
                session = FakeSession(
                    [PERIOD_HTML, "<html>选课页面</html>", response]
                )
                selector = CourseSelector(session)  # type: ignore[arg-type]
                selector.prepare_selection("2026-2027-1")
                with self.assertRaises(RegistrationOutcomeUnknown):
                    selector.register_course("CLASS-1")

    def test_explicit_registration_failure_overrides_success_message(self) -> None:
        responses = (
            {"success": False, "message": "选课成功"},
            {"success": 0, "message": "选择成功"},
            {"success": "false", "message": "选课成功"},
            {"success": True, "code": 500, "message": "选课成功"},
            {"success": False, "code": "MAYBE", "message": "选课成功"},
            {"success": "MAYBE", "code": 500, "message": "选课成功"},
        )
        for payload in responses:
            with self.subTest(payload=payload):
                session = FakeSession(
                    [
                        PERIOD_HTML,
                        "<html>选课页面</html>",
                        json.dumps(payload, ensure_ascii=False),
                    ]
                )
                selector = CourseSelector(session)  # type: ignore[arg-type]
                selector.prepare_selection("2026-2027-1")
                result = selector.register_course("CLASS-1")
                self.assertFalse(result.success)

    def test_query_rejects_explicit_failure_and_blank_course_name(self) -> None:
        payloads = (
            {"success": False, "message": "查询失败", "data": []},
            {"code": 0, "data": [{}]},
            {"code": 0, "data": [{"kc_mc": "   "}]},
        )
        expected_errors = (JwUnavailableError, ResponseFormatError, ResponseFormatError)
        for payload, expected_error in zip(payloads, expected_errors, strict=True):
            with self.subTest(payload=payload):
                selector = CourseSelector(
                    FakeSession([json.dumps(payload, ensure_ascii=False)])
                )
                with self.assertRaises(expected_error):
                    selector.query_enrolled_courses("2026-2027-1")

    def test_enter_period_requires_positive_anchor_and_matching_period(self) -> None:
        invalid_responses = (
            HttpResponse(
                200,
                "<html>选课系统维护中</html>",
                "https://jw.sdau.edu.cn/jsxsd/xsxk/enter?id=PERIOD-1",
            ),
            HttpResponse(
                200,
                "<html>选课页面 2025-2026-1</html>",
                "https://jw.sdau.edu.cn/jsxsd/xsxk/enter?id=PERIOD-1",
            ),
            HttpResponse(
                200,
                "<html>选课页面</html>",
                "https://jw.sdau.edu.cn/jsxsd/xsxk/enter?id=OTHER",
            ),
        )
        for response in invalid_responses:
            with self.subTest(response=response):
                selector = CourseSelector(FakeSession([PERIOD_HTML, response]))
                with self.assertRaises(SelectionPeriodNotFoundError):
                    selector.prepare_selection("2026-2027-1")

    def test_ambiguous_periods_fail_closed(self) -> None:
        ambiguous = PERIOD_HTML.replace(
            "</table>",
            '<tr><td>2026-2027学年 第1学期</td>'
            '<td><a href="/jsxsd/xsxk/enter?id=PERIOD-2">进入选课</a></td></tr>'
            "</table>",
        )
        selector = CourseSelector(FakeSession([ambiguous]))  # type: ignore[arg-type]
        with self.assertRaises(SelectionPeriodNotFoundError):
            selector.prepare_selection("2026-2027-1")

    def test_formatting_keeps_distinct_same_name_rows(self) -> None:
        result = EnrolledCourseResult(
            term="2026-2027-1",
            courses=(enrolled(), enrolled(weekday=3)),
            summary=EnrolledSummary(total_courses=2, total_credits=4),
        )
        self.assertEqual(format_enrolled_result(result).count("同名课程"), 2)

    def test_formatting_empty_result_is_concise(self) -> None:
        result = EnrolledCourseResult(
            term="2026-2027-1",
            courses=(),
            summary=EnrolledSummary(total_courses=0, total_credits=0),
        )
        self.assertEqual(format_enrolled_result(result), "暂无已选课程")

    def test_formatting_groups_credits_and_aligns_chinese_columns(self) -> None:
        result = EnrolledCourseResult(
            term="2026-2027-1",
            courses=(
                enrolled(
                    name="软件工程",
                    credit="3",
                    course_type="限选",
                    meetings=(
                        CourseMeeting("周二 9-10节", "S123"),
                        CourseMeeting("周四 9-10节", "S123"),
                    ),
                ),
                enrolled(
                    name="操作系统实验",
                    teacher="郝霞",
                    location="图信楼413",
                    credit="0.5",
                    course_type="必修",
                    weekday=4,
                ),
                enrolled(
                    name="形势与政策5",
                    teacher="刘成峰",
                    location="",
                    credit="0",
                    course_type="必选",
                    weekday=0,
                ),
                enrolled(
                    name="通识课",
                    teacher="",
                    location="",
                    credit="invalid",
                    course_type="任选",
                    weekday=0,
                ),
            ),
            summary=EnrolledSummary(total_courses=4, total_credits=3.5),
        )

        formatted = format_enrolled_result(result)
        captions = (
            "必修（2门-0.5学分）",
            "限选（1门-3.0学分）",
            "任选（1门-0.0学分）",
        )
        for caption in captions:
            self.assertIn(caption, formatted)
        self.assertLess(formatted.index(captions[0]), formatted.index(captions[1]))
        self.assertLess(formatted.index(captions[1]), formatted.index(captions[2]))
        self.assertIn("教师待定", formatted)
        self.assertIn("时间待定", formatted)
        self.assertIn("地点待定", formatted)
        self.assertIn("周二 9-10节", formatted)
        self.assertIn("周四 9-10节", formatted)
        self.assertEqual(formatted.count("S123"), 2)

        rendered_lines = formatted.splitlines()
        first_meeting = next(
            index for index, line in enumerate(rendered_lines) if "周二 9-10节" in line
        )
        self.assertIn("软件工程", rendered_lines[first_meeting])
        self.assertIn("S123", rendered_lines[first_meeting])
        self.assertNotIn("软件工程", rendered_lines[first_meeting + 1])
        self.assertIn("周四 9-10节", rendered_lines[first_meeting + 1])
        self.assertIn("S123", rendered_lines[first_meeting + 1])

        self.assertNotIn("序号", formatted)
        self.assertNotIn("课程名称", formatted)
        self.assertNotIn("│", formatted)
        self.assertNotIn("┼", formatted)
        self.assertNotIn(result.term, formatted)
        for line in rendered_lines:
            self.assertEqual(line, line.rstrip())

        section_lines = [line for line in rendered_lines if "─" in line]
        self.assertEqual(len(section_lines), len(captions))
        self.assertEqual(len({visual_width(line) for line in section_lines}), 1)
        for line, caption in zip(section_lines, captions, strict=True):
            left, right = line.split(caption, 1)
            self.assertGreaterEqual(left.count("─"), 8)
            self.assertGreaterEqual(right.count("─"), 8)
            self.assertLessEqual(abs(left.count("─") - right.count("─")), 1)

        data_rows = [
            next(line for line in rendered_lines if course_name in line)
            for course_name in ("软件工程", "操作系统实验", "形势与政策5", "通识课")
        ]
        for tokens in (
            ("3.0分", "0.5分", "0.0分", "0.0分"),
            ("同一教师", "郝霞", "刘成峰", "教师待定"),
            ("周二 9-10节", "周四 1-2节", "时间待定", "时间待定"),
            ("S123", "图信楼413", "地点待定", "地点待定"),
        ):
            starts = [
                visual_width(line[: line.index(token)])
                for line, token in zip(data_rows, tokens, strict=True)
            ]
            self.assertEqual(len(set(starts)), 1)

    def test_course_columns_appear_on_first_meeting_row(self) -> None:
        for meeting_count in (2, 3, 4):
            with self.subTest(meeting_count=meeting_count):
                meetings = tuple(
                    CourseMeeting(f"安排{index + 1}", f"教室{index + 1}")
                    for index in range(meeting_count)
                )
                result = EnrolledCourseResult(
                    term="2026-2027-1",
                    courses=(
                        enrolled(
                            name="垂直居中课程",
                            credit="2",
                            course_type="必修",
                            meetings=meetings,
                        ),
                    ),
                    summary=EnrolledSummary(total_courses=1, total_credits=2),
                )

                data_lines = [
                    line
                    for line in format_enrolled_result(result).splitlines()
                    if "安排" in line
                ]
                self.assertEqual(len(data_lines), meeting_count)
                label_rows = [
                    index
                    for index, line in enumerate(data_lines)
                    if "垂直居中课程" in line
                ]
                self.assertEqual(label_rows, [0])
                self.assertIn("2.0分", data_lines[0])
                self.assertIn("同一教师", data_lines[0])
                for continuation in data_lines[1:]:
                    self.assertNotIn("2.0分", continuation)
                    self.assertNotIn("同一教师", continuation)
                time_starts = [
                    visual_width(line[: line.index(f"安排{index + 1}")])
                    for index, line in enumerate(data_lines)
                ]
                self.assertEqual(len(set(time_starts)), 1)

    def test_required_and_limited_courses_are_sorted_with_pending_last(self) -> None:
        def scheduled(name: str, course_type: str, *times: str) -> EnrolledCourse:
            return enrolled(
                name=name,
                course_type=course_type,
                meetings=tuple(CourseMeeting(time, "教室") for time in times),
            )

        courses = (
            enrolled(name="必修待定", course_type="必修", meetings=()),
            scheduled("必修周五", "必修", "周五 1-2节"),
            scheduled("必修周一三四", "必修", "周一 3-4节"),
            scheduled("必修周六", "必修", "周六 1-2节"),
            scheduled("必修周一一二", "必选", "周一 1-2节"),
            enrolled(name="限选待定", course_type="限选", meetings=()),
            scheduled("限选周三", "限选", "周三 5-6节"),
            scheduled("限选多时间", "限选", "周四 3-4节", "周二 3-4节"),
        )
        formatted = format_enrolled_result(
            EnrolledCourseResult(
                term="2026-2027-1",
                courses=courses,
                summary=EnrolledSummary(total_courses=len(courses), total_credits=16),
            )
        )

        required = formatted.split("必修（", 1)[1].split("限选（", 1)[0]
        required_order = (
            "必修周一一二",
            "必修周一三四",
            "必修周五",
            "必修周六",
            "必修待定",
        )
        self.assertEqual(
            sorted(required_order, key=required.index),
            list(required_order),
        )

        limited = formatted.split("限选（", 1)[1]
        limited_order = ("限选多时间", "限选周三", "限选待定")
        self.assertEqual(
            sorted(limited_order, key=limited.index),
            list(limited_order),
        )


if __name__ == "__main__":
    unittest.main()
