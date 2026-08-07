"""Configuration loading and persistence for the Python application."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Mapping

from .crypto import decrypt_secret_or_raise, encrypt_secret, is_encrypted
from .http import DEFAULT_BASE_URL, DEFAULT_USER_AGENT


DEFAULTS: Final[dict[str, str]] = {
    "JW_BASE_URL": DEFAULT_BASE_URL,
    "STUDENT_ID": "",
    "PASSWORD": "",
    "TARGET_COURSE_IDS": "",
    "TARGET_COURSE_NAMES": "",
    "POLL_INTERVAL_MS": "800",
    "JW_TIMEOUT_MS": "12000",
    "JW_RETRY_COUNT": "2",
    "JW_USER_AGENT": DEFAULT_USER_AGENT,
}

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigError(ValueError):
    """Raised when persisted configuration is invalid."""


def is_frozen() -> bool:
    """Return whether the app is running from a PyInstaller executable."""

    return bool(getattr(sys, "frozen", False))


def config_dir() -> Path:
    """Directory containing user-editable configuration and course data."""

    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def config_path() -> Path:
    return config_dir() / ".env.local"


def bundled_dir() -> Path:
    """Return PyInstaller's extraction directory, or the source project root."""

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass).resolve()
    return Path(__file__).resolve().parent.parent


def builtin_config_path() -> Path:
    """Location of the optional configuration bundled by PyInstaller."""

    return bundled_dir() / "builtin_config.json"


