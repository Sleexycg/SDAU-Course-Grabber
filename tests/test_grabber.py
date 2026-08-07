from __future__ import annotations

import threading
import time
import unittest

from main.errors import RegistrationOutcomeUnknown
from main.grabber import GrabEngine
from main.models import GrabTaskStatus, RegisterResult, RegisterResultCode


def _success() -> RegisterResult:
    return RegisterResult(True, "ok", RegisterResultCode.SUCCESS)


def _full() -> RegisterResult:
    return RegisterResult(False, "full", RegisterResultCode.COURSE_FULL)


def _already_enrolled() -> RegisterResult:
    return RegisterResult(False, "already enrolled", RegisterResultCode.ALREADY_ENROLLED)


def _conflict() -> RegisterResult:
    return RegisterResult(False, "conflict", RegisterResultCode.TIME_CONFLICT)


class FakeSession:
    pass


class BlockingSuccessSelector:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def register_course_with_refresh(self, course_id: str) -> RegisterResult:
        with self._lock:
            self.calls.append(course_id)
            self.entered.set()
        if not self.release.wait(1):
            raise TimeoutError("test did not release selector")
        return _success()


class ConcurrentSuccessSelector:
    def __init__(self, expected_parallel: int) -> None:
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.all_entered = threading.Event()
        self.release = threading.Event()
        self._expected_parallel = expected_parallel
        self._lock = threading.Lock()

    def register_course_with_refresh(self, course_id: str) -> RegisterResult:
        with self._lock:
            self.calls.append(course_id)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == self._expected_parallel:
                self.all_entered.set()
        try:
            if not self.release.wait(1):
                raise TimeoutError("test did not release selector")
            return _success()
        finally:
            with self._lock:
                self.active -= 1


class FullThenSuccessSelector:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def register_course_with_refresh(self, course_id: str) -> RegisterResult:
        with self._lock:
            self.calls.append(course_id)
            call_number = len(self.calls)
        return _full() if call_number == 1 else _success()


class ConflictThenSuccessSelector:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def register_course_with_refresh(self, course_id: str) -> RegisterResult:
        with self._lock:
            self.calls.append(course_id)
            call_number = len(self.calls)
        return _conflict() if call_number == 1 else _success()


class UnknownSelector:
    def __init__(self) -> None:
        self.calls = 0

    def register_course_with_refresh(self, course_id: str) -> RegisterResult:
        self.calls += 1
        return RegisterResult(False, "unknown", RegisterResultCode.UNKNOWN)


class NetworkFailureSelector:
    def __init__(self) -> None:
        self.calls = 0

    def register_course_with_refresh(self, course_id: str) -> RegisterResult:
        self.calls += 1
        raise ConnectionError("offline")


class BlockingUnknownOutcomeSelector:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def register_course_with_refresh(self, course_id: str) -> RegisterResult:
        with self._lock:
            self.calls.append(course_id)
            self.entered.set()
        if not self.release.wait(1):
            raise TimeoutError("test did not release selector")
        raise RegistrationOutcomeUnknown("提交响应丢失")


class BlockingAlreadyEnrolledSelector:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def register_course_with_refresh(self, course_id: str) -> RegisterResult:
        with self._lock:
            self.calls.append(course_id)
            self.entered.set()
        if not self.release.wait(1):
            raise TimeoutError("test did not release selector")
        return _already_enrolled()


