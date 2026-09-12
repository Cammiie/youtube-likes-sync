@echo off
cd /d "%~dp0"
".venv\Scripts\pythonw.exe" -m ytlikes.packaged download-status
