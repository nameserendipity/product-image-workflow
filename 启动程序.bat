@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
set "DEFAULT_URL=http://127.0.0.1:8765"
set "URL="

rem Reuse an already running service instead of starting a duplicate.
curl.exe --silent --fail --max-time 2 "%DEFAULT_URL%/api/status" >nul 2>nul
if not errorlevel 1 (
    set "URL=%DEFAULT_URL%"
    goto :ready
)

if exist "%ROOT%ProductImageWorkflow.exe" (
    del /q "%ROOT%startup_url.txt" "%ROOT%startup_error.log" 2>nul
    start "" /b "%ROOT%ProductImageWorkflow.exe" --no-browser
    for /l %%i in (1,1,15) do (
        if exist "%ROOT%startup_url.txt" goto :ready
        timeout /t 1 /nobreak >nul
    )
    goto :failed
)

if not exist "%ROOT%bootstrap.ps1" (
    echo bootstrap.ps1 not found.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%bootstrap.ps1" -Mode Ensure -NonInteractive -Root "%ROOT%"
if errorlevel 1 (
    echo.
    echo Runtime bootstrap failed. Check the network and the error above.
    pause
    exit /b 1
)

del /q "%ROOT%startup_url.txt" "%ROOT%startup_error.log" 2>nul
start "" /b "%PYTHON%" "%ROOT%web_app.py" --no-browser
for /l %%i in (1,1,15) do (
    if exist "%ROOT%startup_url.txt" goto :ready
    timeout /t 1 /nobreak >nul
)
goto :failed

:ready
if not defined URL set /p "URL=" < "%ROOT%startup_url.txt"
if not defined URL goto :failed
start "" "%URL%"
exit /b 0

:failed
echo.
echo Program startup failed.
if exist "%ROOT%startup_error.log" type "%ROOT%startup_error.log"
echo Check that the release package is fully extracted and not blocked by Windows Security.
pause
exit /b 1
