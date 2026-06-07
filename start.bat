@echo off
cd /d "%~dp0"
npx tsx src/index.ts
if %ERRORLEVEL% NEQ 0 (
    echo.
    pause
)
