@echo off
chcp 65001 >nul
title IPEX Ollama dual GPU launcher
setlocal

:: IPEX-LLM / Intel Arc A770
:: Chat   -> GPU Level Zero 1, port 11434
:: Vision -> GPU Level Zero 0, port 11435
set "CHAT_GPU=1"
set "VISION_GPU=0"

:: Vérifier que le port du serveur Chat est libre
netstat -ano -p tcp | findstr /r /c:":11434 .*LISTENING" >nul
if not errorlevel 1 (
    echo.
    echo Port 11434 deja utilise.
    echo Ferme Ollama depuis la zone de notification
    echo ou arrete le serveur deja lance.
    echo.
    pause
    exit /b 1
)

echo Demarrage IPEX Chat sur GPU %CHAT_GPU% - port 11434...
start "IPEX Chat GPU %CHAT_GPU%" cmd /k "set OLLAMA_NUM_GPU=999&& set no_proxy=localhost,127.0.0.1&& set ZES_ENABLE_SYSMAN=1&& set ONEAPI_DEVICE_SELECTOR=level_zero:%CHAT_GPU%&& set OLLAMA_HOST=127.0.0.1:11434&& ollama serve"

echo Demarrage IPEX Vision sur GPU %VISION_GPU% - port 11435...
start "IPEX Vision GPU %VISION_GPU%" cmd /k "set OLLAMA_NUM_GPU=999&& set no_proxy=localhost,127.0.0.1&& set ZES_ENABLE_SYSMAN=1&& set ONEAPI_DEVICE_SELECTOR=level_zero:%VISION_GPU%&& set OLLAMA_HOST=127.0.0.1:11435&& ollama serve"

echo.
echo Les deux fenetres doivent rester ouvertes.
echo.
echo Chat   : http://127.0.0.1:11434
echo Vision : http://127.0.0.1:11435
echo.
pause