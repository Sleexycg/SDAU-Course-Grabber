"""已选课程查询和选课提交协议。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import json
import re
import threading
import unicodedata
from typing import cast
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit

from .errors import (
    BadRequestError,
    JwUnavailableError,
    RateLimitedError,
    RegistrationOutcomeUnknown,
    ResponseFormatError,
    SelectionPeriodNotFoundError,
    UnauthorizedError,
)
from .http import HttpResponse
from .models import (
    CourseMeeting,
    EnrolledCourse,
    EnrolledCourseResult,
    EnrolledSummary,
    RegisterResult,
    RegisterResultCode,
)
from .session import Session
from .term import is_valid_term


_JSON_ACCEPT = "application/json, text/javascript, */*; q=0.01"
_ENROLLED_PATH = "/xkgl/loadXsxkjgList"
_PERIODS_PATH = "/jsxsd/xsxk/xklc_list"
_REGISTER_PATH = "/xsxk/newXsxkzx"

_WEEKDAYS: Mapping[str, int] = {
    "星期一": 1,
    "星期二": 2,
    "星期三": 3,
    "星期四": 4,
    "星期五": 5,
    "星期六": 6,
    "星期日": 7,
}
_WEEKDAY_LABELS = ("", "周一", "周二", "周三", "周四", "周五", "周六", "周日")
_TERM_SEARCH_RE = re.compile(r"(?<!\d)(\d{4}-\d{4}-[12])(?!\d)")
_CHINESE_TERM_RE = re.compile(
    r"(?P<start>\d{4})\s*[-—至]\s*(?P<end>\d{4})\s*学年.*?"
    r"(?:第\s*)?(?P<num>[一二12])\s*学期",
    re.S,
)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_DISPLAY_TIME_RE = re.compile(
    r"^周(?P<weekday>[一二三四五六日])\s*"
    r"(?P<start>\d+)(?:-(?P<end>\d+))?节$"
)


@dataclass(frozen=True, slots=True)
class _Period:
    id: str
    name: str
    url: str
    term: str | None


@dataclass(frozen=True, slots=True)
class _SelectionContext:
    term: str
    period: _Period


class CourseSelector:
    """绑定一个登录会话的最小选课客户端。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._selection_context: _SelectionContext | None = None
        self._selection_generation: int | None = None
        self._selection_lock = threading.RLock()

    def query_enrolled_courses(self, term: str) -> EnrolledCourseResult:
        resolved_term = _require_term(term)
        query = urlencode(
            {
                "lx": "xkrz",
                "type": "list",
                "pageNum": "1",
                "pageSize": "200",
                "xnxqid": resolved_term,
            }
        )
        endpoint = f"{_ENROLLED_PATH}?{query}"
        response = self.session.request(endpoint, accept=_JSON_ACCEPT)
        root = _json_object(response, endpoint=_ENROLLED_PATH, label="选课结果")
        _validate_business_status(root, endpoint=_ENROLLED_PATH, label="选课结果")

        raw_data = root.get("data")
        if not isinstance(raw_data, list):
            raise ResponseFormatError(
                "选课结果接口的 data 字段不是数组；接口结构可能已经改变",
                endpoint=_ENROLLED_PATH,
                response_excerpt=_excerpt(response.text),
            )

        courses: list[EnrolledCourse] = []
        for index, item in enumerate(raw_data):
            if not isinstance(item, dict):
                raise ResponseFormatError(
                    f"选课结果第 {index + 1} 项不是对象；接口结构可能已经改变",
                    endpoint=_ENROLLED_PATH,
                )
            course = _parse_enrolled_course(cast(Mapping[str, object], item))
            if not course.name:
                raise ResponseFormatError(
                    f"选课结果第 {index + 1} 项缺少非空 kc_mc；接口结构可能已经改变",
                    endpoint=_ENROLLED_PATH,
                    response_excerpt=_excerpt(response.text),
                )
            courses.append(course)

        total_credits = sum(_safe_float(course.credit) for course in courses)
        return EnrolledCourseResult(
            term=resolved_term,
            courses=tuple(courses),
            summary=EnrolledSummary(len(courses), total_credits),
        )

    def prepare_selection(self, term: str) -> None:
        """严格匹配并进入一个选课期次。"""

        with self._selection_lock:
            with self.session.auth_transaction():
                self._prepare_locked(_require_term(term), required_period_id=None)

    def register_course(self, course_id: str) -> RegisterResult:
        """Submit once without refreshing or replaying an expired session."""

        normalized_id = course_id.strip()
        if not normalized_id:
            raise BadRequestError("缺少教学班 ID（jx0502zbid）")

        with self._selection_lock:
            with self.session.auth_transaction():
                return self._register_once_locked(normalized_id)

    def register_course_with_refresh(self, course_id: str) -> RegisterResult:
        """Submit, then refresh the period and retry once only after a 401."""

        normalized_id = course_id.strip()
        if not normalized_id:
            raise BadRequestError("缺少教学班 ID（jx0502zbid）")

        # Keep the complete authentication recovery transaction under the same
        # lock as every registration.  No other worker can submit between login,
        # restoring the original period and the one permitted retry.
        with self._selection_lock:
            with self.session.auth_transaction():
                try:
                    return self._register_once_locked(normalized_id)
                except UnauthorizedError:
                    failed_generation = self.session.auth_generation
                    self.session.refresh_if_generation(failed_generation)
                    return self._register_once_locked(normalized_id)

    def _register_once_locked(self, normalized_id: str) -> RegisterResult:
        self._restore_selection_locked()
        context = self._selection_context
        if context is None:  # only helps the type checker
            raise BadRequestError("提交选课前必须先准备选课期次")

        generation = self.session.auth_generation
        endpoint = f"{_REGISTER_PATH}?{urlencode({'jx0502zbid': normalized_id, 'isallsc': '1'})}"
        try:
            response = self.session.request(
                endpoint,
                referer=urljoin(f"{self.session.client.base_url}/", context.period.url),
                accept=_JSON_ACCEPT,
                retry_unauthorized=False,
                replay_safe=False,
                expected_generation=generation,
            )
        except UnauthorizedError:
            raise
        except (JwUnavailableError, RateLimitedError) as error:
            raise RegistrationOutcomeUnknown(
                "选课提交可能已经到达服务器，但未能确认结果；已停止继续提交",
                endpoint=_REGISTER_PATH,
            ) from error
        except BadRequestError as error:
            # A status-less BadRequest from this fixed same-origin endpoint is a
            # rejected cross-origin redirect.  The original request may already
            # have changed server state, so it is outcome-unknown. HTTP 408 is
            # equally ambiguous: the server or an upstream proxy may time out
            # after the registration side effect has already happened.
            if error.status is None or error.status == 408:
                raise RegistrationOutcomeUnknown(
                    "选课提交后的响应无法可靠确认最终结果；已停止继续提交",
                    endpoint=_REGISTER_PATH,
                ) from error
            raise
        except RegistrationOutcomeUnknown:
            raise
        except Exception as error:
            # Reading a response body can fail with transport-specific exception
            # classes outside the normal network-error hierarchy. Once the
            # request has been sent, treating such an exception as a definite
            # failure could allow a later course to exceed the requested quota.
            raise RegistrationOutcomeUnknown(
                "选课提交过程中发生异常，无法确认最终结果；已停止继续提交",
                endpoint=_REGISTER_PATH,
            ) from error

        try:
            root = _json_object(response, endpoint=_REGISTER_PATH, label="选课提交")
            return _registration_result(root, response)
        except RegistrationOutcomeUnknown:
            raise
        except ResponseFormatError as error:
            raise RegistrationOutcomeUnknown(
                "选课提交已返回，但响应格式无法确认成功或失败；已停止继续提交",
                endpoint=_REGISTER_PATH,
                response_excerpt=_excerpt(response.text),
            ) from error

    def _prepare_locked(self, term: str, required_period_id: str | None) -> None:
        periods = self._load_periods()
        selected = _select_period(periods, term, required_period_id=required_period_id)
        self._enter_period(selected)
        self._selection_context = _SelectionContext(term, selected)
        self._selection_generation = self.session.auth_generation

    def _restore_selection_locked(self) -> None:
        context = self._selection_context
        if context is None:
            raise BadRequestError(
                "提交选课前必须成功调用 prepare_selection(term)；拒绝在未知期次盲目提交"
            )
        if self._selection_generation == self.session.auth_generation:
            return
        self._prepare_locked(context.term, required_period_id=context.period.id)

    def _load_periods(self) -> tuple[_Period, ...]:
        response = self.session.request(_PERIODS_PATH)
        parser = _PeriodLinkParser()
        try:
            parser.feed(response.text)
            parser.close()
        except Exception as error:
            raise ResponseFormatError(
                "无法解析选课期次页面；页面结构可能已经改变",
                endpoint=_PERIODS_PATH,
                response_excerpt=_excerpt(response.text),
            ) from error

        periods: list[_Period] = []
        seen_urls: set[str] = set()
        for link in parser.links:
            url = unescape(link.url).strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            periods.append(
                _Period(
                    id=_period_id(url),
                    name=link.name or "选课期次",
                    url=url,
                    term=_infer_term(f"{link.name} {unquote(url)}"),
                )
            )

        if not periods:
            raise SelectionPeriodNotFoundError(
                "选课期次页没有“进入选课”链接：当前可能未开放选课，或页面结构已经改变",
                endpoint=_PERIODS_PATH,
            )
        return tuple(periods)

    def _enter_period(self, period: _Period) -> None:
        response = self.session.request(
            period.url,
            referer=f"{self.session.client.base_url}{_PERIODS_PATH}",
        )
        compact = _normalize_space(_TAG_RE.sub(" ", response.text))
        if any(marker in compact for marker in ("不在选课时间", "选课已经结束", "选课未开放")):
            raise SelectionPeriodNotFoundError(
                f"进入选课期次失败：{_excerpt(compact)}",
                endpoint=period.url,
            )

        positive_markers = (
            "选课页面",
            "选课中心",
            "课程类别",
            "newXsxkzx",
            "选课操作",
        )
        response_period_id = _period_id(response.final_url)
        response_term = _infer_term(compact)
        if (
            period.term is not None
            and response_term is not None
            and response_term != period.term
        ):
            raise SelectionPeriodNotFoundError(
                f"进入页面属于 {response_term}，与目标学期 {period.term} 不一致",
                endpoint=period.url,
            )
        period_confirmed = response_period_id == period.id or (
            period.term is not None and response_term == period.term
        )
        if not any(marker in compact for marker in positive_markers) or not period_confirmed:
            raise SelectionPeriodNotFoundError(
                "进入选课期次后未找到选课页面标志或目标期次标识；拒绝继续提交",
                endpoint=period.url,
            )


