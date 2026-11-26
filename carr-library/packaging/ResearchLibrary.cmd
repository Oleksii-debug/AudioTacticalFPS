@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 (
  echo The application folder could not be opened. Extract the package to a local folder.
  pause
  exit /b 1
)
set "ORT_DISABLE_TELEMETRY=1"
set "HF_HUB_DISABLE_TELEMETRY=1"
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
set "DO_NOT_TRACK=1"
if exist "%~dp0ocr\bin\tesseract.exe" (
  set "PATH=%~dp0ocr\bin;%PATH%"
  set "TESSDATA_PREFIX=%~dp0ocr\share\tessdata"
)
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
