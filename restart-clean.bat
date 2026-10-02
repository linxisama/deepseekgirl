@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo [1/2] 清理旧的机器人进程（多开会导致卡死）...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'SilentlyContinue';" ^
  "$root = (Resolve-Path '.').Path;" ^
  "$pids = New-Object System.Collections.Generic.HashSet[int];" ^
  "netstat -ano | Select-String ':47653|:47654' | ForEach-Object { $parts = ($_.ToString().Trim() -split '\s+'); if ($parts -contains 'LISTENING' -and $parts[-1] -match '^\d+$') { [void]$pids.Add([int]$parts[-1]) } };" ^
  "Get-Process python,pythonw | Where-Object { $_.Path -like ($root + '\.venv\Scripts\python*.exe') } | ForEach-Object { [void]$pids.Add($_.Id) };" ^
  "foreach ($processId in $pids) { try { Stop-Process -Id $processId -Force -ErrorAction Stop; Write-Host ('  已结束进程 ' + $processId) } catch {} }"
timeout /t 3 /nobreak >nul

echo [2/2] 启动机器人看门狗（崩溃后自动恢复）...
if not exist ".venv\Scripts\python.exe" (
    echo Python environment is missing. Run setup.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" supervisor.py
if errorlevel 1 (
    echo Supervisor exited unexpectedly.
    pause
)
