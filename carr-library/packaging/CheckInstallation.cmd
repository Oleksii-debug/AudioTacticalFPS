@echo off
setlocal
set "ORT_DISABLE_TELEMETRY=1"
set "HF_HUB_DISABLE_TELEMETRY=1"
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
set "DO_NOT_TRACK=1"
if exist "%~dp0audio\bin\ffmpeg.exe" set "PATH=%~dp0audio\bin;%PATH%"
if exist "%~dp0citation-runtime\node.exe" set "PATH=%~dp0citation-runtime;%PATH%"
if exist "%~dp0ocr\ocr-manifest.json" set "CARR_OCR_DIR=%~dp0ocr"
if not exist "%~dp0.venv\Scripts\python.exe" goto incomplete
if not exist "%~dp0.venv\.installation-complete" goto incomplete
"%~dp0.venv\Scripts\python.exe" -I -B -X utf8 -m carr_library.native_checks --bundle-root "%~dp0."
set "CHECK_RESULT=%ERRORLEVEL%"
if not "%CHECK_RESULT%"=="0" goto finished
"%~dp0.venv\Scripts\python.exe" -I -B -X utf8 -m carr_library --self-test
set "CHECK_RESULT=%ERRORLEVEL%"
goto finished
:incomplete
echo Installation is incomplete. Extract a fresh package into a new folder and run Install.cmd.
echo Persistent diagnostic text is saved as "%~dp0installation-*.log".
if exist "%~dp0runtime\python.exe" (
  "%~dp0runtime\python.exe" -I -B -X utf8 "%~dp0install_windows.py" --show-install-log
) else (
  py -3.11 -I -B -X utf8 "%~dp0install_windows.py" --show-install-log
)
set "CHECK_RESULT=1"
:finished
pause
exit /b %CHECK_RESULT%
