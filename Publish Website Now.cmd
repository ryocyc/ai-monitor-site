@echo off
setlocal
title Publish AI Monitor Website Now

set "ROOT=%~dp0"
set "MONITOR_DIR=%ROOT%ai_monitor"
set "PYTHON_EXE=python"

if not exist "%MONITOR_DIR%\publish_site.py" (
  echo [ERROR] Cannot find publish_site.py in:
  echo %MONITOR_DIR%
  echo.
  pause
  exit /b 1
)

cd /d "%ROOT%"

echo =========================================
echo   Publishing AI Monitor website now...
echo =========================================
echo.

%PYTHON_EXE% "%MONITOR_DIR%\publish_site.py" --limit 10
if errorlevel 1 goto :fail

%PYTHON_EXE% "%MONITOR_DIR%\publish_to_github.py"
if errorlevel 1 goto :fail

echo.
echo =========================================
echo   Website publish finished.
echo =========================================
echo.
pause
endlocal
exit /b 0

:fail
echo.
echo =========================================
echo   Website publish failed.
echo =========================================
echo.
pause
endlocal
exit /b 1
