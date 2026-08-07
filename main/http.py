"""基于 urllib 的小型同源 HTTP 客户端。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar
import re
import socket
import time
from typing import Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, OpenerDirector, Request, build_opener

from .errors import (
    BadRequestError,
    JwUnavailableError,
    RateLimitedError,
    UnauthorizedError,
)


DEFAULT_BASE_URL = "https://jw.sdau.edu.cn"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

HttpMethod = Literal["GET", "POST"]


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: HttpMethod
    url: str
    headers: Mapping[str, str]
    body: bytes | None = None


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status: int
    body: bytes
    final_url: str
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    text: str
    final_url: str


class HttpTransport(Protocol):
    def send(self, request: HttpRequest, *, timeout: float) -> TransportResponse:
        ...

    def cookie_header(self, *, url: str) -> str:
        ...

    def clear_cookies(self) -> None:
        ...


class _ReadableResponse(Protocol):
    status: int
    headers: Message

    def read(self, amount: int | None = None) -> bytes:
        ...

    def geturl(self) -> str:
        ...

    def close(self) -> None:
        ...


class _Opener(Protocol):
    def open(
        self,
        fullurl: Request,
        data: bytes | None = None,
        timeout: float = ...,
    ) -> _ReadableResponse:
        ...


class UrllibTransport:
    """保留重定向中间响应 Cookie 的默认传输实现。"""

    def __init__(
        self,
        *,
        cookie_jar: CookieJar | None = None,
        opener: OpenerDirector | None = None,
    ) -> None:
        self.cookie_jar = cookie_jar if cookie_jar is not None else CookieJar()
        active_opener = opener or build_opener(HTTPCookieProcessor(self.cookie_jar))
        self._opener = cast(_Opener, active_opener)

    def send(self, request: HttpRequest, *, timeout: float) -> TransportResponse:
        urllib_request = Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        response: _ReadableResponse | None = None
        try:
            response = self._opener.open(urllib_request, timeout=timeout)
            return TransportResponse(
                status=response.status,
                body=response.read(),
                final_url=response.geturl(),
                headers=_header_pairs(response.headers),
            )
        except HTTPError as error:
            try:
                return TransportResponse(
                    status=error.code,
                    body=error.read(),
                    final_url=error.geturl(),
                    headers=_header_pairs(error.headers),
                )
            finally:
                error.close()
        finally:
            if response is not None:
                response.close()

    def cookie_header(self, *, url: str) -> str:
        request = Request(url)
        self.cookie_jar.add_cookie_header(request)
        return request.get_header("Cookie", "")

    def clear_cookies(self) -> None:
        self.cookie_jar.clear()


class JwHttpClient:
    """只允许访问配置教务系统同源地址的同步客户端。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 12.0,
        retry_count: int = 2,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: HttpTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        normalized = base_url.rstrip("/")
        parts = urlsplit(normalized)
        if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
            raise ValueError("base_url 必须是包含 http/https 和主机名的绝对 URL")
        if timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        if retry_count < 0:
            raise ValueError("retry_count 不能小于 0")

        self.base_url = normalized
        self.timeout = timeout
        self.retry_count = retry_count
        self.user_agent = user_agent
        self.transport = transport if transport is not None else UrllibTransport()
        self._sleep = sleep
        self._origin = _origin(normalized)

    @property
    def cookie_header(self) -> str:
        return self.transport.cookie_header(url=f"{self.base_url}/")

    def clear_cookies(self) -> None:
        self.transport.clear_cookies()

    def request(
        self,
        path: str,
        *,
        method: HttpMethod = "GET",
        data: Mapping[str, str] | None = None,
        referer: str | None = None,
        accept: str = DEFAULT_ACCEPT,
        replay_safe: bool = True,
    ) -> HttpResponse:
        """发送请求；只有 ``replay_safe`` 请求才会自动重试。"""

        url = self._resolve_url(path)
        body = urlencode(data).encode("utf-8") if data is not None else None
        headers = {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if method == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        if referer:
            headers["Referer"] = referer

        request = HttpRequest(method=method, url=url, headers=headers, body=body)
        retry_limit = self.retry_count if replay_safe else 0

        for attempt in range(retry_limit + 1):
            try:
                raw = self.transport.send(request, timeout=self.timeout)
            except (URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as error:
                if attempt < retry_limit:
                    self._sleep(_backoff_seconds(attempt))
                    continue
                raise JwUnavailableError(
                    f"教务系统请求失败（已重试 {retry_limit} 次）：{_network_error_text(error)}",
                    endpoint=url,
                ) from error

            if _origin(raw.final_url) != self._origin:
                raise BadRequestError(
                    "教务系统响应重定向到了不同源地址，已拒绝继续处理",
                    endpoint=raw.final_url,
                )

            response = HttpResponse(
                status=raw.status,
                text=_decode_body(raw.body, raw.headers),
                final_url=raw.final_url,
            )
            if raw.status in _RETRYABLE_STATUSES and attempt < retry_limit:
                retry_after = _retry_after_seconds(_find_header(raw.headers, "Retry-After"))
                self._sleep(retry_after if retry_after is not None else _backoff_seconds(attempt))
                continue
            return self._validate_status(response, endpoint=url)

        raise AssertionError("HTTP retry loop ended unexpectedly")

    def _resolve_url(self, path: str) -> str:
        candidate = urljoin(f"{self.base_url}/", path)
        if _origin(candidate) != self._origin:
            raise BadRequestError("拒绝向教务系统以外的地址发送请求", endpoint=candidate)
        return candidate

    @staticmethod
    def _validate_status(response: HttpResponse, *, endpoint: str) -> HttpResponse:
        status = response.status
        if 200 <= status < 300:
            return response

        excerpt = _excerpt(response.text)
        if status in {401, 403}:
            raise UnauthorizedError(status=status, endpoint=endpoint)
        if status == 429:
            raise RateLimitedError(
                f"教务系统返回 HTTP 429，请降低请求频率。响应：{excerpt or '无正文'}",
                endpoint=endpoint,
            )
        if 400 <= status < 500:
            raise BadRequestError(
                f"教务系统拒绝请求（HTTP {status}）。响应：{excerpt or '无正文'}",
                status=status,
                endpoint=endpoint,
                response_excerpt=excerpt,
            )
        raise JwUnavailableError(
            f"教务系统暂时不可用（HTTP {status}）。响应：{excerpt or '无正文'}",
            status=status,
            endpoint=endpoint,
            response_excerpt=excerpt,
        )


def _decode_body(body: bytes, headers: tuple[tuple[str, str], ...]) -> str:
    content_type = _find_header(headers, "Content-Type") or ""
    charset_match = re.search(r"charset\s*=\s*[\"']?([\w.-]+)", content_type, re.I)
    candidates = [charset_match.group(1)] if charset_match else []

    prefix = body[:4096].decode("ascii", errors="ignore")
    meta_match = re.search(r"charset\s*=\s*[\"']?([\w.-]+)", prefix, re.I)
    if meta_match:
        candidates.append(meta_match.group(1))
    candidates.extend(("utf-8-sig", "gb18030"))

    tried: set[str] = set()
    for charset in candidates:
        normalized = charset.casefold()
        if normalized in tried:
            continue
        tried.add(normalized)
        try:
            return body.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _header_pairs(headers: Message | None) -> tuple[tuple[str, str], ...]:
    if headers is None:
        return ()
    return tuple((str(name), str(value)) for name, value in headers.items())


def _find_header(headers: tuple[tuple[str, str], ...], name: str) -> str | None:
    expected = name.casefold()
    for key, value in reversed(headers):
        if key.casefold() == expected:
            return value
    return None


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.casefold()
    try:
        port = parts.port
    except ValueError:
        return scheme, (parts.hostname or "").casefold(), -1
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, (parts.hostname or "").casefold(), port


def _backoff_seconds(attempt: int) -> float:
    return min(0.5 * (2**attempt), 5.0)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    stripped = value.strip()
    if stripped.isdigit():
        return min(float(stripped), 30.0)
    try:
        retry_at = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
    return min(max(seconds, 0.0), 30.0)


def _network_error_text(error: BaseException) -> str:
    if isinstance(error, URLError) and error.reason:
        return str(error.reason)
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "请求超时"
    return str(error) or error.__class__.__name__


def _excerpt(text: str, limit: int = 240) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else f"{compact[:limit]}…"
