@echo off
setlocal enabledelayedexpansion

REM ============================================
REM TikTok x Roblox Live Engine — Startup Auto
REM
REM Abre em ordem, tudo sozinho:
REM   1. Navegador web -> live do TikTok (@live.engine)
REM   2. OBS Studio (obs-websocket em 4455)
REM   3. Roblox Studio (BridgeClient em ServerScriptService)
REM   4. Engine Python (Local API :8787 + TikTok connector + OBS adapter)
REM
REM Uso: duplo-clique em start_all.bat
REM ============================================

cd /d "%~dp0"

echo ============================================
echo  TikTok x Roblox Live Engine
echo  TikTok: @live.engine
echo ============================================
echo.

REM Inicia o launcher Python que dispara tudo (navegador, OBS, Roblox, engine)
start /b "" python launcher.py

echo [start_all] Launcher iniciado em segundo plano.
echo [start_all] Verifique a barra de tarefas para gerenciar os processos.
echo [start_all] Use stop_all.bat para parar todos os componentes.
echo.
timeout /t 5 /nobreak >nul
:loop
timeout /t 10 /nobreak >nul
goto loop