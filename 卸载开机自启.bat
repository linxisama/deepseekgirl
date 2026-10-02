@echo off
chcp 65001 >nul
setlocal EnableExtensions

if /I "%~1"=="--check" (
    echo CHECK_OK
    exit /b 0
)

echo 正在删除微信鲸鱼娘机器人的开机自启快捷方式...

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$startup=[Environment]::GetFolderPath('Startup');" ^
  "$link=Join-Path $startup '微信鲸鱼娘机器人-防崩溃.lnk';" ^
  "if (Test-Path -LiteralPath $link) { Remove-Item -LiteralPath $link -Force; Write-Host '已删除开机自启。' } else { Write-Host '没有找到开机自启快捷方式。' }"

if errorlevel 1 (
    echo [失败] 删除开机自启失败。
    pause
    exit /b 1
)

pause
exit /b 0
