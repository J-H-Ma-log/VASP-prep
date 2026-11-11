@echo off
REM ============================================================
REM  Start the local VASP-prep web UI (Chinese-named launcher).
REM  The real logic lives in start_webui.bat (ASCII name, used by
REM  the desktop shortcut). This file only forwards to it.
REM ============================================================
call "%~dp0start_webui.bat"
