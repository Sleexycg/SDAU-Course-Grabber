"""PyInstaller build support available from installed and source CLIs."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .config import config_dir, settings_from_mapping


PACKAGE_DIR = Path(__file__).resolve().parent


def configured_values(values: Mapping[str, str]) -> dict[str, str]:
    """Validate a complete built-in configuration and expose its plaintext secret.

    A one-file executable must be able to recover the password without access to
    the build machine. Consequently, configured executables contain extractable
    plaintext credentials and must not be distributed.
    """

    settings = settings_from_mapping(values)
    if not settings.student_id or not settings.password:
        raise ValueError("配置版 EXE 需要学号和密码")
    return settings.to_env(encrypt_password=False)


def ensure_pyinstaller() -> None:
    if importlib.util.find_spec("PyInstaller") is None:
        raise RuntimeError(
            '未安装 PyInstaller。请先运行 .\\setup.ps1，'
            '或执行：python -m pip install -e ".[build]"'
        )


def _executable_name(builtin_config: Mapping[str, str] | None) -> str:
    if builtin_config is None:
        return "course-grabber"
    student_id = str(builtin_config.get("STUDENT_ID", "")).strip()
    if not student_id:
        raise ValueError("配置版 EXE 缺少学号")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", student_id):
        raise ValueError("学号包含不能用于 EXE 文件名的字符")
    return student_id


def build_onefile(builtin_config: Mapping[str, str] | None) -> Path:
    """Build one executable, keeping all transient plaintext outside the project."""

    ensure_pyinstaller()
    name = _executable_name(builtin_config)

    working_root = config_dir()
    dist_dir = working_root / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    course_file = working_root / "courses.json"
    icon_file = working_root / "favicon.ico"

    with tempfile.TemporaryDirectory(prefix="course-grabber-build-") as temporary:
        temp_dir = Path(temporary)
        entrypoint = temp_dir / "main_entry.py"
        entrypoint.write_text(
            "from main.cli import main\n"
            "raise SystemExit(main())\n",
            encoding="utf-8",
        )
        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--log-level",
            "ERROR",
            "--name",
            name,
            "--distpath",
            str(dist_dir),
            "--workpath",
            str(temp_dir / "work"),
            "--specpath",
            str(temp_dir / "spec"),
            "--paths",
            str(PACKAGE_DIR.parent),
        ]

        if icon_file.is_file():
            command.extend(("--icon", str(icon_file)))
        if builtin_config is not None:
            config_file = temp_dir / "builtin_config.json"
            config_file.write_text(
                json.dumps(dict(builtin_config), ensure_ascii=False),
                encoding="utf-8",
            )
            command.extend(("--add-data", f"{config_file}{os.pathsep}."))
        if course_file.is_file():
            command.extend(("--add-data", f"{course_file}{os.pathsep}."))

        command.append(str(entrypoint))
        completed = subprocess.run(
            command,
            cwd=working_root,
            check=False,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            output_text = completed.stderr or completed.stdout
            detail = next(
                (line.strip() for line in reversed(output_text.splitlines()) if line.strip()),
                "没有可用的错误信息",
            )
            raise RuntimeError(f"PyInstaller 构建失败（退出码 {completed.returncode}）：{detail}")

    suffix = ".exe" if os.name == "nt" else ""
    output = dist_dir / f"{name}{suffix}"
    if not output.is_file():
        raise RuntimeError(f"构建结束但未找到输出文件：{output}")
    return output


__all__ = ["build_onefile", "configured_values", "ensure_pyinstaller"]