def format_enrolled_result(result: EnrolledCourseResult) -> str:
    """按课程类型分组，并生成中文宽字符对齐的终端列表。"""

    if not result.courses:
        return "暂无已选课程"

    grouped: dict[str, list[EnrolledCourse]] = {}
    for course in result.courses:
        grouped.setdefault(_course_group(course.course_type), []).append(course)

    group_order = [label for label in ("必修", "限选") if label in grouped]
    group_order.extend(label for label in grouped if label not in {"必修", "限选"})

    table_groups: list[tuple[str, int, float, list[tuple[str, ...]]]] = []
    all_rows: list[tuple[str, ...]] = []
    for label in group_order:
        courses = (
            sorted(grouped[label], key=_course_time_sort_key)
            if label in {"必修", "限选"}
            else grouped[label]
        )
        rows: list[tuple[str, ...]] = []
        for course in courses:
            meetings = course.meetings or (CourseMeeting("", ""),)
            for meeting_index, meeting in enumerate(meetings):
                show_course_label = meeting_index == 0
                row = (
                    (course.name or "课程名待定") if show_course_label else "",
                    f"{_safe_float(course.credit):.1f}分" if show_course_label else "",
                    (course.teacher or "教师待定") if show_course_label else "",
                    _meeting_schedule(meeting),
                    meeting.location or "地点待定",
                )
                rows.append(row)
                all_rows.append(row)
        credits = sum(_safe_float(course.credit) for course in courses)
        table_groups.append((label, len(courses), credits, rows))

    widths = tuple(
        max(_visual_width(row[index]) for row in all_rows)
        for index in range(len(all_rows[0]))
    )
    captions = tuple(
        f"{label}（{course_count}门-{credits:.1f}学分）"
        for label, course_count, credits, _rows in table_groups
    )
    content_width = sum(widths) + _visual_width(_COLUMN_GAP) * (len(widths) - 1)
    section_width = max(
        content_width,
        *(_visual_width(caption) + 18 for caption in captions),
    )

    lines: list[str] = []
    for caption, (_label, _course_count, _credits, rows) in zip(
        captions, table_groups, strict=True
    ):
        if lines:
            lines.append("")
        lines.append(_section_line(caption, section_width))
        lines.extend(_table_row(row, widths) for row in rows)
    return "\n".join(lines)


