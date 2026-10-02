@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

if /I "%~1"=="--check" (
    if exist "supervisor.py" if exist "start.bat" (
        echo CHECK_OK
        exit /b 0
    )
    echo CHECK_FAILED
    exit /b 1
)

echo ==========================================
echo   微信鲸鱼娘机器人 - 防崩溃启动
echo ==========================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo 未检测到 .venv，先运行一键安装所有依赖.bat。
    call "%~dp0一键安装所有依赖.bat"
    if errorlevel 1 exit /b 1
)

if not exist "supervisor.py" (
    echo [错误] 找不到 supervisor.py。
    pause
    exit /b 1
)

if not exist "start.bat" (
    echo [错误] 找不到 start.bat。
    pause
    exit /b 1
)

echo 正在启动看门狗。机器人崩溃或无响应时会自动重启。
echo.
call "%~dp0start.bat"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo 启动进程已退出，返回码=%RC%。
    echo 请查看 logs\supervisor.log 和 logs\errors.log。
    pause
)

exit /b %RC%
