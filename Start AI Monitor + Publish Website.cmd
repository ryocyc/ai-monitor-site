@echo off
setlocal
title AI Monitor Runner + Website Publish

set "ROOT=%~dp0"
set "MONITOR_DIR=%ROOT%ai_monitor"
set "PYTHON_EXE=python"

if not exist "%MONITOR_DIR%\run_local_scheduler.py" (
  echo [ERROR] Cannot find run_local_scheduler.py in:
  echo %MONITOR_DIR%
  echo.
  pause
  exit /b 1
)

cd /d "%MONITOR_DIR%"

echo =======================================================
echo   AI Monitor test runner is starting with site publish
echo =======================================================
echo.
echo Mode             : local monitor + auto GitHub publish
echo Monitor interval : 5 minutes
echo Publish interval : 15 minutes
echo Duration         : 10 hours
echo Log file         : %MONITOR_DIR%\logs\overnight_runner.log
echo.
echo After each publish cycle, the site will be pushed to GitHub.
echo Press Ctrl+C in this window to stop it.
echo.

%PYTHON_EXE% "%MONITOR_DIR%\run_local_scheduler.py" --monitor-seconds 300 --publish-seconds 900 --duration-hours 10 --publish-github

echo.
echo =======================================================
echo   AI Monitor runner has stopped.
echo =======================================================
echo.
pause
endlocal
