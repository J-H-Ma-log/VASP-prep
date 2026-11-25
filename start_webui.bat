@echo off
REM ============================================================
REM  Start the local VASP-prep web UI.
REM  ASCII-only on purpose: cmd reads .bat in the ANSI code page,
REM  so Chinese text here would be garbled on some machines.
REM  All Chinese messages are printed by Python.
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"

set "PY=%~dp0..\chgnet_env\python\python.exe"
if not exist "%PY%" set "PY=python"

echo [1/2] Python: %PY%
"%PY%" -c "import sys; print('      version', sys.version.split()[0])" || goto :err

echo [2/2] Starting local server (http://127.0.0.1:8765) ...
"%PY%" "%~dp0webui_launcher.py"
if errorlevel 1 goto :err
goto :eof

:err
echo.
echo [ERROR] Failed to start. Please send the messages above to me.
echo   - "No module named vasp_core": make sure this file is inside vasp-prep
echo   - port busy: edit [ui] port in vasp-prep.toml
echo.
pause
