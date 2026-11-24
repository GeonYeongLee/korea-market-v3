@echo off
setlocal
title Korea Market v3 - LIVE TRADING (REAL CASH)
cd /d "%~dp0"

echo ======================================================================
echo  [WARNING: REAL CASH LIVE TRADING]
echo  Orders will be executed on real brokerage account with REAL MONEY!
echo  Strategy: Korea Market v3 Champion B (Close-to-Open 종가매수 시가매도)
echo  Components: champion_b / anchor_close / anchor_morning
echo  Capital: 10,000,000 KRW (Fixed Single Notional)
echo ======================================================================
echo.
set /p CONFIRM="Are you sure you want to start REAL CASH trading? (Type YES to proceed): "
if not "%CONFIRM%"=="YES" (
    echo [ABORTED] Live trading cancelled.
    pause
    exit /b 0
)

set PYTHON_CMD=py -3.12
where py >nul 2>nul
if %errorlevel% neq 0 (
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set PYTHON_CMD="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    ) else (
        set PYTHON_CMD=python
    )
)

%PYTHON_CMD% main.py live --yes %*
pause
