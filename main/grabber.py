"""Minimal concurrent course-registration engine."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import threading
import time
from typing import TYPE_CHECKING

from .errors import RegistrationOutcomeUnknown
from .models import GrabTaskState, GrabTaskStatus, RegisterResult, RegisterResultCode

if TYPE_CHECKING:
    from .course_selector import CourseSelector
    from .session import Session


_RETRIABLE_CODES = frozenset(
    {RegisterResultCode.COURSE_FULL, RegisterResultCode.NOT_OPEN_YET}
)
_TERMINAL_STATUS = {
    RegisterResultCode.TIME_CONFLICT: GrabTaskStatus.CONFLICT,
    RegisterResultCode.REG_CLOSED: GrabTaskStatus.CLOSED,
}


@dataclass(slots=True)
class _TaskRecord:
    course_id: str
    course_name: str
    status: GrabTaskStatus = GrabTaskStatus.RUNNING
    attempt_count: int = 0
    last_message: str | None = None
    result: RegisterResult | None = None

    def snapshot(self) -> GrabTaskState:
        return GrabTaskState(
            course_id=self.course_id,
            course_name=self.course_name,
            status=self.status,
            attempt_count=self.attempt_count,
            last_message=self.last_message,
            result=self.result,
        )


class _StopRequested(Exception):
    """Internal signal used to release a reserved semaphore token cleanly."""


class GrabEngine:
    """Run one polling worker per course without exceeding ``target_count``.

    A worker acquires one semaphore token before every registration request.
    Determinate failed attempts release their token. Successful and already
    enrolled results permanently consume one token, while an indeterminate
    submission consumes its token and stops all future requests. Consequently
    satisfied plus in-flight or indeterminate requests never exceeds the target.
    """

    def __init__(self, selector: CourseSelector, session: Session) -> None:
        self._selector = selector
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._tasks: list[_TaskRecord] = []
        self._threads: list[threading.Thread] = []
        self._tokens: threading.BoundedSemaphore | None = None
        self._satisfied = 0
        self._target_count = 0
        self._interval_seconds = 0.8

    def start(
        self,
        course_ids: Iterable[str],
        course_names: Sequence[str] | None = None,
        *,
        target_count: int = 1,
        interval_ms: int = 800,
    ) -> None:
        """Start workers for unique non-empty course IDs."""

        courses = _normalise_courses(course_ids, course_names)
        if not courses:
            raise ValueError("at least one non-empty course ID is required")
        if isinstance(target_count, bool) or not isinstance(target_count, int):
            raise TypeError("target_count must be an integer")
        if not 1 <= target_count <= len(courses):
            raise ValueError("target_count must be between 1 and the number of courses")
        if isinstance(interval_ms, bool) or not isinstance(interval_ms, int):
            raise TypeError("interval_ms must be an integer")
        if interval_ms <= 0:
            raise ValueError("interval_ms must be greater than zero")

        with self._state_lock:
            if any(thread.is_alive() for thread in self._threads):
                raise RuntimeError("grab engine is already running")

            self._stop_event.clear()
            self._satisfied = 0
            self._target_count = target_count
            self._interval_seconds = interval_ms / 1000
            self._tokens = threading.BoundedSemaphore(target_count)
            self._tasks = [
                _TaskRecord(course_id=course_id, course_name=course_name)
                for course_id, course_name in courses
            ]
            self._threads = [
                threading.Thread(
                    target=self._run_task,
                    args=(task,),
                    name=f"course-grabber-{index + 1}",
                    daemon=True,
                )
                for index, task in enumerate(self._tasks)
            ]
            threads = list(self._threads)

        for thread in threads:
            thread.start()

    def wait(self, timeout: float | None = None) -> list[GrabTaskState]:
        """Wait for workers up to one overall timeout and return snapshots."""

        if timeout is not None and timeout < 0:
            raise ValueError("timeout cannot be negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._state_lock:
            threads = list(self._threads)
        for thread in threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread.join(remaining)
            if deadline is not None and time.monotonic() >= deadline:
                break
        with self._state_lock:
            return [task.snapshot() for task in self._tasks]

    def stop_all(self) -> None:
        """Stop future requests; an HTTP request already in flight may finish."""

        self._stop_event.set()

    def _run_task(self, task: _TaskRecord) -> None:
        tokens = self._tokens
        if tokens is None:
            self._finish(task, GrabTaskStatus.ERROR, "抢课配额尚未初始化")
            return

        try:
            while not self._stop_event.is_set():
                if not self._acquire_token(tokens):
                    break

                release_token = True
                try:
                    if self._stop_event.is_set():
                        raise _StopRequested

                    with self._state_lock:
                        task.attempt_count += 1

                    result = self._selector.register_course_with_refresh(task.course_id)
                    with self._state_lock:
                        task.result = result
                        task.last_message = result.message

                    if result.code == RegisterResultCode.ALREADY_ENROLLED:
                        # The requested outcome is already true. Count it toward
                        # the target and retain the token just like a success.
                        release_token = False
                        with self._state_lock:
                            self._satisfied += 1
                            task.status = GrabTaskStatus.ALREADY_ENROLLED
                            if self._satisfied >= self._target_count:
                                self._stop_event.set()
                        return

                    if result.success:
                        # A successful request permanently consumes its token.
                        release_token = False
                        with self._state_lock:
                            self._satisfied += 1
                            task.status = GrabTaskStatus.SUCCESS
                            if self._satisfied >= self._target_count:
                                self._stop_event.set()
                        return

                    if result.code in _RETRIABLE_CODES:
                        # Let another course use the quota while this worker is
                        # waiting for its next fixed-interval attempt.
                        tokens.release()
                        release_token = False
                        if self._stop_event.wait(self._interval_seconds):
                            break
                        continue

                    status = _TERMINAL_STATUS.get(result.code, GrabTaskStatus.ERROR)
                    self._finish(task, status, result.message)
                    return
                except _StopRequested:
                    break
                except RegistrationOutcomeUnknown as exc:
                    # Releasing this reservation could let another course submit
                    # even though this request may already have succeeded.
                    release_token = False
                    detail = _safe_exception_message(exc)
                    message = "选课结果未知，请重新查询已选课程确认"
                    if detail and detail != type(exc).__name__:
                        message = f"{message}：{detail}"
                    self._finish(task, GrabTaskStatus.ERROR, message)
                    self._stop_event.set()
                    return
                except Exception as exc:
                    self._finish(
                        task,
                        GrabTaskStatus.ERROR,
                        f"任务异常：{_safe_exception_message(exc)}",
                    )
                    return
                finally:
                    if release_token:
                        tokens.release()
        finally:
            with self._state_lock:
                if task.status == GrabTaskStatus.RUNNING:
                    task.status = GrabTaskStatus.STOPPED
                    task.last_message = "任务已停止"

    def _acquire_token(self, tokens: threading.BoundedSemaphore) -> bool:
        while not self._stop_event.is_set():
            if tokens.acquire(timeout=0.1):
                return True
        return False

    def _finish(
        self,
        task: _TaskRecord,
        status: GrabTaskStatus,
        message: str,
    ) -> None:
        with self._state_lock:
            task.status = status
            task.last_message = message


def _normalise_courses(
    course_ids: Iterable[str],
    course_names: Sequence[str] | None,
) -> list[tuple[str, str]]:
    if isinstance(course_ids, (str, bytes)):
        raise TypeError("course_ids must be an iterable of course IDs")
    raw_ids = [str(course_id).strip() for course_id in course_ids]
    if course_names is None:
        raw_names = raw_ids
    else:
        if isinstance(course_names, (str, bytes)):
            raise TypeError("course_names must be a sequence of names")
        raw_names = [str(name).strip() for name in course_names]
        if len(raw_names) != len(raw_ids):
            raise ValueError("course_names must match course_ids length")

    courses: list[tuple[str, str]] = []
    seen: set[str] = set()
    for course_id, course_name in zip(raw_ids, raw_names, strict=True):
        if not course_id or course_id in seen:
            continue
        seen.add(course_id)
        courses.append((course_id, course_name or course_id))
    return courses


def _safe_exception_message(exc: Exception) -> str:
    message = str(exc).strip().replace("\r", " ").replace("\n", " ")
    if "http://" in message.lower() or "https://" in message.lower():
        return type(exc).__name__
    return message[:300] or type(exc).__name__


__all__ = ["GrabEngine"]
