@echo off
REM ============================================================
REM  VASP prep - terminal version (double-click to run)
REM  ASCII-only on purpose (see 启动网页.bat header).
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"

set "PY=%~dp0..\chgnet_env\python\python.exe"
if not exist "%PY%" set "PY=python"

echo [1/2] Python: %PY%
"%PY%" -c "import sys; print('      version', sys.version.split()[0])" || goto :err

echo [2/2] Starting terminal mode ...
"%PY%" "%~dp0chat_launcher.py"
if errorlevel 1 goto :err
goto :eof

:err
echo.
echo [ERROR] Failed to start. Please send the message above to me.
pause
