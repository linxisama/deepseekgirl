@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "TEMP=%CD%\.tmp"
set "TMP=%CD%\.tmp"
if not exist ".tmp" mkdir ".tmp" >nul 2>nul

echo ==========================================
echo   微信鲸鱼娘机器人 - 一键安装依赖
echo ==========================================
echo.

if /I "%~1"=="--check" goto :check_only

set "PYTHON_EXE="
call :find_python
if defined PYTHON_EXE goto :python_ready

echo [1/5] 未找到 Python 3.12，正在尝试自动安装...
where winget >nul 2>nul
if not errorlevel 1 (
    winget install -e --id Python.Python.3.12 --scope user --silent ^
        --accept-package-agreements --accept-source-agreements
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
    call :find_python
)

if not defined PYTHON_EXE (
    call :download_python
    call :find_python
)

if not defined PYTHON_EXE (
    echo.
    echo [错误] 自动安装 Python 3.12 失败。
    echo 请手动安装 64 位 Python 3.12，并勾选 Add Python to PATH。
    echo 下载地址: https://www.python.org/downloads/windows/
    echo.
    start "" "https://www.python.org/downloads/windows/"
    pause
    exit /b 1
)

:python_ready
echo 使用 Python: %PYTHON_EXE%
echo.

echo [2/5] 创建独立运行环境 .venv ...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>nul
    if errorlevel 1 (
        echo [错误] 现有 .venv 不是 Python 3.12，请先改名或删除该目录后重试。
        pause
        exit /b 1
    )
    echo 已存在兼容的 .venv，跳过创建。
) else (
    "%PYTHON_EXE%" -m venv ".venv"
    if errorlevel 1 goto :failed
)

echo.
echo [3/5] 更新 pip 和 wheel ...
".venv\Scripts\python.exe" -m ensurepip --upgrade
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install --upgrade pip wheel
if errorlevel 1 (
    echo 默认源失败，改用清华镜像重试...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip wheel ^
        -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 goto :failed
)

echo.
echo [4/5] 安装机器人核心依赖 ...
echo 首次安装需要联网，通常需要几分钟，请勿关闭窗口。
call :pip_install "requirements.windows.txt"
if errorlevel 1 goto :failed

echo.
echo [5/5] 安装可选兼容依赖（微信 3.9.x）...
call :pip_install_optional "requirements.wxauto3.txt"
if errorlevel 1 (
    echo [提示] 微信 3.9 兼容依赖下载失败。
    echo 不影响微信 4.x 使用；仅当需要支持微信 3.9.x 时再重试该步骤。
)

if not exist "config.yaml" (
    if exist "config.release.yaml" (
        copy /Y "config.release.yaml" "config.yaml" >nul
    )
)

echo.
echo 正在验证核心模块 ...
".venv\Scripts\python.exe" -c "import PySide6; print('桌面界面依赖通过')"
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, httpx; print('网页依赖通过')"
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -c "import yaml, cryptography, loguru, PIL, psutil, win32api; print('核心依赖通过')"
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -c "import wechatauto, wxauto4; print('微信 4.x 依赖通过')"
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -c "import faster_whisper, pysilk, imageio_ffmpeg; print('本地语音识别依赖通过')"
if errorlevel 1 goto :failed

echo.
echo ==========================================
echo   安装完成
echo ==========================================
echo 1. 打开 start.bat 启动桌面机器人
echo 2. 打开 start-web.bat 启动网页控制台
echo 3. 在 WebUI 中填写 API 地址、API Key 和模型
echo.

if /I "%~1"=="--no-prompt" goto :done
choice /C YN /N /M "是否现在启动网页控制台? [Y/N]: "
if errorlevel 2 goto :done
start "" "%~dp0start-web.bat"

:done
echo.
echo 安装流程结束。
pause
exit /b 0

:check_only
call :find_python
if not defined PYTHON_EXE (
    echo CHECK_FAILED: Python 3.12 not found
    exit /b 1
)
if not exist "requirements.windows.txt" (
    echo CHECK_FAILED: requirements.windows.txt missing
    exit /b 1
)
if not exist "main.py" (
    echo CHECK_FAILED: main.py missing
    exit /b 1
)
echo CHECK_OK
exit /b 0

:find_python
set "PYTHON_EXE="
where py >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%I in ('py -3.12 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%I"
)
if defined PYTHON_EXE exit /b 0
where python >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%I in ('python -c "import sys; print(sys.executable if sys.version_info[:2] == (3, 12) else '')" 2^>nul') do set "PYTHON_EXE=%%I"
)
exit /b 0

:download_python
set "PYTHON_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
set "PYTHON_INSTALLER=%TEMP%\python-3.12.10-amd64.exe"
echo 下载 Python 3.12.10 安装程序...
if exist "%PYTHON_INSTALLER%" del /f /q "%PYTHON_INSTALLER%" >nul 2>nul
where curl.exe >nul 2>nul
if not errorlevel 1 (
    curl.exe -L --fail --retry 3 -o "%PYTHON_INSTALLER%" "%PYTHON_URL%"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%PYTHON_INSTALLER%'"
)
if not exist "%PYTHON_INSTALLER%" (
    echo Python 安装程序下载失败。
    exit /b 1
)
echo 正在静默安装 Python 3.12 ...
start /wait "" "%PYTHON_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1
set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
exit /b 0

:pip_install
echo 安装依赖清单: %~1
".venv\Scripts\python.exe" -m pip install --no-cache-dir -r "%~1"
if not errorlevel 1 exit /b 0
echo 默认源失败，改用清华镜像重试...
".venv\Scripts\python.exe" -m pip install --no-cache-dir ^
    -i https://pypi.tuna.tsinghua.edu.cn/simple -r "%~1"
exit /b %errorlevel%

:pip_install_optional
echo 可选依赖清单: %~1
".venv\Scripts\python.exe" -m pip install --no-cache-dir ^
    --retries 0 --timeout 10 -r "%~1"
exit /b %errorlevel%

:failed
echo.
echo [错误] 安装失败，请检查网络、代理和 Python 3.12 安装。
echo 可参考 AI自动部署说明.txt 手动执行命令。
pause
exit /b 1
