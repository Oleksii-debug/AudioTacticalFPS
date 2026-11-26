@echo off
setlocal
set "CARR_PROGRAM_DIR=%~dp0"
if exist "%~dp0ocr\ocr-manifest.json" set "CARR_OCR_DIR=%~dp0ocr"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run Install.cmd in this folder first.
  pause
  exit /b 1
)
if exist "%~dp0models\minilm-manifest.json" (
  "%~dp0.venv\Scripts\python.exe" -I -B -m carr_library --model-dir "%~dp0models\minilm" --model-manifest "%~dp0models\minilm-manifest.json" %*
) else (
  "%~dp0.venv\Scripts\python.exe" -I -B -m carr_library %*
)
if errorlevel 1 (
  echo The application could not start. The error is shown above.
  pause
  exit /b 1
)
