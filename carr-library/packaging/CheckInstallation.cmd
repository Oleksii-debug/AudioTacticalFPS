@echo off
setlocal
if exist "%~dp0ocr\ocr-manifest.json" set "CARR_OCR_DIR=%~dp0ocr"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run Install.cmd in this folder first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -I -B -m carr_library --self-test
set "CHECK_RESULT=%ERRORLEVEL%"
if not "%CHECK_RESULT%"=="0" goto finished
if not exist "%~dp0ocr\ocr-manifest.json" goto finished
"%~dp0.venv\Scripts\python.exe" -I -B -m carr_library.extract.ocr_runtime --check
set "CHECK_RESULT=%ERRORLEVEL%"
:finished
pause
exit /b %CHECK_RESULT%
