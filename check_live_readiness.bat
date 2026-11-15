@echo off
setlocal
title Korea Market v3 - PRE-FLIGHT READINESS CHECK (VTS)
cd /d "%~dp0"

echo [INFO] Running Pre-Flight Live Trading Diagnostic (VTS)...

set PYTHON_CMD=py -3.12
where py >nul 2>nul
if %errorlevel% neq 0 (
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set PYTHON_CMD="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    ) else (
        set PYTHON_CMD=python
    )
)

%PYTHON_CMD% main.py check-live --env vts %*
pause
