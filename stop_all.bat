@echo off
setlocal enabledelayedexpansion

REM ============================================
REM TikTok x Roblox Live Engine — Stop All
REM ============================================

echo [stop] Parando componentes...

REM --- Engine Python ---
echo [stop] Parando Live Engine (Python)...
for /f "tokens=2" %%a in ('tasklist /fi "imagename eq python.exe" /fo csv 2^>nul ^| find /i "src.app"') do (
    taskkill /pid %%a /f >nul 2>&1
)
timeout /t 2 /nobreak >nul

REM --- OBS Studio ---
echo [stop] Parando OBS Studio...
taskkill /im obs.exe /f >nul 2>&1
timeout /t 1 /nobreak >nul

REM --- Roblox Studio ---
echo [stop] Parando Roblox Studio...
taskkill /im RobloxStudioLauncher.exe /f >nul 2>&1
taskkill /im RobloxStudio.exe /f >nul 2>&1

echo [stop] Concluido.