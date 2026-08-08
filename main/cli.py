"""Command-line entry points and interactive menu."""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from dataclasses import replace

from .course_selector import CourseSelector, format_enrolled_result
from .config import (
    ConfigError,
    Settings,
    is_frozen,
    load_course_list,
    load_settings,
    save_settings,
    settings_from_mapping,
)
from .crypto import SecretProtectionError
from .grabber import GrabEngine
from .http import JwHttpClient
from .models import EnrolledCourseResult, GrabTaskState, GrabTaskStatus
from .session import Session
from .term import infer_current_term, is_valid_term
from .ui import (
    choose_courses,
    clear_screen,
    comma_items,
    configure_console,
    confirm,
    error,
    info,
    print_box,
    prompt,
    prompt_int,
    prompt_secret,
    select_term,
    success,
    wait_key,
    warning,
)


def _make_services(settings: Settings) -> tuple[Session, CourseSelector]:
    """Construct the protocol stack in one place for every CLI mode."""

    client = JwHttpClient(
        timeout=settings.jw_timeout_seconds,
        retry_count=settings.jw_retry_count,
        user_agent=settings.jw_user_agent,
    )
    session = Session(client, settings.student_id, settings.password)
    return session, CourseSelector(session)


def _require_credentials(settings: Settings) -> bool:
    if settings.student_id and settings.password:
        return True
    error("请先运行 config 设置学号和密码。")
    return False


def _login(session: Session) -> bool:
    info("正在登录教务系统...")
    try:
        session.login()
    except Exception as exc:
        error(f"登录失败：{exc}")
        return False
    success("登录成功", check=True)
    return True


def run_query(
    settings: Settings,
    *,
    term: str | None = None,
    interactive: bool = False,
) -> int:
    """Run an enrolled-course query for both menu and direct commands."""

    if not _require_credentials(settings):
        return 1
    default_term = infer_current_term()
    selected_term = term or default_term
    if interactive and term is None:
        selected_term = select_term(default_term, title="查询选课结果")
    if not is_valid_term(selected_term):
        error(f"学期格式无效：{selected_term!r}，应为 YYYY-YYYY-1/2。")
        return 2

    session, selector = _make_services(settings)
    if not _login(session):
        return 1
    print("")
    info(f"正在查询 {selected_term} 的选课结果...")
    try:
        result = selector.query_enrolled_courses(selected_term)
    except Exception as exc:
        error(f"查询已选课程失败：{exc}")
        return 1

    success(
        f"查询成功：共 {result.summary.total_courses} 门课程，"
        f"{result.summary.total_credits:.1f} 学分"
    )
    print("\n" + format_enrolled_result(result) + "\n")
    return 0


def _targets_with_names(
    settings: Settings,
    course_ids: Sequence[str] | None,
    course_names: Sequence[str] | None,
    *,
    interactive: bool,
) -> tuple[list[str], list[str]]:
    explicit_ids = course_ids is not None
    ids_source = course_ids if explicit_ids else settings.target_course_ids
    ids = [item.strip() for item in ids_source if item.strip()]
    if course_names is not None:
        names_source: Sequence[str] = course_names
    elif explicit_ids:
        # Configured names belong to configured IDs and must never leak across
        # an argparse --course-id override.
        names_source = ()
    else:
        names_source = settings.target_course_names
    names = [item.strip() for item in names_source]

    course_list = []
    try:
        course_list = load_course_list()
    except ConfigError as exc:
        warning(str(exc))

    if not ids and interactive and course_list:
        clear_screen()
        print_box("选择课程")
        print("")
        selected = choose_courses(course_list)
        ids = [course.id for course in selected]
        names = [course.name for course in selected]
    if not ids and interactive:
        ids = comma_items(prompt("请输入课程 ID（多个用逗号或空格分隔）"))

    by_id = {course.id: course.name for course in course_list}
    if len(names) < len(ids):
        names.extend("" for _ in range(len(ids) - len(names)))
    names = names[: len(ids)]
    for index, course_id in enumerate(ids):
        if not names[index]:
            names[index] = by_id.get(course_id, "")

    # Never launch duplicate requests for one teaching-class ID. In particular,
    # this protects users migrating the known malformed legacy courses.json.
    normalized_ids: list[str] = []
    normalized_names: list[str] = []
    seen: dict[str, str] = {}
    for course_id, name in zip(ids, names, strict=True):
        existing = seen.get(course_id)
        if existing is not None:
            if existing != name:
                warning(
                    f"课程 ID {course_id} 同时配置为“{existing}”和“{name}”，"
                    "已保留第一项并跳过冲突项。"
                )
            else:
                warning(f"重复课程 ID {course_id} 已跳过。")
            continue
        seen[course_id] = name
        normalized_ids.append(course_id)
        normalized_names.append(name)
    return normalized_ids, normalized_names


