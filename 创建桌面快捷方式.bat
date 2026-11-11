@echo off
REM ============================================================
REM  Create a Desktop shortcut "VASP-Prep" that starts the web UI.
REM  Double-click this file once; then use the desktop icon.
REM  ASCII-only on purpose: cmd reads .bat in the ANSI code page.
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"

echo Creating Desktop shortcut ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\make_shortcut.ps1"

echo.
echo Done. In future, double-click the "VASP-Prep" icon on your Desktop.
echo If the shortcut already existed, it has been updated.
echo.
pause
