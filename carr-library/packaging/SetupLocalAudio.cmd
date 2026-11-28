@echo off
setlocal
chcp 65001 >nul
if not exist "%~dp0.venv\Scripts\python.exe" goto not_installed
if not exist "%~dp0.venv\.installation-complete" goto not_installed
"%~dp0.venv\Scripts\python.exe" -I -B -X utf8 -m carr_library.local_audio_setup --interactive
set "audio_setup_status=%ERRORLEVEL%"
echo.
echo Натисніть будь-яку клавішу, щоб закрити це вікно.
pause >nul
exit /b %audio_setup_status%
:not_installed
echo Спочатку завершіть Install.cmd у цій папці, потім запустіть SetupLocalAudio.cmd знову.
pause
exit /b 1