def _course_names(result: EnrolledCourseResult) -> set[str]:
    return {course.name.strip() for course in result.courses if course.name.strip()}


def _display_grab_results(
    states: Sequence[GrabTaskState], already_selected: Sequence[str]
) -> int:
    satisfied = [
        state
        for state in states
        if state.status in {GrabTaskStatus.SUCCESS, GrabTaskStatus.ALREADY_ENROLLED}
    ]
    print("")
    if satisfied or already_selected:
        success("抢课流程结束，已选中的目标课程：")
        for name in already_selected:
            print(f"  ✓ {name}")
        for state in satisfied:
            suffix = "（系统已选）" if state.status == GrabTaskStatus.ALREADY_ENROLLED else ""
            print(f"  ✓ {state.course_name or state.course_id}{suffix}")
    else:
        warning("没有目标课程抢课成功。")

    for state in states:
        course_name = state.course_name or state.course_id
        if state.status in {
            GrabTaskStatus.ERROR,
            GrabTaskStatus.CONFLICT,
            GrabTaskStatus.CLOSED,
        }:
            warning(f"{course_name}：{state.last_message or state.status.value}")
    return len(satisfied)


def run_grab(
    settings: Settings,
    *,
    term: str | None = None,
    course_ids: Sequence[str] | None = None,
    course_names: Sequence[str] | None = None,
    target_count: int | None = None,
    poll_interval_ms: int | None = None,
    assume_yes: bool = False,
    interactive: bool = False,
) -> int:
    """Run the complete grab workflow shared by menu and direct invocation."""

    if not _require_credentials(settings):
        return 1

    default_term = infer_current_term()
    selected_term = term or default_term
    if interactive and term is None:
        selected_term = select_term(default_term, title="抢课模式")
    if not is_valid_term(selected_term):
        error(f"学期格式无效：{selected_term!r}，应为 YYYY-YYYY-1/2。")
        return 2
    if interactive and selected_term != default_term:
        warning(f"选择的 {selected_term} 不是当前日期默认学期 {default_term}。")
        if not confirm("确认仍使用这个学期？", default=True):
            info("已取消。")
            return 0

    ids, names = _targets_with_names(
        settings, course_ids, course_names, interactive=interactive
    )
    if not ids:
        error("未设置课程 ID；请运行 config 或使用 --course-id。")
        return 2

    maximum_target = min(7, len(ids))
    desired = target_count
    if desired is None:
        desired = (
            prompt_int(
                "请输入抢课数量",
                default=1,
                minimum=1,
                maximum=maximum_target,
            )
            if interactive
            else 1
        )
    if not 1 <= desired <= maximum_target:
        error(f"抢课数量必须在 1-{maximum_target} 之间。")
        return 2

    poll_ms = poll_interval_ms if poll_interval_ms is not None else settings.poll_interval_ms
    if poll_ms < 300:
        error("轮询间隔不能低于 300 毫秒。")
        return 2

    print(f"目标学期：{selected_term}")
    print(f"目标课程：{', '.join(name or course_id for course_id, name in zip(ids, names, strict=True))}")
    print(f"目标数量：{desired}")
    print(f"轮询间隔：{poll_ms}ms")
    if not assume_yes and not confirm("确认启动抢课？", default=True):
        info("已取消。")
        return 0

    session, selector = _make_services(settings)
    if not _login(session):
        return 1

    enrolled_names: set[str] = set()
    info("正在查询已选课程...")
    try:
        enrolled = selector.query_enrolled_courses(selected_term)
        enrolled_names = _course_names(enrolled)
    except Exception as exc:
        error(f"查询已选课程失败：{exc}")
        error("为避免重复选课，已停止本次抢课；不会盲目提交全部目标。")
        return 1

    already_selected: list[str] = []
    counted_selected: set[str] = set()
    remaining_ids: list[str] = []
    remaining_names: list[str] = []
    for course_id, name in zip(ids, names, strict=True):
        if name and name in enrolled_names:
            if name not in counted_selected:
                already_selected.append(name)
                counted_selected.add(name)
        else:
            remaining_ids.append(course_id)
            remaining_names.append(name or course_id)

    if len(already_selected) >= desired:
        success("目标数量已经满足，无需启动抢课任务。")
        for name in already_selected:
            print(f"  ✓ {name}")
        return 0

    remaining_target = desired - len(already_selected)
    if not remaining_ids:
        warning("没有可继续处理的目标课程。")
        return 1
    remaining_target = min(remaining_target, len(remaining_ids))

    info(f"正在准备 {selected_term} 选课期次...")
    try:
        selector.prepare_selection(selected_term)
    except Exception as exc:
        error(f"准备选课期次失败，未启动抢课：{exc}")
        return 1
    success("选课期次准备完成")

    engine = GrabEngine(selector, session)
    try:
        engine.start(
            remaining_ids,
            remaining_names,
            target_count=remaining_target,
            interval_ms=poll_ms,
        )
        info("抢课任务已启动；按 Ctrl+C 可安全停止。")
        try:
            states = engine.wait()
        except KeyboardInterrupt:
            warning("收到停止信号，正在结束所有任务...")
            engine.stop_all()
            states = engine.wait()
    finally:
        engine.stop_all()

    successful_count = _display_grab_results(states, already_selected)
    return 0 if successful_count + len(already_selected) >= desired else 1