def _course_group(course_type: str) -> str:
    label = course_type.strip()
    compact = re.sub(r"\s+", "", label)
    if "必修" in compact or "必选" in compact:
        return "必修"
    if "限选" in compact:
        return "限选"
    return label or "其他"


def _meeting_schedule(meeting: CourseMeeting) -> str:
    return meeting.time or "时间待定"


def _course_time_sort_key(course: EnrolledCourse) -> tuple[int, int, int, int]:
    known_times: list[tuple[int, int, int]] = []
    for meeting in course.meetings:
        match = _DISPLAY_TIME_RE.fullmatch(meeting.time.strip())
        if match is None:
            continue
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        weekday = _WEEKDAYS[f"星期{match.group('weekday')}"]
        known_times.append((weekday, start, end))
    if not known_times:
        return 1, 8, 0, 0
    return (0, *min(known_times))


def _visual_width(text: str) -> int:
    width = 0
    for character in text:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return width


def _pad_cell(text: str, width: int, *, right: bool = False) -> str:
    padding = " " * max(0, width - _visual_width(text))
    return f"{padding}{text}" if right else f"{text}{padding}"


_ROW_INDENT = "  "
_COLUMN_GAP = "  "


def _table_row(values: tuple[str, ...], widths: tuple[int, ...]) -> str:
    cells = [
        _pad_cell(value, width, right=index == 1) if index < len(values) - 1 else value
        for index, (value, width) in enumerate(zip(values, widths, strict=True))
    ]
    return (_ROW_INDENT + _COLUMN_GAP.join(cells)).rstrip()


