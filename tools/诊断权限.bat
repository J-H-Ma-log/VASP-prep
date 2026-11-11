@echo off
REM ============================================================
REM  Diagnose write permissions (double-click, no command line).
REM  Writes a report next to this file: 权限诊断.txt
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0.."

set "PY=%~dp0..\..\chgnet_env\python\python.exe"
if not exist "%PY%" set "PY=%~dp0..\chgnet_env\python\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" "%~dp0diag_write.py"
echo.
pause
