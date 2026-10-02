@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

if /I "%~1"=="--check" (
    if exist "一键启动机器人-防崩溃.bat" (
        echo CHECK_OK
        exit /b 0
    )
    echo CHECK_FAILED
    exit /b 1
)

echo ==========================================
echo   安装开机自启 - 防崩溃版本
echo ==========================================
echo.

if not exist "一键启动机器人-防崩溃.bat" (
    echo [错误] 找不到一键启动机器人-防崩溃.bat。
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root=(Resolve-Path '.').Path;" ^
  "$startup=[Environment]::GetFolderPath('Startup');" ^
  "$link=Join-Path $startup '微信鲸鱼娘机器人-防崩溃.lnk';" ^
  "$target=Join-Path $root '一键启动机器人-防崩溃.bat';" ^
  "$shell=New-Object -ComObject WScript.Shell;" ^
  "$shortcut=$shell.CreateShortcut($link);" ^
  "$shortcut.TargetPath=$target;" ^
  "$shortcut.WorkingDirectory=$root;" ^
  "$shortcut.Description='微信鲸鱼娘机器人防崩溃启动';" ^
  "$shortcut.Save();" ^
  "Write-Host ('已创建开机自启: ' + $link)"

if errorlevel 1 (
    echo [失败] 创建开机自启失败。
    pause
    exit /b 1
)

echo.
echo 安装完成。下次登录 Windows 时会自动启动机器人。
echo 如需取消，运行“卸载开机自启.bat”。
pause
exit /b 0
