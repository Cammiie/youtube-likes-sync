@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m ytlikes.cli open-monochrome
if errorlevel 1 pause
