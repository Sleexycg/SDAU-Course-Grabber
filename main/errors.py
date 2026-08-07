"""教务系统协议错误。"""

from __future__ import annotations

class JwError(RuntimeError):
    """所有协议错误的基类。"""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        endpoint: str | None = None,
        response_excerpt: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.endpoint = endpoint
        self.response_excerpt = response_excerpt


class JwUnavailableError(JwError):
    pass


class InvalidCredentialsError(JwError):
    def __init__(self, message: str = "学号或密码错误，或账号当前不可登录") -> None:
        super().__init__(message)


class UnauthorizedError(JwError):
    def __init__(
        self,
        message: str = "登录状态已失效，请重新登录",
        *,
        status: int | None = None,
        endpoint: str | None = None,
    ) -> None:
        super().__init__(message, status=status, endpoint=endpoint)


class BadRequestError(JwError):
    pass


class RateLimitedError(JwError):
    def __init__(
        self,
        message: str = "教务系统请求过于频繁",
        *,
        status: int = 429,
        endpoint: str | None = None,
    ) -> None:
        super().__init__(message, status=status, endpoint=endpoint)


class ResponseFormatError(JwError):
    pass


class RegistrationOutcomeUnknown(JwError):
    """注册请求可能已生效，但客户端无法确认最终结果。"""


class SelectionPeriodNotFoundError(JwError):
    pass