def _section_line(caption: str, width: int) -> str:
    centered = f" {caption} "
    remaining = max(0, width - _visual_width(centered))
    left = remaining // 2
    right = remaining - left
    return f"{_ROW_INDENT}{'─' * left}{centered}{'─' * right}"


def _parse_enrolled_course(item: Mapping[str, object]) -> EnrolledCourse:
    return EnrolledCourse(
        name=_string(item, "kc_mc"),
        teacher=_string(item, "xm"),
        credit=_string(item, "xf", "0"),
        course_type=_string(item, "kclb_mc"),
        meetings=_parse_meetings(
            _string(item, "sksj"),
            _string(item, "skdd"),
        ),
    )


def _parse_meetings(schedule_raw: str, location_raw: str) -> tuple[CourseMeeting, ...]:
    schedules = tuple(
        _format_schedule_line(line) if line else ""
        for line in _split_paired_lines(schedule_raw)
    )
    locations = _split_paired_lines(location_raw)
    if len(locations) == 1 and len(schedules) > 1:
        locations = locations * len(schedules)

    count = max(len(schedules), len(locations))
    return tuple(
        CourseMeeting(
            schedules[index] if index < len(schedules) else "",
            locations[index] if index < len(locations) else "",
        )
        for index in range(count)
    )


