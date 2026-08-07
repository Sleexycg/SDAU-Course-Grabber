@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>nul
    if not errorlevel 1 (
        ".venv\Scripts\python.exe" -m main %*
        exit /b !ERRORLEVEL!
    )
)

py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>nul
if not errorlevel 1 (
    py -3 -m main %*
    exit /b !ERRORLEVEL!
)

python -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>nul
if not errorlevel 1 (
    python -m main %*
    exit /b !ERRORLEVEL!
)

echo.
echo Python 3.11 or newer was not found.
echo Install Python, then run setup.ps1 in PowerShell.
pause
exit /b 1
