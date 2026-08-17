@echo off
setlocal
chcp 65001 >nul
set "ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%bootstrap.ps1" -Mode Ensure -Root "%ROOT%"
if errorlevel 1 goto :error

echo.
echo 依赖安装完成。首次启动网页时填写模型 API Key。
pause
exit /b 0

:error
echo.
echo 依赖安装失败，请检查网络、Python 安装和命令行错误信息。
pause
exit /b 1
