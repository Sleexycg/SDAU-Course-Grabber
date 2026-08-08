"""Render enrolled courses as a PNG timetable using Windows GDI+ only."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re
import struct
import sys
import tempfile
import threading
import unicodedata
from typing import Any, Final

from .models import CourseMeeting, EnrolledCourse, EnrolledCourseResult


_IS_WINDOWS = sys.platform == "win32"
_WEEKDAY_NUMBERS: Final[dict[str, int]] = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "日": 7,
}
WEEKDAY_LABELS: Final[tuple[str, ...]] = (
    "",
    "周一",
    "周二",
    "周三",
    "周四",
    "周五",
    "周六",
    "周日",
)
_MEETING_RE = re.compile(
    r"^周(?P<weekday>[一二三四五六日])\s*"
    r"(?P<start>\d+)(?:-(?P<end>\d+))?节$"
)
_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_MAX_COURSES: Final = 500
_MAX_TEXT_LENGTH: Final = 160
_MAX_IMAGE_HEIGHT: Final = 30_000
_EXPORT_LOCK = threading.Lock()

_MARGIN = 40
_TABLE_TOP = 124
_SLOT_COLUMN_WIDTH = 100
_HEADER_HEIGHT = 56
_EMPTY_TABLE_HEIGHT = 66
_ENTRY_LINE_HEIGHT = 21
_ENTRY_PADDING = 7
_ENTRY_GAP = 5
_PENDING_TITLE_GAP = 34
_PENDING_TITLE_HEIGHT = 38
_PENDING_LINE_HEIGHT = 21


class TimetableExportError(RuntimeError):
    """Raised when a timetable cannot be planned, rendered, or saved."""


@dataclass(frozen=True, slots=True)
class TimetableSlot:
    start: int
    end: int

    @property
    def label(self) -> str:
        return f"{self.start}节" if self.start == self.end else f"{self.start}-{self.end}节"


@dataclass(frozen=True, slots=True)
class TimetableEntry:
    course_name: str
    teacher: str
    location: str


@dataclass(frozen=True, slots=True)
class TimetableCell:
    slot: TimetableSlot
    weekday: int
    entries: tuple[TimetableEntry, ...]


@dataclass(frozen=True, slots=True)
class PendingTimetableEntry:
    course_name: str
    teacher: str
    location: str
    time_label: str


@dataclass(frozen=True, slots=True)
class TimetablePlan:
    term: str
    weekdays: tuple[int, ...]
    slots: tuple[TimetableSlot, ...]
    cells: tuple[TimetableCell, ...]
    pending: tuple[PendingTimetableEntry, ...]

    def entries_at(self, slot: TimetableSlot, weekday: int) -> tuple[TimetableEntry, ...]:
        for cell in self.cells:
            if cell.slot == slot and cell.weekday == weekday:
                return cell.entries
        return ()


@dataclass(frozen=True, slots=True)
class TimetableRowLayout:
    slot: TimetableSlot
    top: int
    height: int


@dataclass(frozen=True, slots=True)
class PendingRowLayout:
    index: int
    top: int
    height: int


@dataclass(frozen=True, slots=True)
class TimetableLayout:
    width: int
    height: int
    table_left: int
    table_top: int
    slot_column_width: int
    day_column_width: int
    header_height: int
    weekdays: tuple[int, ...]
    rows: tuple[TimetableRowLayout, ...]
    empty_table_height: int
    pending_title_top: int | None
    pending_rows: tuple[PendingRowLayout, ...]

    @property
    def table_width(self) -> int:
        return self.slot_column_width + self.day_column_width * len(self.weekdays)

    @property
    def table_bottom(self) -> int:
        if self.rows:
            return self.rows[-1].top + self.rows[-1].height
        return self.table_top + self.header_height + self.empty_table_height


def _clean_text(value: str, fallback: str) -> str:
    normalized = " ".join(str(value).split())
    if not normalized:
        return fallback
    if len(normalized) > _MAX_TEXT_LENGTH:
        return f"{normalized[: _MAX_TEXT_LENGTH - 1]}…"
    return normalized


def _parse_meeting(meeting: CourseMeeting) -> tuple[int, TimetableSlot] | None:
    match = _MEETING_RE.fullmatch(meeting.time.strip())
    if match is None:
        return None
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    if not 1 <= start <= end <= 99:
        return None
    weekday = _WEEKDAY_NUMBERS[match.group("weekday")]
    return weekday, TimetableSlot(start, end)


def _course_entry(course: EnrolledCourse, meeting: CourseMeeting) -> TimetableEntry:
    return TimetableEntry(
        course_name=_clean_text(course.name, "课程名待定"),
        teacher=_clean_text(course.teacher, "教师待定"),
        location=_clean_text(meeting.location, "地点待定"),
    )


def _pending_entry(
    course: EnrolledCourse, meeting: CourseMeeting | None
) -> PendingTimetableEntry:
    location = meeting.location if meeting is not None else ""
    time_label = meeting.time if meeting is not None else ""
    return PendingTimetableEntry(
        course_name=_clean_text(course.name, "课程名待定"),
        teacher=_clean_text(course.teacher, "教师待定"),
        location=_clean_text(location, "地点待定"),
        time_label=_clean_text(time_label, "时间待定"),
    )


def build_timetable_plan(result: EnrolledCourseResult) -> TimetablePlan:
    """Map a query result into deterministic timetable cells without platform APIs."""

    if len(result.courses) > _MAX_COURSES:
        raise TimetableExportError(f"课程数量超过安全上限 {_MAX_COURSES}，拒绝生成异常尺寸图片")

    mapped: dict[tuple[TimetableSlot, int], list[TimetableEntry]] = {}
    pending: list[PendingTimetableEntry] = []
    has_weekend = False
    for course in result.courses:
        if not course.meetings:
            pending.append(_pending_entry(course, None))
            continue
        for meeting in course.meetings:
            parsed = _parse_meeting(meeting)
            if parsed is None:
                pending.append(_pending_entry(course, meeting))
                continue
            weekday, slot = parsed
            has_weekend = has_weekend or weekday >= 6
            mapped.setdefault((slot, weekday), []).append(_course_entry(course, meeting))

    slots = tuple(sorted({slot for slot, _weekday in mapped}, key=lambda item: (item.start, item.end)))
    weekdays = tuple(range(1, 8 if has_weekend else 6))
    cells = tuple(
        TimetableCell(slot, weekday, tuple(mapped[(slot, weekday)]))
        for slot in slots
        for weekday in weekdays
        if (slot, weekday) in mapped
    )
    return TimetablePlan(
        term=_clean_text(result.term, "学期待定"),
        weekdays=weekdays,
        slots=slots,
        cells=cells,
        pending=tuple(pending),
    )


def _visual_units(text: str) -> int:
    units = 0
    for character in text:
        if unicodedata.combining(character):
            continue
        units += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return units


def _wrap_visual(text: str, maximum_units: int) -> tuple[str, ...]:
    value = text.strip()
    if not value:
        return ("",)
    lines: list[str] = []
    remaining = value
    while remaining:
        units = 0
        split_at = 0
        last_space = -1
        for index, character in enumerate(remaining):
            character_units = _visual_units(character)
            if units + character_units > maximum_units and index > 0:
                break
            units += character_units
            split_at = index + 1
            if character.isspace():
                last_space = index
        else:
            split_at = len(remaining)

        if split_at < len(remaining) and last_space > 0:
            split_at = last_space
        if split_at <= 0:
            split_at = 1
        line = remaining[:split_at].strip()
        lines.append(line or remaining[:split_at])
        remaining = remaining[split_at:].lstrip()
    return tuple(lines)


def _entry_lines(
    entry: TimetableEntry, day_column_width: int
) -> tuple[tuple[str, bool], ...]:
    capacity = max(10, int((day_column_width - 2 * _ENTRY_PADDING) / 8))
    lines: list[tuple[str, bool]] = []
    lines.extend((line, True) for line in _wrap_visual(entry.course_name, capacity))
    lines.extend((line, False) for line in _wrap_visual(f"教师：{entry.teacher}", capacity))
    lines.extend((line, False) for line in _wrap_visual(f"地点：{entry.location}", capacity))
    return tuple(lines)


def _cell_height(entries: tuple[TimetableEntry, ...], day_column_width: int) -> int:
    if not entries:
        return 0
    content = sum(
        len(_entry_lines(entry, day_column_width)) * _ENTRY_LINE_HEIGHT
        + 2 * _ENTRY_PADDING
        for entry in entries
    )
    return content + _ENTRY_GAP * (len(entries) - 1)


def _pending_lines(entry: PendingTimetableEntry, table_width: int) -> tuple[tuple[str, bool], ...]:
    capacity = max(20, int((table_width - 32) / 8))
    details = f"教师：{entry.teacher}  地点：{entry.location}  时间：{entry.time_label}"
    lines: list[tuple[str, bool]] = []
    lines.extend((line, True) for line in _wrap_visual(entry.course_name, capacity))
    lines.extend((line, False) for line in _wrap_visual(details, capacity))
    return tuple(lines)


def calculate_timetable_layout(plan: TimetablePlan) -> TimetableLayout:
    """Calculate a bounded pixel layout using only deterministic Python code."""

    day_column_width = 252 if len(plan.weekdays) == 5 else 232
    table_width = _SLOT_COLUMN_WIDTH + day_column_width * len(plan.weekdays)
    width = table_width + 2 * _MARGIN
    cell_map = {(cell.slot, cell.weekday): cell.entries for cell in plan.cells}

    row_layouts: list[TimetableRowLayout] = []
    top = _TABLE_TOP + _HEADER_HEIGHT
    for slot in plan.slots:
        required = max(
            (
                _cell_height(cell_map.get((slot, weekday), ()), day_column_width)
                for weekday in plan.weekdays
            ),
            default=0,
        )
        height = max(76, required)
        row_layouts.append(TimetableRowLayout(slot, top, height))
        top += height

    table_bottom = (
        top
        if row_layouts
        else _TABLE_TOP + _HEADER_HEIGHT + _EMPTY_TABLE_HEIGHT
    )
    pending_title_top: int | None = None
    pending_rows: list[PendingRowLayout] = []
    if plan.pending:
        pending_title_top = table_bottom + _PENDING_TITLE_GAP
        pending_top = pending_title_top + _PENDING_TITLE_HEIGHT
        for index, entry in enumerate(plan.pending):
            line_count = len(_pending_lines(entry, table_width))
            row_height = max(54, line_count * _PENDING_LINE_HEIGHT + 16)
            pending_rows.append(PendingRowLayout(index, pending_top, row_height))
            pending_top += row_height
        content_bottom = pending_top
    else:
        content_bottom = table_bottom

    height = content_bottom + _MARGIN
    if height > _MAX_IMAGE_HEIGHT:
        raise TimetableExportError(
            f"课程表图片高度 {height}px 超过安全上限 {_MAX_IMAGE_HEIGHT}px"
        )
    return TimetableLayout(
        width=width,
        height=height,
        table_left=_MARGIN,
        table_top=_TABLE_TOP,
        slot_column_width=_SLOT_COLUMN_WIDTH,
        day_column_width=day_column_width,
        header_height=_HEADER_HEIGHT,
        weekdays=plan.weekdays,
        rows=tuple(row_layouts),
        empty_table_height=_EMPTY_TABLE_HEIGHT,
        pending_title_top=pending_title_top,
        pending_rows=tuple(pending_rows),
    )


class _RectF(ctypes.Structure):
    _fields_ = [
        ("X", ctypes.c_float),
        ("Y", ctypes.c_float),
        ("Width", ctypes.c_float),
        ("Height", ctypes.c_float),
    ]


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", ctypes.c_int32),
        ("SuppressExternalCodecs", ctypes.c_int32),
    ]


class _ImageCodecInfo(ctypes.Structure):
    _fields_ = [
        ("Clsid", _Guid),
        ("FormatID", _Guid),
        ("CodecName", ctypes.c_wchar_p),
        ("DllName", ctypes.c_wchar_p),
        ("FormatDescription", ctypes.c_wchar_p),
        ("FilenameExtension", ctypes.c_wchar_p),
        ("MimeType", ctypes.c_wchar_p),
        ("Flags", ctypes.c_uint32),
        ("Version", ctypes.c_uint32),
        ("SigCount", ctypes.c_uint32),
        ("SigSize", ctypes.c_uint32),
        ("SigPattern", ctypes.POINTER(ctypes.c_ubyte)),
        ("SigMask", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _bind(dll: Any, name: str, argtypes: list[Any], restype: Any = ctypes.c_int) -> Any:
    function = getattr(dll, name)
    function.argtypes = argtypes
    function.restype = restype
    return function


class _GdiPlusApi:
    def __init__(self) -> None:
        if not _IS_WINDOWS:
            raise TimetableExportError("PNG 课程表导出需要 Windows GDI+")
        try:
            dll = ctypes.WinDLL("gdiplus", use_last_error=True)
        except (AttributeError, OSError) as exc:
            raise TimetableExportError("无法加载 Windows GDI+") from exc

        handle = ctypes.c_void_p
        uint = ctypes.c_uint32
        integer = ctypes.c_int32
        self.startup = _bind(
            dll,
            "GdiplusStartup",
            [ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(_GdiplusStartupInput), ctypes.c_void_p],
        )
        self.shutdown = _bind(dll, "GdiplusShutdown", [ctypes.c_size_t], None)
        self.create_bitmap = _bind(
            dll,
            "GdipCreateBitmapFromScan0",
            [integer, integer, integer, integer, ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(handle)],
        )
        self.dispose_image = _bind(dll, "GdipDisposeImage", [handle])
        self.get_graphics = _bind(dll, "GdipGetImageGraphicsContext", [handle, ctypes.POINTER(handle)])
        self.delete_graphics = _bind(dll, "GdipDeleteGraphics", [handle])
        self.clear = _bind(dll, "GdipGraphicsClear", [handle, uint])
        self.set_smoothing = _bind(dll, "GdipSetSmoothingMode", [handle, integer])
        self.set_text_rendering = _bind(dll, "GdipSetTextRenderingHint", [handle, integer])
        self.create_solid_fill = _bind(dll, "GdipCreateSolidFill", [uint, ctypes.POINTER(handle)])
        self.delete_brush = _bind(dll, "GdipDeleteBrush", [handle])
        self.create_pen = _bind(
            dll, "GdipCreatePen1", [uint, ctypes.c_float, integer, ctypes.POINTER(handle)]
        )
        self.delete_pen = _bind(dll, "GdipDeletePen", [handle])
        self.fill_rectangle = _bind(
            dll, "GdipFillRectangleI", [handle, handle, integer, integer, integer, integer]
        )
        self.draw_line = _bind(
            dll, "GdipDrawLineI", [handle, handle, integer, integer, integer, integer]
        )
        self.create_family = _bind(
            dll,
            "GdipCreateFontFamilyFromName",
            [ctypes.c_wchar_p, handle, ctypes.POINTER(handle)],
        )
        self.get_generic_sans = _bind(
            dll, "GdipGetGenericFontFamilySansSerif", [ctypes.POINTER(handle)]
        )
        self.delete_family = _bind(dll, "GdipDeleteFontFamily", [handle])
        self.create_font = _bind(
            dll,
            "GdipCreateFont",
            [handle, ctypes.c_float, integer, integer, ctypes.POINTER(handle)],
        )
        self.delete_font = _bind(dll, "GdipDeleteFont", [handle])
        self.create_string_format = _bind(
            dll, "GdipCreateStringFormat", [integer, ctypes.c_uint16, ctypes.POINTER(handle)]
        )
        self.set_format_align = _bind(dll, "GdipSetStringFormatAlign", [handle, integer])
        self.set_format_line_align = _bind(dll, "GdipSetStringFormatLineAlign", [handle, integer])
        self.delete_string_format = _bind(dll, "GdipDeleteStringFormat", [handle])
        self.draw_string = _bind(
            dll,
            "GdipDrawString",
            [
                handle,
                ctypes.c_wchar_p,
                integer,
                handle,
                ctypes.POINTER(_RectF),
                handle,
                handle,
            ],
        )
        self.get_encoders_size = _bind(
            dll,
            "GdipGetImageEncodersSize",
            [ctypes.POINTER(uint), ctypes.POINTER(uint)],
        )
        self.get_encoders = _bind(
            dll,
            "GdipGetImageEncoders",
            [uint, uint, ctypes.POINTER(_ImageCodecInfo)],
        )
        self.save_image = _bind(
            dll,
            "GdipSaveImageToFile",
            [handle, ctypes.c_wchar_p, ctypes.POINTER(_Guid), ctypes.c_void_p],
        )


@lru_cache(maxsize=1)
def _gdiplus_api() -> _GdiPlusApi:
    return _GdiPlusApi()


_STATUS_NAMES: Final = {
    1: "GenericError",
    2: "InvalidParameter",
    3: "OutOfMemory",
    4: "ObjectBusy",
    5: "InsufficientBuffer",
    6: "NotImplemented",
    7: "Win32Error",
    8: "WrongState",
    9: "Aborted",
    10: "FileNotFound",
    11: "ValueOverflow",
    12: "AccessDenied",
    13: "UnknownImageFormat",
    14: "FontFamilyNotFound",
    15: "FontStyleNotFound",
    16: "NotTrueTypeFont",
    17: "UnsupportedGdiplusVersion",
    18: "GdiplusNotInitialized",
}


def _check_status(status: int, operation: str) -> None:
    if status:
        name = _STATUS_NAMES.get(status, f"status={status}")
        raise TimetableExportError(f"GDI+ {operation} 失败：{name}")


def _create_font_family(api: _GdiPlusApi) -> ctypes.c_void_p:
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "SimSun"):
        family = ctypes.c_void_p()
        if api.create_family(name, None, ctypes.byref(family)) == 0 and family.value:
            return family
        if family.value:
            api.delete_family(family)
    family = ctypes.c_void_p()
    status = api.get_generic_sans(ctypes.byref(family))
    if status:
        if family.value:
            api.delete_family(family)
        _check_status(status, "获取通用字体")
    return family


def _create_font(
    api: _GdiPlusApi, family: ctypes.c_void_p, size: float, *, bold: bool = False
) -> ctypes.c_void_p:
    font = ctypes.c_void_p()
    status = api.create_font(
        family,
        ctypes.c_float(size),
        1 if bold else 0,
        2,
        ctypes.byref(font),
    )
    if status:
        if font.value:
            api.delete_font(font)
        _check_status(status, "创建字体")
    return font


def _create_brush(api: _GdiPlusApi, color: int) -> ctypes.c_void_p:
    brush = ctypes.c_void_p()
    status = api.create_solid_fill(color, ctypes.byref(brush))
    if status:
        if brush.value:
            api.delete_brush(brush)
        _check_status(status, "创建画刷")
    return brush


def _draw_text(
    api: _GdiPlusApi,
    graphics: ctypes.c_void_p,
    text: str,
    font: ctypes.c_void_p,
    brush: ctypes.c_void_p,
    x: float,
    y: float,
    width: float,
    height: float,
    string_format: ctypes.c_void_p | None = None,
) -> None:
    rectangle = _RectF(x, y, max(1.0, width), max(1.0, height))
    _check_status(
        api.draw_string(
            graphics,
            text,
            -1,
            font,
            ctypes.byref(rectangle),
            string_format,
            brush,
        ),
        "绘制文字",
    )


def _png_encoder(api: _GdiPlusApi) -> _Guid:
    count = ctypes.c_uint32()
    size = ctypes.c_uint32()
    _check_status(api.get_encoders_size(ctypes.byref(count), ctypes.byref(size)), "查询图片编码器")
    if not count.value or size.value < ctypes.sizeof(_ImageCodecInfo):
        raise TimetableExportError("Windows GDI+ 没有可用的图片编码器")
    buffer = ctypes.create_string_buffer(size.value)
    codecs = ctypes.cast(buffer, ctypes.POINTER(_ImageCodecInfo))
    _check_status(api.get_encoders(count, size, codecs), "读取图片编码器")
    for index in range(count.value):
        if (codecs[index].MimeType or "").casefold() == "image/png":
            return _Guid.from_buffer_copy(codecs[index].Clsid)
    raise TimetableExportError("Windows GDI+ 未提供 PNG 编码器")


def _render_plan_to_png(
    plan: TimetablePlan, layout: TimetableLayout, output_path: Path
) -> None:
    api = _gdiplus_api()
    token = ctypes.c_size_t()
    started = False
    bitmap = ctypes.c_void_p()
    graphics = ctypes.c_void_p()
    family = ctypes.c_void_p()
    fonts: list[ctypes.c_void_p] = []
    brushes: list[ctypes.c_void_p] = []
    pen = ctypes.c_void_p()
    formats: list[ctypes.c_void_p] = []
    try:
        startup_input = _GdiplusStartupInput(1, None, 0, 0)
        _check_status(api.startup(ctypes.byref(token), ctypes.byref(startup_input), None), "启动")
        started = True
        _check_status(
            api.create_bitmap(
                layout.width,
                layout.height,
                0,
                0x0026200A,
                None,
                ctypes.byref(bitmap),
            ),
            "创建位图",
        )
        _check_status(api.get_graphics(bitmap, ctypes.byref(graphics)), "创建绘图上下文")
        _check_status(api.clear(graphics, 0xFFFFFFFF), "清空画布")
        _check_status(api.set_smoothing(graphics, 4), "设置抗锯齿")
        _check_status(api.set_text_rendering(graphics, 3), "设置文字抗锯齿")

        family = _create_font_family(api)
        title_font = _create_font(api, family, 28, bold=True)
        fonts.append(title_font)
        header_font = _create_font(api, family, 17, bold=True)
        fonts.append(header_font)
        body_bold_font = _create_font(api, family, 15, bold=True)
        fonts.append(body_bold_font)
        body_font = _create_font(api, family, 14)
        fonts.append(body_font)
        small_font = _create_font(api, family, 13)
        fonts.append(small_font)

        text_brush = _create_brush(api, 0xFF1F2937)
        brushes.append(text_brush)
        muted_brush = _create_brush(api, 0xFF52616F)
        brushes.append(muted_brush)
        accent_brush = _create_brush(api, 0xFF1D4ED8)
        brushes.append(accent_brush)
        header_background = _create_brush(api, 0xFFEFF6FF)
        brushes.append(header_background)
        slot_background = _create_brush(api, 0xFFF8FAFC)
        brushes.append(slot_background)
        multi_background = _create_brush(api, 0xFFFFF7ED)
        brushes.append(multi_background)
        pending_background = _create_brush(api, 0xFFFFFBEB)
        brushes.append(pending_background)

        _check_status(api.create_pen(0xFFCBD5E1, ctypes.c_float(1.0), 2, ctypes.byref(pen)), "创建边框")
        center_format = ctypes.c_void_p()
        format_status = api.create_string_format(0, 0, ctypes.byref(center_format))
        if center_format.value:
            formats.append(center_format)
        _check_status(format_status, "创建居中格式")
        _check_status(api.set_format_align(center_format, 1), "设置水平居中")
        _check_status(api.set_format_line_align(center_format, 1), "设置垂直居中")

        _draw_text(
            api,
            graphics,
            "课程表",
            title_font,
            accent_brush,
            layout.table_left,
            28,
            layout.table_width,
            42,
            center_format,
        )
        has_multiple = any(len(cell.entries) > 1 for cell in plan.cells)
        subtitle = plan.term
        if has_multiple:
            subtitle += "  ·  同一时间格多门课程，请结合具体周次核对"
        _draw_text(
            api,
            graphics,
            subtitle,
            small_font,
            muted_brush,
            layout.table_left,
            74,
            layout.table_width,
            28,
            center_format,
        )

        table_left = layout.table_left
        table_right = table_left + layout.table_width
        header_bottom = layout.table_top + layout.header_height
        _check_status(
            api.fill_rectangle(
                graphics,
                header_background,
                table_left,
                layout.table_top,
                layout.table_width,
                layout.header_height,
            ),
            "填充表头",
        )

        headers = ("节次", *(WEEKDAY_LABELS[weekday] for weekday in layout.weekdays))
        column_widths = (layout.slot_column_width,) + (
            layout.day_column_width,
        ) * len(layout.weekdays)
        x = table_left
        for header, width in zip(headers, column_widths, strict=True):
            _draw_text(
                api,
                graphics,
                header,
                header_font,
                text_brush,
                x,
                layout.table_top,
                width,
                layout.header_height,
                center_format,
            )
            x += width

        cell_map = {(cell.slot, cell.weekday): cell.entries for cell in plan.cells}
        if layout.rows:
            for row in layout.rows:
                _check_status(
                    api.fill_rectangle(
                        graphics,
                        slot_background,
                        table_left,
                        row.top,
                        layout.slot_column_width,
                        row.height,
                    ),
                    "填充节次栏",
                )
                _draw_text(
                    api,
                    graphics,
                    row.slot.label,
                    header_font,
                    text_brush,
                    table_left,
                    row.top,
                    layout.slot_column_width,
                    row.height,
                    center_format,
                )
                for weekday_index, weekday in enumerate(layout.weekdays):
                    entries = cell_map.get((row.slot, weekday), ())
                    cell_left = (
                        table_left
                        + layout.slot_column_width
                        + weekday_index * layout.day_column_width
                    )
                    if len(entries) > 1:
                        _check_status(
                            api.fill_rectangle(
                                graphics,
                                multi_background,
                                cell_left,
                                row.top,
                                layout.day_column_width,
                                row.height,
                            ),
                            "填充多课程单元格",
                        )
                    entry_top = row.top
                    for entry_index, entry in enumerate(entries):
                        lines = _entry_lines(entry, layout.day_column_width)
                        item_height = len(lines) * _ENTRY_LINE_HEIGHT + 2 * _ENTRY_PADDING
                        line_top = entry_top + _ENTRY_PADDING
                        for text, bold in lines:
                            _draw_text(
                                api,
                                graphics,
                                text,
                                body_bold_font if bold else small_font,
                                text_brush if bold else muted_brush,
                                cell_left + _ENTRY_PADDING,
                                line_top,
                                layout.day_column_width - 2 * _ENTRY_PADDING,
                                _ENTRY_LINE_HEIGHT,
                            )
                            line_top += _ENTRY_LINE_HEIGHT
                        entry_top += item_height
                        if entry_index < len(entries) - 1:
                            separator_y = entry_top + _ENTRY_GAP // 2
                            _check_status(
                                api.draw_line(
                                    graphics,
                                    pen,
                                    cell_left + 8,
                                    separator_y,
                                    cell_left + layout.day_column_width - 8,
                                    separator_y,
                                ),
                                "绘制课程分隔线",
                            )
                            entry_top += _ENTRY_GAP
        else:
            _draw_text(
                api,
                graphics,
                "暂无确定时间的课程",
                body_font,
                muted_brush,
                table_left,
                header_bottom,
                layout.table_width,
                layout.empty_table_height,
                center_format,
            )

        horizontal_lines = [layout.table_top, header_bottom]
        horizontal_lines.extend(row.top + row.height for row in layout.rows)
        if not layout.rows:
            horizontal_lines.append(layout.table_bottom)
        for y in horizontal_lines:
            _check_status(
                api.draw_line(graphics, pen, table_left, y, table_right, y),
                "绘制横向表格线",
            )
        x_positions = [table_left, table_left + layout.slot_column_width]
        x_positions.extend(
            table_left + layout.slot_column_width + index * layout.day_column_width
            for index in range(1, len(layout.weekdays) + 1)
        )
        for x in x_positions:
            _check_status(
                api.draw_line(graphics, pen, x, layout.table_top, x, layout.table_bottom),
                "绘制纵向表格线",
            )

        if layout.pending_title_top is not None:
            _draw_text(
                api,
                graphics,
                "时间待定 / 无法解析的课程安排",
                header_font,
                accent_brush,
                table_left,
                layout.pending_title_top,
                layout.table_width,
                _PENDING_TITLE_HEIGHT,
            )
            for pending_layout in layout.pending_rows:
                entry = plan.pending[pending_layout.index]
                _check_status(
                    api.fill_rectangle(
                        graphics,
                        pending_background,
                        table_left,
                        pending_layout.top,
                        layout.table_width,
                        pending_layout.height - 4,
                    ),
                    "填充待定课程区域",
                )
                line_top = pending_layout.top + 8
                for text, bold in _pending_lines(entry, layout.table_width):
                    _draw_text(
                        api,
                        graphics,
                        text,
                        body_bold_font if bold else body_font,
                        text_brush if bold else muted_brush,
                        table_left + 12,
                        line_top,
                        layout.table_width - 24,
                        _PENDING_LINE_HEIGHT,
                    )
                    line_top += _PENDING_LINE_HEIGHT

        encoder = _png_encoder(api)
        _check_status(api.save_image(bitmap, str(output_path), ctypes.byref(encoder), None), "保存 PNG")
    finally:
        for string_format in reversed(formats):
            api.delete_string_format(string_format)
        for brush in reversed(brushes):
            api.delete_brush(brush)
        if pen.value:
            api.delete_pen(pen)
        for font in reversed(fonts):
            api.delete_font(font)
        if family.value:
            api.delete_family(family)
        if graphics.value:
            api.delete_graphics(graphics)
        if bitmap.value:
            api.dispose_image(bitmap)
        if started:
            api.shutdown(token)


def _safe_term_filename(term: str) -> str:
    component = _INVALID_FILENAME_RE.sub("_", " ".join(term.split())).strip(" .")
    return (component[:80].rstrip(" .") or "学期待定")


def _next_available_path(candidate: Path) -> Path:
    if not candidate.exists():
        return candidate
    for index in range(2, 10_000):
        alternative = candidate.with_name(f"{candidate.stem}-{index}{candidate.suffix}")
        if not alternative.exists():
            return alternative
    raise TimetableExportError("同名课程表文件过多，请清理输出目录后重试")


def _resolve_output_path(
    result: EnrolledCourseResult,
    output_path: str | os.PathLike[str] | None,
    output_dir: str | os.PathLike[str] | None,
) -> Path:
    if output_path is not None and output_dir is not None:
        raise ValueError("output_path 和 output_dir 不能同时指定")
    if output_path is None:
        directory = Path(output_dir) if output_dir is not None else Path.cwd()
        candidate = directory / f"课程表-{_safe_term_filename(result.term)}.png"
    else:
        candidate = Path(output_path)
        if candidate.suffix.casefold() != ".png":
            candidate = candidate.with_name(f"{candidate.name}.png")
    absolute = candidate.expanduser().resolve()
    if not absolute.parent.is_dir():
        raise TimetableExportError(f"输出目录不存在：{absolute.parent}")
    return _next_available_path(absolute)


def _verify_png(path: Path, layout: TimetableLayout) -> None:
    try:
        # A writable descriptor is required by ``os.fsync`` on Windows even
        # though this function never changes the encoded bytes.
        with path.open("r+b") as stream:
            header = stream.read(24)
            if len(header) < 24 or not header.startswith(_PNG_SIGNATURE):
                raise TimetableExportError("GDI+ 输出的文件不是有效 PNG")
            if header[12:16] != b"IHDR":
                raise TimetableExportError("PNG 文件缺少 IHDR 尺寸信息")
            width, height = struct.unpack(">II", header[16:24])
            if (width, height) != (layout.width, layout.height):
                raise TimetableExportError(
                    f"PNG 尺寸异常：{width}x{height}，预期 {layout.width}x{layout.height}"
                )
            stream.seek(0, os.SEEK_END)
            if stream.tell() <= 32:
                raise TimetableExportError("PNG 文件内容为空")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise TimetableExportError(f"无法校验临时 PNG：{exc}") from exc


def export_timetable_png(
    result: EnrolledCourseResult,
    output_path: str | os.PathLike[str] | None = None,
    *,
    output_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Render *result* to an atomically published PNG and return its final path.

    With no explicit path, the file is written to the current directory as
    ``课程表-<学期>.png``. Existing files are never overwritten; ``-2``, ``-3``
    and so on are appended to the requested stem.
    """

    if not isinstance(result, EnrolledCourseResult):
        raise TypeError("result must be an EnrolledCourseResult")
    if not _IS_WINDOWS:
        raise TimetableExportError("PNG 课程表导出仅支持 Windows GDI+")

    plan = build_timetable_plan(result)
    layout = calculate_timetable_layout(plan)
    with _EXPORT_LOCK:
        target = _resolve_output_path(result, output_path, output_dir)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp.png",
            dir=target.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            _render_plan_to_png(plan, layout, temporary)
            _verify_png(temporary, layout)
            os.replace(temporary, target)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return target


__all__ = [
    "PendingTimetableEntry",
    "TimetableCell",
    "TimetableEntry",
    "TimetableExportError",
    "TimetableLayout",
    "TimetablePlan",
    "TimetableSlot",
    "WEEKDAY_LABELS",
    "build_timetable_plan",
    "calculate_timetable_layout",
    "export_timetable_png",
]
