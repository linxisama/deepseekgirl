@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

fltmc >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -WorkingDirectory '%~dp0' -Verb RunAs"
    exit /b
)

if not exist ".venv\Scripts\python.exe" (
    echo Python environment is missing. Run setup.bat first.
    pause
    exit /b 1
)

echo Cleaning old bot processes...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; $root=(Resolve-Path '.').Path; $pids=New-Object System.Collections.Generic.HashSet[int]; netstat -ano | Select-String ':47653|:47654' | ForEach-Object { $parts=($_.ToString().Trim() -split '\s+'); if ($parts -contains 'LISTENING' -and $parts[-1] -match '^\d+$') { [void]$pids.Add([int]$parts[-1]) } }; Get-Process python,pythonw | Where-Object { $_.Path -like ($root + '\.venv\Scripts\python*.exe') } | ForEach-Object { [void]$pids.Add($_.Id) }; foreach ($processId in $pids) { Stop-Process -Id $processId -Force }"
timeout /t 2 /nobreak >nul

echo Starting bot supervisor...
:run_supervisor
".venv\Scripts\python.exe" supervisor.py
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" exit /b 0
echo Supervisor exited with code %EXIT_CODE%. Restarting in 3 seconds...
timeout /t 3 /nobreak >nul
goto run_supervisor
