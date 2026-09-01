@echo off
REM Launcher for the syslab-server scheduled task.
REM Kept as a batch file so Task Scheduler has one simple thing to run, and so
REM you can double-click it to test exactly what the task will do.

setlocal
set "ROOT=%~dp0..\.."
cd /d "%ROOT%"

if not exist ".venv\Scripts\python.exe" (
  echo No virtual environment found at "%ROOT%\.venv".
  echo Create it with:  python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  exit /b 1
)

if not exist "logs" mkdir "logs"

echo [%date% %time%] starting syslab-server >> "logs\server.log"
".venv\Scripts\python.exe" -m app.main >> "logs\server.log" 2>&1
set "CODE=%ERRORLEVEL%"
echo [%date% %time%] syslab-server exited with %CODE% >> "logs\server.log"
exit /b %CODE%
