"""教务系统登录和会话刷新。"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from html import unescape
import re
import threading
from typing import Iterator
from urllib.parse import urlsplit

from .encoding import build_encoded_credential
from .errors import InvalidCredentialsError, JwUnavailableError, UnauthorizedError
from .http import DEFAULT_ACCEPT, HttpMethod, HttpResponse, JwHttpClient


_LOGIN_PATH = "/xk/LoginToXk"
_SCODE_RE = re.compile(r"\bvar\s+scode\s*=\s*(['\"])(.*?)\1\s*;?", re.I | re.S)
_SXH_RE = re.compile(r"\bvar\s+sxh\s*=\s*(['\"])(.*?)\1\s*;?", re.I | re.S)
_LOGIN_FORM_RE = re.compile(
    r"name\s*=\s*['\"]loginForm['\"]|"
    r"欢迎登录教务系统|请先登录系统|"
    r"name\s*=\s*['\"](?:userAccount|encoded)['\"]",
    re.I,
)
_LOGIN_MESSAGE_RE = re.compile(
    r"id\s*=\s*['\"]showMsg['\"][^>]*>(.*?)</[^>]+>",
    re.I | re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")


class Session:
    """一个可由多个抢课 worker 共享的登录会话。"""

    def __init__(self, client: JwHttpClient, student_id: str, password: str) -> None:
        if not student_id.strip():
            raise ValueError("student_id 不能为空")
        if not password:
            raise ValueError("password 不能为空")
        self.client = client
        self.student_id = student_id.strip()
        self.password = password
        self._lock = threading.RLock()
        self._authenticated = False
        self._auth_generation = 0

    @property
    def auth_generation(self) -> int:
        with self._lock:
            return self._auth_generation

    def login(self) -> str:
        with self._lock:
            return self._login_locked()

    @contextmanager
    def auth_transaction(self) -> Iterator[None]:
        """Serialize authentication state and all CookieJar-dependent I/O.

        Callers that need several authenticated requests to share one session
        generation can hold this transaction across the complete operation.
        The underlying lock is re-entrant, so calls to :meth:`request`,
        :meth:`login` and :meth:`refresh_if_generation` remain safe inside it.
        """

        with self._lock:
            yield

    def refresh_if_generation(self, observed_generation: int) -> int:
        """仅当会话仍是调用方观察到的代次时重新登录。"""

        with self._lock:
            if self._auth_generation != observed_generation:
                return self._auth_generation
            self._login_locked()
            return self._auth_generation

    def request(
        self,
        path: str,
        *,
        method: HttpMethod = "GET",
        data: Mapping[str, str] | None = None,
        referer: str | None = None,
        accept: str = DEFAULT_ACCEPT,
        retry_unauthorized: bool = True,
        replay_safe: bool = True,
        expected_generation: int | None = None,
    ) -> HttpResponse:
        """发送认证请求，安全请求遇登录失效时最多重放一次。"""

        if retry_unauthorized and not replay_safe:
            raise ValueError("不可重放请求不能启用自动登录重试")
        if expected_generation is not None and retry_unauthorized:
            raise ValueError("固定会话代次的请求不能启用自动登录重试")

        # Every transport operation is coordinated by the same re-entrant lock.
        # Besides protecting authentication flags, this prevents another thread
        # from clearing/replacing the CookieJar while an ordinary request is in
        # flight.  Multi-request operations can extend this critical section via
        # ``auth_transaction()``.
        with self._lock:
            if expected_generation is None:
                self._ensure_authenticated()
            elif (
                self._auth_generation != expected_generation
                or not self._authenticated
                or not self.client.cookie_header
            ):
                raise UnauthorizedError(
                    "会话代次已变化或登录状态无效；拒绝在未恢复选课期次时提交",
                    endpoint=path,
                )

            observed_generation = self.auth_generation

            try:
                response = self.client.request(
                    path,
                    method=method,
                    data=data,
                    referer=referer,
                    accept=accept,
                    replay_safe=replay_safe,
                )
            except UnauthorizedError:
                self._mark_unauthorized(observed_generation)
                if not retry_unauthorized:
                    raise
            else:
                if not _is_login_response(response):
                    return response
                self._mark_unauthorized(observed_generation)
                if not retry_unauthorized:
                    raise UnauthorizedError(endpoint=path)

            self.refresh_if_generation(observed_generation)
            retry_generation = self.auth_generation
            try:
                retried = self.client.request(
                    path,
                    method=method,
                    data=data,
                    referer=referer,
                    accept=accept,
                    replay_safe=replay_safe,
                )
            except UnauthorizedError:
                self._mark_unauthorized(retry_generation)
                raise
            if _is_login_response(retried):
                self._mark_unauthorized(retry_generation)
                raise UnauthorizedError(
                    "重新登录后请求仍返回登录页；会话端点可能已改变或账号不可登录",
                    status=retried.status,
                    endpoint=path,
                )
            return retried

    def _ensure_authenticated(self) -> None:
        with self._lock:
            if not self._authenticated or not self.client.cookie_header:
                self._login_locked()

    def _mark_unauthorized(self, generation: int) -> None:
        with self._lock:
            if self._auth_generation == generation:
                self._authenticated = False

    def _login_locked(self) -> str:
        self.client.clear_cookies()
        self._authenticated = False

        login_page = self.client.request("/")
        scode, sxh = _extract_login_seed(login_page.text)
        encoded = build_encoded_credential(
            self.student_id,
            self.password,
            scode,
            sxh,
        )
        form = {
            "loginMethod": "LoginToXk",
            "userlanguage": "0",
            "userAccount": self.student_id,
            "userPassword": "",
            "encoded": encoded,
        }

        try:
            result = self.client.request(
                _LOGIN_PATH,
                method="POST",
                data=form,
                referer=login_page.final_url,
            )
        except UnauthorizedError as error:
            raise InvalidCredentialsError() from error

        login_message = _parse_login_message(result.text)
        if login_message and "请先登录系统" not in login_message:
            raise InvalidCredentialsError(login_message)
        if _is_login_response(result):
            raise InvalidCredentialsError()
        if not self.client.cookie_header:
            raise JwUnavailableError(
                "登录响应未设置会话 Cookie；登录端点或流程可能已经改变",
                endpoint=_LOGIN_PATH,
            )

        self._authenticated = True
        self._auth_generation += 1
        return self.client.cookie_header


def _extract_login_seed(html: str) -> tuple[str, str]:
    scode_match = _SCODE_RE.search(html)
    sxh_match = _SXH_RE.search(html)
    scode = unescape(scode_match.group(2)).strip() if scode_match else ""
    sxh = unescape(sxh_match.group(2)).strip() if sxh_match else ""
    if not scode or not sxh:
        raise JwUnavailableError(
            "无法从登录页提取 scode/sxh 混淆参数；登录页结构可能已经改变",
            endpoint="/",
        )
    return scode, sxh


def _parse_login_message(html: str) -> str:
    match = _LOGIN_MESSAGE_RE.search(html)
    if match is None:
        return ""
    return " ".join(unescape(_TAG_RE.sub("", match.group(1))).split())


def _is_login_response(response: HttpResponse) -> bool:
    if response.status in {401, 403}:
        return True
    path = urlsplit(response.final_url).path.casefold().rstrip("/")
    return (
        path.endswith("/xk/logintoxk")
        or path.endswith("/login")
        or _LOGIN_FORM_RE.search(response.text) is not None
    )


__all__ = ["Session"]
