@echo off
setlocal
chcp 65001 >nul
if not exist "%~dp0.venv\Scripts\python.exe" goto not_installed
if not exist "%~dp0.venv\.installation-complete" goto not_installed
"%~dp0.venv\Scripts\python.exe" -I -B -X utf8 "%~dp0tools\audio-setup\setup_local_audio.py" %*
set "audio_setup_status=%ERRORLEVEL%"
echo.
echo The default command prints a plan only. Use --archive, --destination and --interactive to prepare.
exit /b %audio_setup_status%
:not_installed
echo Complete the application installation before running this local setup tool.
exit /b 1