def _format_schedule_line(line: str) -> str:
    match = re.fullmatch(
        r"(星期[一二三四五六日])\s*(\d+)(?:节)?"
        r"(?:\s*[（(][^（）()]*周[^（）()]*[）)])?",
        line,
    )
    if match is None:
        return line
    digits = re.sub(r"\D", "", match.group(2))
    sections = [
        int(digits[index : index + 2])
        for index in range(0, len(digits) - 1, 2)
    ]
    if not sections:
        return line
    weekday = _WEEKDAYS[match.group(1)]
    return f"{_WEEKDAY_LABELS[weekday]} {sections[0]}-{sections[-1]}节"


def _select_period(
    periods: Iterable[_Period],
    term: str,
    *,
    required_period_id: str | None,
) -> _Period:
    candidates = tuple(periods)
    if required_period_id is not None:
        explicit = tuple(period for period in candidates if period.id == required_period_id)
        if len(explicit) == 1:
            selected = explicit[0]
            if selected.term is not None and selected.term != term:
                raise SelectionPeriodNotFoundError(
                    f"期次 {required_period_id} 属于 {selected.term}，与请求学期 {term} 不一致"
                )
            return selected
        if len(explicit) > 1:
            raise SelectionPeriodNotFoundError(
                f"期次 ID {required_period_id} 不唯一，拒绝盲目选择"
            )
        raise SelectionPeriodNotFoundError(f"找不到原选课期次 ID {required_period_id}")

    matched = tuple(
        period
        for period in candidates
        if period.term == term or term in unquote(period.name) or term in unquote(period.url)
    )
    if len(matched) == 1:
        return matched[0]
    if not matched:
        available = ", ".join(
            f"{period.id}({period.term or '学期未知'})" for period in candidates
        )
        raise SelectionPeriodNotFoundError(
            f"没有与学期 {term} 明确匹配的选课期次。页面期次：{available or '无'}"
        )
    raise SelectionPeriodNotFoundError(
        f"学期 {term} 匹配到多个期次（{', '.join(item.id for item in matched)}），拒绝盲目选择"
    )


@dataclass(frozen=True, slots=True)
class _PeriodLink:
    url: str
    name: str


class _PeriodLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[_PeriodLink] = []
        self._in_row = False
        self._row_text: list[str] = []
        self._row_anchors: list[tuple[str, str]] = []
        self._anchor_url: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.casefold()
        if lowered == "tr":
            self._in_row = True
            self._row_text = []
            self._row_anchors = []
        elif lowered == "a":
            values = {name.casefold(): value or "" for name, value in attrs}
            self._anchor_url = values.get("href", "")
            self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._in_row:
            self._row_text.append(data)
        if self._anchor_url is not None:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "a" and self._anchor_url is not None:
            label = _normalize_space(" ".join(self._anchor_text))
            if "进入选课" in label:
                if self._in_row:
                    self._row_anchors.append((self._anchor_url, label))
                else:
                    self.links.append(_PeriodLink(self._anchor_url, label))
            self._anchor_url = None
            self._anchor_text = []
        elif lowered == "tr" and self._in_row:
            row_label = _normalize_space(" ".join(self._row_text))
            for url, anchor_label in self._row_anchors:
                name = _normalize_space(row_label.replace(anchor_label, "")) or "选课期次"
                self.links.append(_PeriodLink(url, name))
            self._in_row = False
            self._row_text = []
            self._row_anchors = []


def _json_object(response: HttpResponse, *, endpoint: str, label: str) -> Mapping[str, object]:
    try:
        payload: object = json.loads(response.text)
    except json.JSONDecodeError as error:
        raise ResponseFormatError(
            f"{label}接口没有返回 JSON；端点或会话流程可能已经改变",
            endpoint=endpoint,
            response_excerpt=_excerpt(response.text),
        ) from error
    if not isinstance(payload, dict):
        raise ResponseFormatError(
            f"{label}接口 JSON 根节点不是对象",
            endpoint=endpoint,
            response_excerpt=_excerpt(response.text),
        )
    return cast(Mapping[str, object], payload)


