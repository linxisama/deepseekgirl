@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Python environment is missing. Run the setup commands from README.md first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" web_main.py
if errorlevel 1 pause
