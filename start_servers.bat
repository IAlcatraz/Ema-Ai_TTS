@echo off
:: UTF-8 console. Without it cmd.exe is codepage 437 and Python
:: raises UnicodeEncodeError on the first em dash it prints.
chcp 65001 >nul
title Riko Server Launcher
setlocal

:: ===========================================================================
set "XTTS_PYTHON=G:\AI-Chat\XTTS-Test\.venv\Scripts\python.exe"
set "XTTS_DEVICE=auto"
set "XTTS_FFMPEG_PATH=G:\AI-Chat\riko_project_v2_patreon\tools\ffmpeg\bin\ffmpeg.exe"
:: Pin XTTS to one Intel GPU (0 or 1) so it stays off the chat card when using
:: start_ollama_dual_gpu.bat. Leave empty to let PyTorch see both GPUs.
set "XTTS_GPU=0"
set "COQUI_TOS_AGREED=1"
set "HF_HUB_DISABLE_TELEMETRY=1"

::  Which character to load. Must match a config file:
::    riko  -> character_config.yaml
::    bob   -> character_config_bob.yaml
set "RIKO_CHARACTER=riko"

:: ===========================================================================
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   No virtual environment found. Run setup.bat first.
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo.
    echo   No .env file found. Copy .env.example to .env and paste in your
    echo   API keys, then run this again.
    echo.
    pause
    exit /b 1
)

:: Read the port out of server/config.py so this window can never announce a
:: number the server is not actually using. Pure batch - no Python start-up, and
:: nothing to go stale. The line it matches is:   SERVER_PORT: int = 8100
set "SERVER_PORT="
for /f "tokens=2 delims==" %%P in ('findstr /b /c:"SERVER_PORT: int =" "server\config.py"') do set "SERVER_PORT=%%P"
set "SERVER_PORT=%SERVER_PORT: =%"
if not defined SERVER_PORT set "SERVER_PORT=8100"

echo ======================================
echo   Starting Riko - character: %RIKO_CHARACTER%
echo ======================================
echo.

echo Starting the server ^(port %SERVER_PORT%^)...
start "Riko Server" /D "%~dp0" cmd /k "set RIKO_CHARACTER=%RIKO_CHARACTER%&& .venv\Scripts\python.exe -m server.server"

echo Starting the web page ^(port 5173^)...
start "Riko Web Page" /D "%~dp0client" cmd /k "npx vite --host 0.0.0.0"

netstat -ano -p tcp | findstr /r /c:":8020 .*LISTENING" >nul
if not errorlevel 1 (
    echo XTTS-v2 is already listening on port 8020; leaving it running.
) else if exist "%XTTS_PYTHON%" (
    echo Starting private XTTS-v2 with automatic Intel XPU detection. The first speech request loads the model...
    if defined XTTS_GPU (
        start "XTTS-v2 private voice" /D "%~dp0" cmd /k "set ONEAPI_DEVICE_SELECTOR=level_zero:%XTTS_GPU%&& "%XTTS_PYTHON%" -m server.tts.xtts_service"
    ) else (
        start "XTTS-v2 private voice" /D "%~dp0" cmd /k ""%XTTS_PYTHON%" -m server.tts.xtts_service"
    )
) else (
    echo XTTS-v2 Python environment was not found at:
    echo    %XTTS_PYTHON%
    echo Edit XTTS_PYTHON at the top of start_servers.bat.
)

echo.
echo ======================================
echo   Launched. LEAVE THOSE WINDOWS OPEN.
echo ======================================
echo.
echo   Next:
echo     1. Open  http://127.0.0.1:5173  in your browser. You should see her.
echo     2. Click once on the page ^(browsers block audio until you do^).
echo     3. Run  talk.bat  and start speaking.
echo.
echo   If the 'Riko Server' window shows a port error instead of
echo   "listening on http://127.0.0.1:%SERVER_PORT%", run check_setup.bat -
echo   test 8 diagnoses it and prints the fix.
echo.
pause
