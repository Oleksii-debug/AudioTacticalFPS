@echo off
setlocal
set "ORT_DISABLE_TELEMETRY=1"
set "HF_HUB_DISABLE_TELEMETRY=1"
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
set "DO_NOT_TRACK=1"
if exist "%~dp0audio\bin\ffmpeg.exe" set "PATH=%~dp0audio\bin;%PATH%"
if exist "%~dp0citation-runtime\node.exe" set "PATH=%~dp0citation-runtime;%PATH%"
cd /d "%~dp0"
if exist "%~dp0runtime\python.exe" (
  "%~dp0runtime\python.exe" -I -B -X utf8 "%~dp0install_windows.py"
) else (
  py -3.11 -I -B -X utf8 "%~dp0install_windows.py"
)
if errorlevel 1 (
  echo Installation failed. The message above explains what needs attention.
  pause
  exit /b 1
)
echo Press any key to close this installer.
pause >nul
