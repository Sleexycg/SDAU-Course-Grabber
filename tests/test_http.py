from __future__ import annotations

import threading
import unittest

from main.errors import BadRequestError, JwUnavailableError, UnauthorizedError
from main.http import HttpRequest, HttpResponse, JwHttpClient, TransportResponse
from main.session import Session


class FakeTransport:
    def __init__(self, responses: list[TransportResponse]) -> None:
        self.responses = responses
        self.requests: list[HttpRequest] = []
        self.cookies = ""

    def send(self, request: HttpRequest, *, timeout: float) -> TransportResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    def cookie_header(self, *, url: str) -> str:
        return self.cookies

    def clear_cookies(self) -> None:
        self.cookies = ""


class ScriptedSessionClient:
    """让请求连续两次返回登录页，用于验证认证重试上限。"""

    base_url = "https://jw.sdau.edu.cn"

    def __init__(self) -> None:
        self.cookie_header = ""
        self.private_calls = 0
        self.login_calls = 0

    def clear_cookies(self) -> None:
        self.cookie_header = ""

    def request(self, path: str, **kwargs: object) -> HttpResponse:
        if path == "/":
            return HttpResponse(
                200,
                "<script>var scode='abcdef'; var sxh='1111111111111111111111111111111111111111111111111111111';</script>",
                f"{self.base_url}/",
            )
        if path == "/xk/LoginToXk":
            self.login_calls += 1
            self.cookie_header = f"JSESSIONID={self.login_calls}"
            return HttpResponse(200, "<html>主页</html>", f"{self.base_url}/framework/main")
        if path == "/private":
            self.private_calls += 1
            return HttpResponse(
                200,
                '<form name="loginForm"><input name="userAccount"></form>',
                f"{self.base_url}/xk/LoginToXk",
            )
        raise AssertionError(path)


class BlockingSessionClient(ScriptedSessionClient):
    def __init__(self) -> None:
        super().__init__()
        self.request_started = threading.Event()
        self.release_request = threading.Event()

    def request(self, path: str, **kwargs: object) -> HttpResponse:
        if path == "/ordinary":
            self.request_started.set()
            if not self.release_request.wait(2):
                raise TimeoutError("test did not release ordinary request")
            return HttpResponse(200, "ok", f"{self.base_url}/ordinary")
        return super().request(path, **kwargs)


class HttpTests(unittest.TestCase):
    def test_replay_safe_request_retries_server_error(self) -> None:
        transport = FakeTransport(
            [
                TransportResponse(503, b"busy", "https://jw.sdau.edu.cn/a"),
                TransportResponse(200, "成功".encode(), "https://jw.sdau.edu.cn/a"),
            ]
        )
        sleeps: list[float] = []
        client = JwHttpClient(transport=transport, retry_count=1, sleep=sleeps.append)
        response = client.request("/a")
        self.assertEqual(response.text, "成功")
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(len(sleeps), 1)

    def test_non_replayable_request_is_sent_only_once_on_503(self) -> None:
        transport = FakeTransport(
            [
                TransportResponse(503, b"busy", "https://jw.sdau.edu.cn/register"),
                TransportResponse(200, b"ok", "https://jw.sdau.edu.cn/register"),
            ]
        )
        client = JwHttpClient(transport=transport, retry_count=2, sleep=lambda _: None)
        with self.assertRaises(JwUnavailableError):
            client.request("/register", replay_safe=False)
        self.assertEqual(len(transport.requests), 1)

    def test_unauthorized_is_not_treated_as_normal_body(self) -> None:
        transport = FakeTransport(
            [TransportResponse(401, b"login", "https://jw.sdau.edu.cn/login")]
        )
        with self.assertRaises(UnauthorizedError):
            JwHttpClient(transport=transport).request("/private")

    def test_rejects_cross_origin_request_and_redirect(self) -> None:
        client = JwHttpClient(transport=FakeTransport([]))
        with self.assertRaises(BadRequestError):
            client.request("https://example.com/steal")

        redirect_transport = FakeTransport(
            [TransportResponse(200, b"external", "https://example.com/landing")]
        )
        with self.assertRaises(BadRequestError):
            JwHttpClient(transport=redirect_transport).request("/redirect")

    def test_session_refreshes_once_and_replays_at_most_once(self) -> None:
        client = ScriptedSessionClient()
        session = Session(client, "20260001", "secret")  # type: ignore[arg-type]
        session.login()
        with self.assertRaises(UnauthorizedError):
            session.request("/private")
        self.assertEqual(client.private_calls, 2)
        self.assertEqual(client.login_calls, 2)

    def test_refresh_if_generation_deduplicates_workers(self) -> None:
        client = ScriptedSessionClient()
        session = Session(client, "20260001", "secret")  # type: ignore[arg-type]
        session.login()
        observed = session.auth_generation
        self.assertEqual(session.refresh_if_generation(observed), observed + 1)
        self.assertEqual(session.refresh_if_generation(observed), observed + 1)
        self.assertEqual(client.login_calls, 2)

    def test_fixed_generation_request_never_logs_in_implicitly(self) -> None:
        client = ScriptedSessionClient()
        session = Session(client, "20260001", "secret")  # type: ignore[arg-type]
        session.login()
        generation = session.auth_generation
        login_calls = client.login_calls
        client.cookie_header = ""

        with self.assertRaises(UnauthorizedError):
            session.request(
                "/private",
                retry_unauthorized=False,
                replay_safe=False,
                expected_generation=generation,
            )

        self.assertEqual(client.login_calls, login_calls)
        self.assertEqual(client.private_calls, 0)

    def test_fixed_generation_request_rejects_generation_change(self) -> None:
        client = ScriptedSessionClient()
        session = Session(client, "20260001", "secret")  # type: ignore[arg-type]
        session.login()

        with self.assertRaises(UnauthorizedError):
            session.request(
                "/private",
                retry_unauthorized=False,
                replay_safe=False,
                expected_generation=session.auth_generation - 1,
            )

        self.assertEqual(client.private_calls, 0)

    def test_ordinary_request_holds_auth_transaction_through_transport(self) -> None:
        client = BlockingSessionClient()
        session = Session(client, "20260001", "secret")  # type: ignore[arg-type]
        session.login()
        errors: list[BaseException] = []

        def request() -> None:
            try:
                session.request("/ordinary")
            except BaseException as error:  # pragma: no cover - surfaced below
                errors.append(error)

        transaction_entered = threading.Event()

        def enter_transaction() -> None:
            with session.auth_transaction():
                transaction_entered.set()

        request_thread = threading.Thread(target=request)
        request_thread.start()
        self.assertTrue(client.request_started.wait(1))
        transaction_thread = threading.Thread(target=enter_transaction)
        transaction_thread.start()
        self.assertFalse(transaction_entered.wait(0.05))

        client.release_request.set()
        request_thread.join(1)
        transaction_thread.join(1)
        self.assertFalse(request_thread.is_alive())
        self.assertFalse(transaction_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(transaction_entered.is_set())


if __name__ == "__main__":
    unittest.main()