def _validate_business_status(
    root: Mapping[str, object],
    *,
    endpoint: str,
    label: str,
) -> None:
    explicit_success, failed_code, signal_error = _business_signals(
        root,
        endpoint=endpoint,
        label=label,
    )
    if explicit_success is False or failed_code:
        message = _first_string(root, ("msg", "message", "error")) or "未知错误"
        raise JwUnavailableError(
            f"{label}接口返回业务错误（code={root.get('code')!s}）：{message}",
            endpoint=endpoint,
        )
    if signal_error is None:
        return
    raise signal_error


def _registration_result(
    root: Mapping[str, object],
    response: HttpResponse,
) -> RegisterResult:
    message = _first_string(root, ("message", "msg", "error"))
    remaining_slots = _remaining_slots(root)
    explicit_success, failed_code, signal_error = _business_signals(
        root,
        endpoint=_REGISTER_PATH,
        label="选课提交",
    )
    failure_kind = _classify_registration_failure(message)

    # Explicit negative flags and failure codes are authoritative.  A positive
    # flag that contradicts a recognized failure message is outcome-unknown:
    # choosing either side could make the engine select one course too many.
    if explicit_success is False or failed_code:
        return RegisterResult(
            success=False,
            message=message or "选课失败（接口未返回原因）",
            code=failure_kind,
            remaining_slots=remaining_slots,
        )

    if explicit_success is True:
        if signal_error is not None:
            raise signal_error
        if failure_kind is not RegisterResultCode.UNKNOWN:
            raise RegistrationOutcomeUnknown(
                "选课提交响应的成功标志与失败原因互相矛盾；已停止继续提交",
                endpoint=_REGISTER_PATH,
                response_excerpt=_excerpt(response.text),
            )
        return RegisterResult(
            success=True,
            message=message or "选课成功",
            code=RegisterResultCode.SUCCESS,
            remaining_slots=remaining_slots,
        )

    if failure_kind is not RegisterResultCode.UNKNOWN:
        return RegisterResult(
            success=False,
            message=message,
            code=failure_kind,
            remaining_slots=remaining_slots,
        )

    if signal_error is not None:
        raise signal_error

    if _message_confirms_success(message):
        return RegisterResult(
            success=True,
            message=message,
            code=RegisterResultCode.SUCCESS,
            remaining_slots=remaining_slots,
        )

    # A syntactically valid object is not automatically a definite failure.
    # The server may have completed the side effect while changing its schema.
    raise RegistrationOutcomeUnknown(
        "选课提交响应既没有明确成功，也没有明确失败；已停止继续提交",
        endpoint=_REGISTER_PATH,
        response_excerpt=_excerpt(response.text),
    )


def _explicit_success(
    root: Mapping[str, object],
    *,
    endpoint: str,
    label: str,
) -> bool | None:
    if "success" not in root:
        return None
    value = root.get("success")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
    elif isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "success"}:
            return True
        if normalized in {"0", "false", "fail", "failed"}:
            return False
    raise ResponseFormatError(
        f"{label}接口 success 字段格式无法识别",
        endpoint=endpoint,
    )


def _has_failure_code(
    root: Mapping[str, object],
    *,
    endpoint: str,
    label: str,
) -> bool:
    if "code" not in root:
        return False
    value = root.get("code")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ResponseFormatError(
            f"{label}接口 code 字段格式无法识别",
            endpoint=endpoint,
        )
    if isinstance(value, (int, float)):
        return value not in {0, 200}

    normalized = value.strip().casefold()
    if normalized in {"0", "200", "ok", "success"}:
        return False
    if normalized.lstrip("+-").isdigit():
        return int(normalized) not in {0, 200}
    if normalized in {"error", "fail", "failed", "failure", "false"}:
        return True
    raise ResponseFormatError(
        f"{label}接口 code 字段值无法识别",
        endpoint=endpoint,
    )