def _unquote_env_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, str) else str(decoded)
        except json.JSONDecodeError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Parse the small dotenv subset used by this project."""

    env_path = Path(path)
    if not env_path.is_file():
        return {}

    result: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {env_path}: {exc}") from exc

    for line_number, original in enumerate(lines, start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_RE.fullmatch(key):
            raise ConfigError(f"{env_path} 第 {line_number} 行的配置名无效: {key!r}")
        result[key] = _unquote_env_value(raw_value)
    return result


def _load_builtin_config() -> dict[str, str]:
    path = builtin_config_path()
    if not path.is_file():
        return {}
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取内置配置 {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"内置配置 {path} 必须是 JSON 对象")
    return {str(key): str(value) for key, value in document.items() if value is not None}


def load_raw_config(base_dir: str | Path | None = None) -> dict[str, str]:
    """Load merged raw values without decrypting secrets.

    Precedence, from lowest to highest, is defaults, bundled config, ``.env``,
    ``.env.local`` and process environment.
    """

    root = Path(base_dir).resolve() if base_dir is not None else config_dir()
    merged = dict(DEFAULTS)
    merged.update(_load_builtin_config())
    merged.update(parse_env_file(root / ".env"))
    merged.update(parse_env_file(root / ".env.local"))
    for key in DEFAULTS:
        if key in os.environ:
            merged[key] = os.environ[key]
    return merged


def _csv_items(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _positive_int(
    values: Mapping[str, str], key: str, *, minimum: int = 1, maximum: int | None = None
) -> int:
    raw = str(values.get(key, DEFAULTS[key])).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} 必须是整数，当前值为 {raw!r}") from exc
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f">= {minimum}" if maximum is None else f"{minimum}-{maximum}"
        raise ConfigError(f"{key} 必须在 {bounds} 范围内，当前值为 {value}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    jw_base_url: str = DEFAULTS["JW_BASE_URL"]
    student_id: str = ""
    password: str = ""
    target_course_ids: tuple[str, ...] = ()
    target_course_names: tuple[str, ...] = ()
    poll_interval_ms: int = 800
    jw_timeout_ms: int = 12000
    jw_retry_count: int = 2
    jw_user_agent: str = DEFAULT_USER_AGENT

    @property
    def configured(self) -> bool:
        return bool(self.student_id and self.password)

    @property
    def jw_timeout_seconds(self) -> float:
        return self.jw_timeout_ms / 1000.0

    def to_env(self, *, encrypt_password: bool = True) -> dict[str, str]:
        password = self.password
        if password and encrypt_password and not is_encrypted(password):
            password = encrypt_secret(password)
        return {
            "JW_BASE_URL": self.jw_base_url.rstrip("/"),
            "STUDENT_ID": self.student_id,
            "PASSWORD": password,
            "TARGET_COURSE_IDS": ",".join(self.target_course_ids),
            "TARGET_COURSE_NAMES": ",".join(self.target_course_names),
            "POLL_INTERVAL_MS": str(self.poll_interval_ms),
            "JW_TIMEOUT_MS": str(self.jw_timeout_ms),
            "JW_RETRY_COUNT": str(self.jw_retry_count),
            "JW_USER_AGENT": self.jw_user_agent,
        }


def settings_from_mapping(
    values: Mapping[str, str], *, decrypt_password: bool = True
) -> Settings:
    raw_password = str(values.get("PASSWORD", "")).strip()
    password = raw_password
    if raw_password and is_encrypted(raw_password) and decrypt_password:
        password = decrypt_secret_or_raise(raw_password)

    poll = _positive_int(values, "POLL_INTERVAL_MS", minimum=300, maximum=3_600_000)

    base_url = str(values.get("JW_BASE_URL", DEFAULTS["JW_BASE_URL"])).strip().rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise ConfigError("JW_BASE_URL 必须以 http:// 或 https:// 开头")

    course_ids = _csv_items(str(values.get("TARGET_COURSE_IDS", "")))
    course_names = _csv_items(str(values.get("TARGET_COURSE_NAMES", "")))
    if course_names and len(course_names) != len(course_ids):
        raise ConfigError(
            "TARGET_COURSE_NAMES 非空时必须与 TARGET_COURSE_IDS 数量一致，避免课程名称错位"
        )

    return Settings(
        jw_base_url=base_url,
        student_id=str(values.get("STUDENT_ID", "")).strip(),
        password=password,
        target_course_ids=course_ids,
        target_course_names=course_names,
        poll_interval_ms=poll,
        jw_timeout_ms=_positive_int(values, "JW_TIMEOUT_MS", minimum=100, maximum=300_000),
        jw_retry_count=_positive_int(values, "JW_RETRY_COUNT", minimum=0, maximum=20),
        jw_user_agent=str(values.get("JW_USER_AGENT", DEFAULT_USER_AGENT)).strip()
        or DEFAULT_USER_AGENT,
    )


def load_settings(
    base_dir: str | Path | None = None, *, decrypt_password: bool = True
) -> Settings:
    return settings_from_mapping(
        load_raw_config(base_dir), decrypt_password=decrypt_password
    )


def _quote_env_value(value: str) -> str:
    if not value:
        return ""
    if value != value.strip() or any(char in value for char in ('#', '"', "\n", "\r")):
        return json.dumps(value, ensure_ascii=False)
    return value


def save_settings(settings: Settings, path: str | Path | None = None) -> Path:
    """Atomically save settings using the versioned encrypted password format."""

    target = Path(path).resolve() if path is not None else config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    values = settings.to_env(encrypt_password=True)
    ordered_keys = tuple(DEFAULTS)
    lines = [f"# 生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    lines.extend(f"{key}={_quote_env_value(values[key])}" for key in ordered_keys)
    lines.append("")
    content = "\n".join(lines)

    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, target)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConfigError(f"无法保存配置 {target}: {exc}") from exc
    return target


@dataclass(frozen=True, slots=True)
class CourseItem:
    id: str
    name: str


def load_course_list(path: str | Path | None = None) -> list[CourseItem]:
    course_path = Path(path) if path is not None else config_dir() / "courses.json"
    if not course_path.is_file():
        bundled = bundled_dir() / "courses.json"
        course_path = bundled if bundled.is_file() else course_path
    if not course_path.is_file():
        return []
    try:
        data: Any = json.loads(course_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取课程列表 {course_path}: {exc}") from exc
    if not isinstance(data, list):
        raise ConfigError(f"课程列表 {course_path} 必须是 JSON 数组")

    courses: list[CourseItem] = []
    seen_by_id: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        course_id = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip()
        if not course_id or not name:
            continue
        existing_name = seen_by_id.get(course_id)
        if existing_name is not None and existing_name != name:
            raise ConfigError(
                f"课程列表中 ID {course_id!r} 同时对应 {existing_name!r} 和 {name!r}，"
                "请先修正 courses.json"
            )
        if existing_name is None:
            courses.append(CourseItem(course_id, name))
            seen_by_id[course_id] = name
    return courses


__all__ = [
    "ConfigError",
    "CourseItem",
    "Settings",
    "builtin_config_path",
    "bundled_dir",
    "config_dir",
    "config_path",
    "is_frozen",
    "load_course_list",
    "load_raw_config",
    "load_settings",
    "parse_env_file",
    "save_settings",
    "settings_from_mapping",
]
