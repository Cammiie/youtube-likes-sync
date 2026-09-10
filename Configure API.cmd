@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m ytlikes.cli configure-api
pause