def _business_signals(
    root: Mapping[str, object],
    *,
    endpoint: str,
    label: str,
) -> tuple[bool | None, bool, ResponseFormatError | None]:
    """Read success/code while preserving any independent definite failure."""

    error: ResponseFormatError | None = None
    try:
        explicit_success = _explicit_success(root, endpoint=endpoint, label=label)
    except ResponseFormatError as caught:
        explicit_success = None
        error = caught
    try:
        failed_code = _has_failure_code(root, endpoint=endpoint, label=label)
    except ResponseFormatError as caught:
        failed_code = False
        if error is None:
            error = caught
    return explicit_success, failed_code, error


def _message_confirms_success(message: str) -> bool:
    negative = ("未成功", "不成功", "失败", "未选中", "未选择")
    return any(marker in message for marker in ("选课成功", "选择成功")) and not any(
        marker in message for marker in negative
    )


def _classify_registration_failure(message: str) -> RegisterResultCode:
    if any(marker in message for marker in ("已选择其它教学班", "已经选择", "已选过", "重复选课")):
        return RegisterResultCode.ALREADY_ENROLLED
    if any(marker in message for marker in ("人数已满", "课程已满", "容量已满", "无余量", "名额已满")):
        return RegisterResultCode.COURSE_FULL
    if "时间冲突" in message or "课程冲突" in message:
        return RegisterResultCode.TIME_CONFLICT
    if any(marker in message for marker in ("选课结束", "不在选课时间", "选课已关闭", "选课关闭")):
        return RegisterResultCode.REG_CLOSED
    if any(marker in message for marker in ("未到", "未开放", "尚未开放", "当前未开放")):
        return RegisterResultCode.NOT_OPEN_YET
    return RegisterResultCode.UNKNOWN


def _remaining_slots(root: Mapping[str, object]) -> int | None:
    for key in ("remainingSlots", "remaining_slots", "syrs", "remain", "remaining"):
        value = root.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            match = re.search(r"-?\d+", value)
            if match:
                return int(match.group())
    return None


def _string(record: Mapping[str, object], key: str, default: str = "") -> str:
    value = record.get(key)
    if value is None:
        return default
    if isinstance(value, str):
        return unescape(value).strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return default


def _first_string(record: Mapping[str, object], keys: Iterable[str]) -> str:
    for key in keys:
        value = _string(record, key)
        if value:
            return value
    return ""


def _safe_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _period_id(url: str) -> str:
    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    for key in ("id", "xklcid", "xklc", "xnxqid"):
        values = query.get(key)
        if values and values[0]:
            return unquote(values[0])
    return url


def _infer_term(text: str) -> str | None:
    direct = _TERM_SEARCH_RE.search(text)
    if direct and is_valid_term(direct.group(1)):
        return direct.group(1)
    chinese = _CHINESE_TERM_RE.search(text)
    if chinese is None:
        return None
    number = {"一": 1, "二": 2, "1": 1, "2": 2}[chinese.group("num")]
    candidate = f"{chinese.group('start')}-{chinese.group('end')}-{number}"
    return candidate if is_valid_term(candidate) else None


def _require_term(term: str) -> str:
    normalized = term.strip()
    if not is_valid_term(normalized):
        raise BadRequestError(
            "学期格式无效，应为 YYYY-YYYY-1 或 YYYY-YYYY-2，且结束年份应比开始年份大 1"
        )
    return normalized


def _split_paired_lines(raw: str) -> tuple[str, ...]:
    expanded = _BR_RE.sub("\n", unescape(raw))
    lines = [_normalize_space(line) for line in expanded.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return tuple(lines)


def _normalize_space(value: str) -> str:
    return " ".join(value.split())


def _excerpt(text: str, limit: int = 240) -> str:
    compact = _normalize_space(_TAG_RE.sub(" ", text))
    return compact if len(compact) <= limit else f"{compact[:limit]}…"


__all__ = ["CourseSelector", "format_enrolled_result"]
