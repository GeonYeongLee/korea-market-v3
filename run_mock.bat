@echo off
setlocal
title Korea Market v3 - MOCK TRADING (VTS)
cd /d "%~dp0"

echo [INFO] Starting Mock Paper Trading (VTS) with Korea Market v3...

set PYTHON_CMD=py -3.12
where py >nul 2>nul
if %errorlevel% neq 0 (
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set PYTHON_CMD="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    ) else (
        set PYTHON_CMD=python
    )
)

%PYTHON_CMD% main.py mock %*
pause
