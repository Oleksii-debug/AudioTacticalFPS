@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run Install.cmd in this folder first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -I -B -m carr_library --self-test
set "CHECK_RESULT=%ERRORLEVEL%"
pause
exit /b %CHECK_RESULT%
