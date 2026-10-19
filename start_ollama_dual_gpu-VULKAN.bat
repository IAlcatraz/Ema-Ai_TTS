@echo off
chcp 65001 >nul
title Ollama Vulkan dual GPU launcher
setlocal

:: Chemin explicite vers Ollama officiel Vulkan
set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"

:: GPU Vulkan
set "CHAT_GPU=1"
set "VISION_GPU=0"

:: Vérification du binaire
if not exist "%OLLAMA_EXE%" (
    echo ERREUR : Ollama officiel introuvable :
    echo %OLLAMA_EXE%
    pause
    exit /b 1
)

:: Vérifier les ports
for %%P in (11434 11435) do (
    netstat -ano -p tcp | findstr /r /c:":%%P .*LISTENING" >nul
    if not errorlevel 1 (
        echo ERREUR : le port %%P est deja utilise.
        echo Ferme le serveur correspondant avant de continuer.
        pause
        exit /b 1
    )
)

echo Lancement Ollama Vulkan - Chat GPU %CHAT_GPU% - port 11434
start "Ollama Vulkan Chat" cmd /k "set OLLAMA_VULKAN=1&& set GGML_VK_VISIBLE_DEVICES=%CHAT_GPU%&& set OLLAMA_HOST=127.0.0.1:11434&& ""%OLLAMA_EXE%"" serve"

timeout /t 3 /nobreak >nul

echo Lancement Ollama Vulkan - Vision GPU %VISION_GPU% - port 11435
start "Ollama Vulkan Vision" cmd /k "set OLLAMA_VULKAN=1&& set GGML_VK_VISIBLE_DEVICES=%VISION_GPU%&& set OLLAMA_HOST=127.0.0.1:11435&& ""%OLLAMA_EXE%"" serve"

echo.
echo Serveurs demandes :
echo Chat   : http://127.0.0.1:11434
echo Vision : http://127.0.0.1:11435
echo.
echo Pour tester le serveur vision dans une nouvelle fenetre :
echo set OLLAMA_HOST=http://127.0.0.1:11435
echo "%OLLAMA_EXE%" ps
echo.
pause