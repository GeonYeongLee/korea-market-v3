@echo off
setlocal
title Korea Market v3 - DAILY ROUTINE (MOCK)
cd /d "%~dp0"

echo ======================================================================
echo  [DAILY ROUTINE] Korea Market v3 Automated Trading Routine (MOCK)
echo  - Stage 1: Wait for 09:00 regular market open ^& account sync
echo  - Stage 2: 09:00~09:25 Real-time KIS condition scanner ^& breakout buy
echo  - Stage 3: 09:00~ Dynamic SL ^& step-up TP / 15:20 EOD liquidation
echo  - Stage 4: Zero API Idle complete shutdown after session
echo  - Stage 5: Post-market backtest ^& reconciliation report
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

%PYTHON_CMD% main.py routine --mode mock %*
pause
