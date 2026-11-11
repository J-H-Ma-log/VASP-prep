@echo off
REM ============================================================
REM  Write-permission self check (double-click to run).
REM  ASCII-only on purpose: cmd reads .bat in the ANSI code page,
REM  so Chinese text here would be garbled on some machines.
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"

set "PY=%~dp0..\chgnet_env\python\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" "%~dp0tools\check_write.py"
echo.
pause
