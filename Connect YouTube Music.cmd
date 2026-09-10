@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Run Install.cmd first, then connect YouTube Music.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m ytlikes.cli setup
