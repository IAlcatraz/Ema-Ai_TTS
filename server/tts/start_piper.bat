@echo off
echo [Piper TTS] Starting Piper Service...
cd /d "%~dp0"
:: We assume the user is running in the project's virtual environment or has piper-tts installed
python piper_service.py
pause