def run_config(settings: Settings) -> int:
    """Interactively edit and atomically persist local settings."""

    clear_screen()
    print_box("设置环境变量")
    print("\n留空保持当前值。\n")

    student_id = prompt("学号", default=settings.student_id)
    new_password = prompt_secret("密码", has_current=bool(settings.password))
    password = new_password or settings.password

    poll_ms = prompt_int(
        "轮询间隔(ms)", default=settings.poll_interval_ms, minimum=300, maximum=3_600_000
    )

    course_ids = list(settings.target_course_ids)
    course_names = list(settings.target_course_names)
    try:
        course_list = load_course_list()
    except ConfigError as exc:
        warning(str(exc))
        course_list = []
    if course_list:
        print("\n可选课程（留空保持当前课程）：")
        picked = choose_courses(course_list)
        if picked:
            course_ids = [course.id for course in picked]
            course_names = [course.name for course in picked]
    else:
        raw_ids = prompt(
            "目标课程 ID（多个用逗号分隔，留空保持）",
            default=",".join(course_ids),
        )
        if raw_ids:
            course_ids = comma_items(raw_ids)
            raw_names = prompt(
                "对应课程名称（多个用逗号分隔，可留空）",
                default=",".join(course_names),
            )
            course_names = comma_items(raw_names) if raw_names else []

    candidate = replace(
        settings,
        student_id=student_id,
        password=password,
        target_course_ids=tuple(course_ids),
        target_course_names=tuple(course_names),
        poll_interval_ms=poll_ms,
    )
    try:
        # Round-trip through the central validator so menu editing cannot create
        # a file that the next process fails to load.
        candidate = settings_from_mapping(candidate.to_env(encrypt_password=False))
        saved_path = save_settings(candidate)
    except (ConfigError, SecretProtectionError) as exc:
        error(f"配置未保存：{exc}")
        return 1
    success(f"配置已保存到 {saved_path}（密码使用 Windows DPAPI 加密）")
    return 0


def run_info(settings: Settings) -> int:
    """Show account and target information without exposing the password."""

    if not _require_credentials(settings):
        return 1
    clear_screen()
    print_box("个人信息")
    print("")
    session, _selector = _make_services(settings)
    college = ""
    class_name = ""
    if not _login(session):
        return 1
    try:
        response = session.request("/framework/xsMainV_new.htmlx?t1=1")
        compact = re.sub(r"\s+|&nbsp;|&#160;", "", response.text)
        college_match = re.search(r"学院：([^<]+?)<", compact)
        class_match = re.search(r"班级：([^<]+?)<", compact)
        college = college_match.group(1).strip() if college_match else ""
        class_name = class_match.group(1).strip() if class_match else ""
    except Exception as exc:
        warning(f"个人信息抓取失败：{exc}")

    print("")
    value_gap = " " * 7
    print(f"  学号：{value_gap}{settings.student_id}")
    if college:
        print(f"  学院：{value_gap}{college}")
    if class_name:
        print(f"  班级：{value_gap}{class_name}")
    if settings.target_course_ids:
        print("  目标课程：")
        names = list(settings.target_course_names)
        names.extend("" for _ in range(len(settings.target_course_ids) - len(names)))
        for index, (course_id, name) in enumerate(
            zip(settings.target_course_ids, names, strict=True), start=1
        ):
            print(f"    {index}. {name + '（' if name else ''}{course_id}{'）' if name else ''}")
    else:
        print("  目标课程：（未设置）")
    return 0


def run_build(
    settings: Settings,
    *,
    include_config: bool | None = None,
) -> int:
    """Build a one-file executable through the packaged build helper."""

    if is_frozen():
        error("已在打包后的 EXE 中运行，不能再次构建。")
        return 1
    if include_config is None:
        include_config = confirm("是否把当前配置内置到 EXE？", default=False)

    try:
        from .builder import build_onefile, configured_values

        builtin = (
            configured_values(settings.to_env(encrypt_password=False))
            if include_config
            else None
        )
        info("正在构建 EXE，这可能需要几分钟...")
        output = build_onefile(builtin)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        error(f"构建失败：{exc}")
        return 1
    success(f"构建完成：{output}")
    if include_config:
        warning("配置版 EXE 包含可提取的明文账号密码，请勿公开分发。")
    return 0