class GrabberTests(unittest.TestCase):
    def test_target_one_never_starts_a_second_request_while_one_is_in_flight(self) -> None:
        selector = BlockingSuccessSelector()
        engine = GrabEngine(selector, FakeSession())
        engine.start(["A", "B", "C"], target_count=1, interval_ms=10)

        self.assertTrue(selector.entered.wait(1))
        time.sleep(0.05)
        self.assertEqual(len(selector.calls), 1)

        selector.release.set()
        states = engine.wait(timeout=2)
        self.assertEqual(sum(state.status == GrabTaskStatus.SUCCESS for state in states), 1)
        self.assertEqual(len(selector.calls), 1)

    def test_target_two_limits_real_parallel_requests(self) -> None:
        selector = ConcurrentSuccessSelector(expected_parallel=2)
        engine = GrabEngine(selector, FakeSession())
        engine.start(["A", "B", "C"], target_count=2, interval_ms=10)

        self.assertTrue(selector.all_entered.wait(1))
        time.sleep(0.05)
        self.assertEqual(selector.max_active, 2)
        self.assertEqual(len(selector.calls), 2)

        selector.release.set()
        states = engine.wait(timeout=2)
        self.assertEqual(sum(state.status == GrabTaskStatus.SUCCESS for state in states), 2)
        self.assertEqual(len(selector.calls), 2)

    def test_course_full_releases_token_for_another_request(self) -> None:
        selector = FullThenSuccessSelector()
        engine = GrabEngine(selector, FakeSession())
        engine.start(["A", "B"], target_count=1, interval_ms=20)

        states = engine.wait(timeout=2)
        self.assertEqual(len(selector.calls), 2)
        self.assertEqual(sum(state.status == GrabTaskStatus.SUCCESS for state in states), 1)

    def test_determinate_failure_releases_token_for_another_request(self) -> None:
        selector = ConflictThenSuccessSelector()
        engine = GrabEngine(selector, FakeSession())
        engine.start(["A", "B"], target_count=1, interval_ms=10)

        states = engine.wait(timeout=2)
        self.assertEqual(len(selector.calls), 2)
        self.assertEqual(
            sum(state.status == GrabTaskStatus.CONFLICT for state in states),
            1,
        )
        self.assertEqual(
            sum(state.status == GrabTaskStatus.SUCCESS for state in states),
            1,
        )

    def test_unknown_result_is_terminal(self) -> None:
        selector = UnknownSelector()
        engine = GrabEngine(selector, FakeSession())
        engine.start(["A"], target_count=1, interval_ms=10)

        states = engine.wait(timeout=2)
        self.assertEqual(selector.calls, 1)
        self.assertEqual(states[0].status, GrabTaskStatus.ERROR)

    def test_network_exception_is_terminal(self) -> None:
        selector = NetworkFailureSelector()
        engine = GrabEngine(selector, FakeSession())
        engine.start(["A"], target_count=1, interval_ms=10)

        states = engine.wait(timeout=2)
        self.assertEqual(selector.calls, 1)
        self.assertEqual(states[0].status, GrabTaskStatus.ERROR)

    def test_target_one_unknown_outcome_stops_before_second_request(self) -> None:
        selector = BlockingUnknownOutcomeSelector()
        engine = GrabEngine(selector, FakeSession())
        engine.start(["A", "B"], target_count=1, interval_ms=10)

        self.assertTrue(selector.entered.wait(1))
        time.sleep(0.05)
        self.assertEqual(len(selector.calls), 1)

        selector.release.set()
        states = engine.wait(timeout=2)
        self.assertEqual(len(selector.calls), 1)
        called = next(state for state in states if state.course_id == selector.calls[0])
        self.assertEqual(called.status, GrabTaskStatus.ERROR)
        self.assertIn("结果未知", called.last_message or "")
        self.assertIn("重新查询", called.last_message or "")
        self.assertEqual(
            sum(state.status == GrabTaskStatus.STOPPED for state in states),
            1,
        )

    def test_target_one_already_enrolled_stops_before_second_request(self) -> None:
        selector = BlockingAlreadyEnrolledSelector()
        engine = GrabEngine(selector, FakeSession())
        engine.start(["A", "B"], target_count=1, interval_ms=10)

        self.assertTrue(selector.entered.wait(1))
        time.sleep(0.05)
        self.assertEqual(len(selector.calls), 1)

        selector.release.set()
        states = engine.wait(timeout=2)
        self.assertEqual(len(selector.calls), 1)
        called = next(state for state in states if state.course_id == selector.calls[0])
        self.assertEqual(called.status, GrabTaskStatus.ALREADY_ENROLLED)
        self.assertEqual(
            sum(state.status == GrabTaskStatus.STOPPED for state in states),
            1,
        )


if __name__ == "__main__":
    unittest.main()
