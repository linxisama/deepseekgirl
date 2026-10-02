@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

if /I "%~1"=="--check" (
    echo CHECK_OK
    exit /b 0
)

echo ==========================================
echo   微信鲸鱼娘机器人 - 一键安装所有依赖
echo ==========================================
echo.
echo 将自动安装 Python 3.12、项目 .venv 和全部依赖。
echo 首次安装需要联网，请勿关闭窗口。
echo.

if not exist "setup.bat" (
    echo [错误] 找不到 setup.bat。
    pause
    exit /b 1
)

call "%~dp0setup.bat" --no-prompt
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [失败] 依赖安装没有完成，返回码=%RC%。
    echo 请查看 AI自动部署说明.txt 和 先看我-安装使用说明书.txt。
    pause
    exit /b %RC%
)

echo [完成] 所有依赖已经安装。
echo 下一步建议双击：一键启动机器人-防崩溃.bat
echo.
pause
exit /b 0