def show_menu(initial_settings: Settings | None = None) -> int:
    settings = initial_settings or load_settings()
    while True:
        if not settings.configured:
            warning("尚未配置账号信息，请先完成配置。")
            wait_key()
            run_config(settings)
            try:
                settings = load_settings()
            except (ConfigError, SecretProtectionError) as exc:
                error(str(exc))
            continue

        clear_screen()
        print_box("SDAU-Course-Grabber")
        print("\n  1. 查询选课结果")
        print("  2. 启动抢课")
        print("  3. 设置环境变量")
        print("  4. 查看信息")
        if not is_frozen():
            print("  5. 导出 EXE")
        print("  0. 退出\n")
        choice = prompt("请选择")

        if choice == "0":
            print("再见！")
            return 0
        if choice == "1":
            run_query(settings, interactive=True)
        elif choice == "2":
            run_grab(settings, interactive=True)
        elif choice == "3":
            run_config(settings)
        elif choice == "4":
            run_info(settings)
        elif choice == "5" and not is_frozen():
            run_build(settings)
        else:
            warning("无效选择。")
        wait_key()
        try:
            settings = load_settings()
        except (ConfigError, SecretProtectionError) as exc:
            error(f"重新加载配置失败：{exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="course-grabber",
        description="山东农业大学教务系统选课查询与抢课工具",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("menu", help="打开交互菜单")

    query_parser = subparsers.add_parser("query", help="查询已选课程")
    query_parser.add_argument(
        "term", nargs="?", help="学期，例如 2026-2027-1；默认按当前日期自动判断"
    )
    grab_parser = subparsers.add_parser("grab", help="启动抢课")
    grab_parser.add_argument("term", nargs="?", help="目标学期；默认按当前日期自动判断")
    grab_parser.add_argument(
        "-c", "--course-id", action="append", default=[], help="课程 ID；可重复或用逗号分隔"
    )
    grab_parser.add_argument(
        "--course-name", action="append", default=[], help="课程名称；顺序与课程 ID 对应"
    )
    grab_parser.add_argument("--count", type=int, help="成功目标数量，默认 1")
    grab_parser.add_argument("--poll-ms", type=int, help="轮询间隔（毫秒，最低 300）")
    grab_parser.add_argument("-y", "--yes", action="store_true", help="跳过启动确认")

    subparsers.add_parser("config", help="交互修改配置")
    subparsers.add_parser("info", help="查看账号与目标课程信息")
    build_command = subparsers.add_parser("build", help="使用 PyInstaller 导出 EXE")
    build_mode = build_command.add_mutually_exclusive_group()
    build_mode.add_argument(
        "--with-config",
        action="store_true",
        help="内置当前配置（EXE 会包含可提取的明文账号密码）",
    )
    build_mode.add_argument("--blank", action="store_true", help="构建空白配置版本")
    return parser


def _flatten_cli_values(values: Sequence[str]) -> list[str]:
    flattened: list[str] = []
    for value in values:
        flattened.extend(comma_items(value))
    return flattened


def main(argv: Sequence[str] | None = None) -> int:
    configure_console()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    command = args.command or "menu"

    try:
        settings = load_settings()
    except (ConfigError, SecretProtectionError) as exc:
        if command == "config":
            warning(str(exc))
            try:
                settings = load_settings(decrypt_password=False)
            except (ConfigError, SecretProtectionError) as fallback_exc:
                error(f"无法加载配置：{fallback_exc}")
                return 2
        else:
            error(f"配置加载失败：{exc}")
            error("请运行 course-grabber config 修复配置。")
            return 2

    try:
        if command == "menu":
            return show_menu(settings)
        if command == "query":
            return run_query(settings, term=args.term)
        if command == "grab":
            return run_grab(
                settings,
                term=args.term,
                course_ids=_flatten_cli_values(args.course_id) or None,
                course_names=_flatten_cli_values(args.course_name) or None,
                target_count=args.count,
                poll_interval_ms=args.poll_ms,
                assume_yes=args.yes,
            )
        if command == "config":
            return run_config(settings)
        if command == "info":
            return run_info(settings)
        if command == "build":
            include_config = True if args.with_config else False if args.blank else None
            return run_build(settings, include_config=include_config)
    except KeyboardInterrupt:
        warning("操作已取消。")
        return 130
    except Exception as exc:
        error(f"未处理的错误：{exc}")
        return 1
    parser.error(f"未知命令：{command}")
    return 2


__all__ = [
    "build_parser",
    "main",
    "run_build",
    "run_config",
    "run_grab",
    "run_info",
    "run_query",
    "show_menu",
]
