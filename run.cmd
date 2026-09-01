@echo off
REM Run anything with this project's Python, without activating the virtualenv.
REM
REM   run.cmd scripts\check_remote.py
REM   run.cmd -m pytest -q
REM   run.cmd -m app.main
REM
REM PowerShell blocks .ps1 scripts by default, which is what stops
REM ".venv\Scripts\activate" from working. A .cmd file is not affected, so this
REM sidesteps the whole question rather than asking you to change a system
REM security setting.

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo No virtual environment at "%~dp0.venv".
  echo Create it with:
  echo     python -m venv .venv
  echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
  exit /b 1
)

"%~dp0.venv\Scripts\python.exe" %*
