"""Small terminal UI helpers shared by the menu and argparse commands."""

from __future__ import annotations

import os
import sys
import unicodedata
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TypeVar

from .config import CourseItem
from .term import is_valid_term, next_term, previous_term


BOX_WIDTH = 40
T = TypeVar("T")


def configure_console() -> None:
    """Make Chinese status output robust on modern Windows terminals."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def clear_screen() -> None:
    if not sys.stdout.isatty():
        return
    # ANSI is supported by current Windows Terminal and avoids spawning a shell.
    print("\033[2J\033[H", end="", flush=True)


def visual_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _center_visual(text: str, width: int) -> str:
    padding = max(0, width - visual_width(text))
    return " " * (padding // 2) + text + " " * (padding - padding // 2)


def print_box(title: str) -> None:
    inner = BOX_WIDTH - 2
    print("╔" + "═" * inner + "╗")
    print("║" + _center_visual(title, inner) + "║")
    print("╚" + "═" * inner + "╝")


def _log_timestamp() -> str:
    now = datetime.now()
    return f"{now.year}/{now.month}/{now.day} {now:%H:%M:%S}"


def info(message: str) -> None:
    print(f"[{_log_timestamp()}] [INFO] {message}")


def success(message: str, *, check: bool = False) -> None:
    suffix = " ✅" if check else ""
    print(f"[{_log_timestamp()}] [SUCCESS] {message}{suffix}")


def warning(message: str) -> None:
    print(f"⚠ {message}", file=sys.stderr)


def error(message: str) -> None:
    print(f"✗ {message}", file=sys.stderr)


def prompt(message: str, *, default: str | None = None) -> str:
    suffix = f" [{default}]" if default not in {None, ""} else ""
    try:
        answer = input(f"{message}{suffix}: ").strip()
    except EOFError:
        return default or ""
    return answer if answer else (default or "")


def prompt_secret(message: str, *, has_current: bool = False) -> str:
    from getpass import getpass

    suffix = " [已设置，留空保持不变]" if has_current else ""
    try:
        return getpass(f"{message}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def confirm(message: str, *, default: bool = False) -> bool:
    """Ask a yes/no question with conventional and explicit default behavior."""

    marker = "Y/n" if default else "y/N"
    while True:
        try:
            answer = input(f"{message} ({marker}): ").strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        if answer in {"y", "yes", "是", "好", "确认"}:
            return True
        if answer in {"n", "no", "否", "取消"}:
            return False
        warning("请输入 y 或 n。")


def prompt_int(
    message: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    while True:
        raw = prompt(message, default=str(default))
        try:
            value = int(raw)
        except ValueError:
            warning("请输入整数。")
            continue
        if minimum <= value <= maximum:
            return value
        warning(f"请输入 {minimum}-{maximum} 之间的整数。")


def wait_key() -> None:
    if not sys.stdin.isatty():
        return
    try:
        input("\n按回车键返回菜单...")
    except (EOFError, KeyboardInterrupt):
        pass


def _supports_key_navigation() -> bool:
    return sys.stdin.isatty() and (sys.platform == "win32" or os.name == "posix")


def _read_navigation_key() -> str | None:
    """Read one navigation key without requiring Enter."""

    if not _supports_key_navigation():
        return None

    if sys.platform == "win32":
        import msvcrt

        key = msvcrt.getwch()
        if key == "\x03":
            raise KeyboardInterrupt
        if key in {"\x00", "\xe0"}:
            return {"H": "up", "P": "down"}.get(msvcrt.getwch())
        if key in {"\r", "\n"}:
            return "enter"
        if key == "\x1b":
            return "escape"
        return key.casefold()

    try:
        import select
        import termios
        import tty

        descriptor = sys.stdin.fileno()
        previous = termios.tcgetattr(descriptor)
        try:
            tty.setraw(descriptor)
            first = os.read(descriptor, 1)
            if first == b"\x03":
                raise KeyboardInterrupt
            if first in {b"\r", b"\n"}:
                return "enter"
            if first != b"\x1b":
                return first.decode(errors="ignore").casefold()

            sequence = b""
            for _ in range(2):
                ready, _, _ = select.select([descriptor], [], [], 0.05)
                if not ready:
                    return "escape"
                sequence += os.read(descriptor, 1)
            return {b"[A": "up", b"OA": "up", b"[B": "down", b"OB": "down"}.get(
                sequence, "escape"
            )
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)
    except (ImportError, OSError, ValueError):
        return None


def _prompt_term(current: str, *, title: str) -> str:
    """Line-based fallback for redirected or unsupported terminals."""

    while True:
        clear_screen()
        print_box(title)
        print(f"\n当前学期：{current}（输入 p/n 切换,Enter确认）")
        print("也可直接输入 YYYY-YYYY-1/2")
        answer = prompt("选择", default="").lower()
        if not answer:
            return current
        if answer in {"p", "prev", "w", "up"}:
            current = previous_term(current)
            continue
        if answer in {"n", "next", "s", "down"}:
            current = next_term(current)
            continue
        if is_valid_term(answer):
            return answer
        warning("学期格式无效，例如：2026-2027-1。")


def select_term(default_term: str, *, title: str = "选择学期") -> str:
    """Select a term with arrow-key scrolling and a line-input fallback."""

    if not _supports_key_navigation():
        return _prompt_term(default_term, title=title)

    current = default_term
    while True:
        clear_screen()
        print_box(title)
        print(f"\n当前学期：{current}（通过↑/↓键切换,Enter确认）")

        key = _read_navigation_key()
        if key in {"up", "p", "w"}:
            current = previous_term(current)
        elif key in {"down", "n", "s"}:
            current = next_term(current)
        elif key == "enter":
            return current
        elif key == "escape":
            return default_term
        elif key in {"e", "edit"}:
            while True:
                manual = prompt("请输入学期", default=current)
                if is_valid_term(manual):
                    return manual
                warning("学期格式无效，例如：2026-2027-1。")
        elif key is None:
            return _prompt_term(current, title=title)


def choose_many(
    items: Sequence[T],
    *,
    label: Callable[[T], str] = str,
    message: str = "请选择序号（多个用逗号或空格分隔）",
) -> list[T]:
    if not items:
        return []
    for index, item in enumerate(items, start=1):
        print(f"  {index:>{len(str(len(items)))}}. {label(item)}")
    raw = prompt(message)
    if not raw:
        return []
    normalized = raw.replace("，", ",").replace(";", ",").replace("；", ",")
    pieces = [piece for group in normalized.split(",") for piece in group.split()]
    indices: list[int] = []
    for piece in pieces:
        try:
            index = int(piece)
        except ValueError:
            continue
        if 1 <= index <= len(items) and index not in indices:
            indices.append(index)
    return [items[index - 1] for index in indices]


def choose_courses(courses: Sequence[CourseItem]) -> list[CourseItem]:
    return choose_many(
        courses,
        label=lambda course: f"{course.name}（{course.id}）",
        message="请选择课程序号（多个用逗号或空格分隔）",
    )


def comma_items(raw: str) -> list[str]:
    normalized = raw.replace("，", ",").replace(";", ",").replace("；", ",")
    return [part.strip() for group in normalized.split(",") for part in group.split() if part.strip()]


__all__ = [
    "BOX_WIDTH",
    "choose_courses",
    "choose_many",
    "clear_screen",
    "comma_items",
    "configure_console",
    "confirm",
    "error",
    "info",
    "print_box",
    "prompt",
    "prompt_int",
    "prompt_secret",
    "select_term",
    "success",
    "visual_width",
    "wait_key",
    "warning",
]
