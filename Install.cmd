@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
    echo Installation could not finish. Check the message above.
    pause
    exit /b 1
)
call "Connect YouTube Music.cmd"
