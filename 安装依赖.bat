@echo off
setlocal
chcp 65001 >nul
set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [1/3] 正在创建 Python 虚拟环境...
    py -3.12 -m venv "%ROOT%.venv"
    if errorlevel 1 (
        echo 未找到 Python 3.12。请安装 Python 3.12 或更高版本，并勾选 Add Python to PATH。
        pause
        exit /b 1
    )
)

echo [2/3] 正在安装 Python 依赖...
"%PYTHON%" -m pip install --upgrade pip
if errorlevel 1 goto :error
"%PYTHON%" -m pip install -r "%ROOT%requirements.txt"
if errorlevel 1 goto :error

echo [3/3] 正在安装 Chromium 浏览器组件...
"%PYTHON%" -m playwright install chromium
if errorlevel 1 goto :error

echo.
echo 依赖安装完成。请复制 local_settings.example.json 为 local_settings.json 后填写 API 配置。
pause
exit /b 0

:error
echo.
echo 依赖安装失败，请检查网络、Python 安装和命令行错误信息。
pause
exit /b 1
