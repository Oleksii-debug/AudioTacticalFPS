@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0runtime\python.exe" (
  "%~dp0runtime\python.exe" -I -B "%~dp0install_windows.py"
) else (
  py -3.11 -I -B "%~dp0install_windows.py"
)
if errorlevel 1 (
  echo Installation failed. The message above explains what needs attention.
  pause
  exit /b 1
)
echo Press any key to close this installer.
pause >nul
