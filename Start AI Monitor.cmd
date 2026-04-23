@echo off
setlocal
title AI Monitor Runner

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

echo ==========================================
echo   AI Monitor local runner is starting...
echo ==========================================
echo.
echo Monitor interval : 5 minutes
echo Publish interval : 15 minutes
echo Duration         : 10 hours
echo Log file         : %MONITOR_DIR%\logs\overnight_runner.log
echo.
echo Press Ctrl+C in this window to stop it.
echo.

%PYTHON_EXE% "%MONITOR_DIR%\run_local_scheduler.py" --monitor-seconds 300 --publish-seconds 900 --duration-hours 10

echo.
echo ==========================================
echo   AI Monitor runner has stopped.
echo ==========================================
echo.
pause
endlocal
