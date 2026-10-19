@echo off
echo 🛑 Stopping XTTS service on port 8020...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8020') do taskkill /F /PID %%a 2>nul

echo ⏳ Waiting for port to clear...
timeout /t 2 /nobreak >nul

echo 🚀 Starting XTTS service...
:: On lance le TTS dans une nouvelle fenêtre pour que vous puissiez voir les logs
start "XTTS-v2 Service" cmd /k "G:\AI-Chat\XTTS-Test\.venv\Scripts\python.exe -m server.tts.xtts_service"

echo ✅ XTTS is restarting in a new window.
pause