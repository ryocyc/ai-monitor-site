@echo off
setlocal
cd /d "%~dp0"
python run_local_scheduler.py --monitor-seconds 300 --publish-seconds 900 --duration-hours 10 %*
endlocal
