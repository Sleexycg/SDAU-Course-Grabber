"""学期格式校验和切换。"""

from __future__ import annotations

from datetime import date, datetime
import re


_TERM_RE = re.compile(r"^(\d{4})-(\d{4})-([12])$")


def _parts(term: str) -> tuple[int, int, int] | None:
    match = _TERM_RE.fullmatch(term.strip())
    if match is None:
        return None
    start, end, number = (int(value) for value in match.groups())
    return (start, end, number) if end == start + 1 else None


def is_valid_term(term: str) -> bool:
    return _parts(term) is not None


def infer_current_term(today: date | datetime | None = None) -> str:
    """Return the date-based default term using the school's cutoff dates."""

    current = today or date.today()
    month_day = (current.month, current.day)
    if month_day >= (7, 20):
        start_year = current.year
        term_number = 1
    elif month_day >= (2, 16):
        start_year = current.year - 1
        term_number = 2
    else:
        start_year = current.year - 1
        term_number = 1
    return f"{start_year}-{start_year + 1}-{term_number}"


def next_term(term: str) -> str:
    parts = _parts(term)
    if parts is None:
        return term
    start, end, number = parts
    return f"{start}-{end}-2" if number == 1 else f"{start + 1}-{end + 1}-1"


def previous_term(term: str) -> str:
    parts = _parts(term)
    if parts is None:
        return term
    start, end, number = parts
    return f"{start}-{end}-1" if number == 2 else f"{start - 1}-{end - 1}-2"


__all__ = ["infer_current_term", "is_valid_term", "next_term", "previous_term"]
