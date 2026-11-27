@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run Install.cmd in this folder first.
  pause
  exit /b 1
)
if not exist "%~dp0.venv\.installation-complete" (
  echo Installation is incomplete. Finish the reviewed install or Repair first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -I -B -X utf8 "%~dp0tools\audio-setup\setup_local_audio.py" --interactive %*
set "CARR_AUDIO_SETUP_EXIT=%ERRORLEVEL%"
if not "%CARR_AUDIO_SETUP_EXIT%"=="0" echo Local audio setup did not complete. The details are shown above.
pause
exit /b %CARR_AUDIO_SETUP_EXIT%
