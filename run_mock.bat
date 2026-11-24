@echo off
setlocal
title Korea Market v3 - MOCK TRADING (VTS)
cd /d "%~dp0"

echo ======================================================================
echo  [MOCK TRADING (VTS)] Korea Market v3 Paper Trading
echo  Strategy: Champion B 종가매수 시가매도 (champion_b / anchor_close / anchor_morning)
echo  Capital: 10,000,000 KRW
echo ======================================================================
echo.

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
