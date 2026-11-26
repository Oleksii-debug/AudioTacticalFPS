@echo off
setlocal
set "ORT_DISABLE_TELEMETRY=1"
set "HF_HUB_DISABLE_TELEMETRY=1"
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
set "DO_NOT_TRACK=1"
if exist "%~dp0ocr\ocr-manifest.json" set "CARR_OCR_DIR=%~dp0ocr"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run Install.cmd in this folder first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -I -B -X utf8 -m carr_library.native_checks --bundle-root "%~dp0."
set "CHECK_RESULT=%ERRORLEVEL%"
if not "%CHECK_RESULT%"=="0" goto finished
"%~dp0.venv\Scripts\python.exe" -I -B -X utf8 -m carr_library --self-test
set "CHECK_RESULT=%ERRORLEVEL%"
if not "%CHECK_RESULT%"=="0" goto finished
if not exist "%~dp0ocr\ocr-manifest.json" goto finished
"%~dp0.venv\Scripts\python.exe" -I -B -X utf8 -m carr_library.extract.ocr_runtime --check
set "CHECK_RESULT=%ERRORLEVEL%"
:finished
pause
exit /b %CHECK_RESULT%
