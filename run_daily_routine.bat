@echo off
setlocal
title Korea Market v3 - DAILY ROUTINE (MOCK)
cd /d "%~dp0"

echo ======================================================================
echo  [DAILY ROUTINE] Korea Market v3 Automated Trading Routine (MOCK)
echo  - Default Strategy: Champion B 종가매수 시가매도
echo  - Stage 1: 08:30~09:00 장개시전 동시호가 시가청산 / 09:00 정규장 개장 동기화
echo  - Stage 2: 15:15~15:30 실시간 주도주 종가베팅 발굴 및 매수 체결
echo  - Stage 3: 15:30 장마감 동시호가 체결 및 오버나잇 잔고 확정
echo  - Stage 4: Zero API Idle 완전 차단 안전 종료
echo  - Stage 5: Post-market 백테스트 vs 실전 자동 대사(Reconciliation)
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
