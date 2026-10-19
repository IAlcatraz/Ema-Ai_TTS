@echo off
:: UTF-8 console. Without it cmd.exe is codepage 437 and Python
:: raises UnicodeEncodeError on the first em dash it prints.
chcp 65001 >nul
title Riko - conversation
setlocal
cd /d "%~dp0"

::  Must match RIKO_CHARACTER in start_servers.bat.
set "RIKO_CHARACTER=riko"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   No virtual environment found. Run setup.bat first.
    echo.
    pause
    exit /b 1
)

echo Starting the conversation loop.
echo Speak when it says "listening". Ctrl-C to stop.
echo.
.venv\Scripts\python.exe -m server.chat_terminal
pause
