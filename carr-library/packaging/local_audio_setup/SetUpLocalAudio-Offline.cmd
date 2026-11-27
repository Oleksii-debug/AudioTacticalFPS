@echo off
setlocal EnableExtensions DisableDelayedExpansion
if not exist "%~dp0.venv\.installation-complete" goto not_installed
if not exist "%~dp0.venv\Scripts\python.exe" goto not_installed
if not defined LOCALAPPDATA goto no_local_data
set "RESONANCE_AUDIO_OUTPUT=%LOCALAPPDATA%\Resonance\runtimes\ffmpeg-9.1"
set "RESONANCE_AUDIO_CACHE=%LOCALAPPDATA%\Resonance\runtime-cache\ffmpeg-9.1"
if exist "%RESONANCE_AUDIO_OUTPUT%" goto output_exists
echo Resonance: optional local audio setup.
echo Offline mode: place ffmpeg-9.1-essentials_build.zip in the cache folder shown below.
echo No network request is made in offline mode.
echo Cache folder: "%RESONANCE_AUDIO_CACHE%"
echo The application package and its sealed manifest will not be changed.
echo Runtime destination: "%RESONANCE_AUDIO_OUTPUT%"
echo.
"%~dp0.venv\Scripts\python.exe" -I -B -X utf8 "%~dp0setup\prepare_windows_audio.py" --lock "%~dp0setup\windows-audio.lock.json" --cache "%RESONANCE_AUDIO_CACHE%" --output "%RESONANCE_AUDIO_OUTPUT%"
set "RESONANCE_AUDIO_EXIT=%ERRORLEVEL%"
if not "%RESONANCE_AUDIO_EXIT%"=="0" goto preparation_failed
echo.
echo Local audio files were verified and prepared. This setup did not execute them.
echo Open the desired library in Resonance.
echo Audio Studio: Local voice and music engines...
echo Choose FFmpeg executable:
echo "%RESONANCE_AUDIO_OUTPUT%\bin\ffmpeg.exe"
echo Choose FFprobe executable:
echo "%RESONANCE_AUDIO_OUTPUT%\bin\ffprobe.exe"
echo Close and reopen Audio Studio after selecting both files.
echo Settings apply to the selected library. No system PATH was changed.
pause
exit /b 0

:not_installed
echo Run Install.cmd in this program folder and wait for Installation complete first.
pause
exit /b 1

:no_local_data
echo Windows did not provide LOCALAPPDATA. No runtime was prepared.
pause
exit /b 1

:output_exists
echo The runtime destination already exists and was left unchanged.
echo "%RESONANCE_AUDIO_OUTPUT%"
echo This run did not verify that existing directory.
echo A previously successful setup does not need to be repeated.
echo Select only a verified existing runtime, or use the documented command with a new output folder.
pause
exit /b 1

:preparation_failed
echo.
echo Audio setup could not finish. Read the error above.
echo No existing runtime was overwritten. The application remains installed.
pause
exit /b %RESONANCE_AUDIO_EXIT%
