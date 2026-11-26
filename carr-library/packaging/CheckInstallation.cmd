@echo off
setlocal
set "ORT_DISABLE_TELEMETRY=1"
set "HF_HUB_DISABLE_TELEMETRY=1"
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
set "DO_NOT_TRACK=1"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run Install.cmd in this folder first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -I -B -m carr_library --self-test
set "CHECK_RESULT=%ERRORLEVEL%"
pause
exit /b %CHECK_RESULT%
