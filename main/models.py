"""应用层共享的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RegisterResultCode(StrEnum):
    SUCCESS = "SUCCESS"
    COURSE_FULL = "COURSE_FULL"
    ALREADY_ENROLLED = "ALREADY_ENROLLED"
    TIME_CONFLICT = "TIME_CONFLICT"
    REG_CLOSED = "REG_CLOSED"
    NOT_OPEN_YET = "NOT_OPEN_YET"
    UNKNOWN = "UNKNOWN"


class GrabTaskStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    STOPPED = "stopped"
    ERROR = "error"
    ALREADY_ENROLLED = "already_enrolled"
    CONFLICT = "conflict"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class CourseMeeting:
    time: str
    location: str


@dataclass(frozen=True, slots=True)
class EnrolledCourse:
    name: str
    teacher: str
    credit: str
    course_type: str = ""
    meetings: tuple[CourseMeeting, ...] = ()


@dataclass(frozen=True, slots=True)
class EnrolledSummary:
    total_courses: int
    total_credits: float


@dataclass(frozen=True, slots=True)
class EnrolledCourseResult:
    term: str
    courses: tuple[EnrolledCourse, ...]
    summary: EnrolledSummary


@dataclass(frozen=True, slots=True)
class RegisterResult:
    success: bool
    message: str
    code: RegisterResultCode = RegisterResultCode.UNKNOWN
    remaining_slots: int | None = None


@dataclass(frozen=True, slots=True)
class GrabTaskState:
    course_id: str
    course_name: str
    status: GrabTaskStatus
    attempt_count: int
    last_message: str | None = None
    result: RegisterResult | None = None
