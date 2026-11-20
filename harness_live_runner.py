"""
Korea Market v3 - Feature-Specific Live Trading Verification Runner
===================================================================
Executes all 6 functional live trading test harnesses and displays
a comprehensive status table with verification metrics.

Usage:
  python harness_live_runner.py
"""
import sys
import time
import unittest
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tests.test_live_trading_feature_harnesses import (
    TestHarness1_AuthAndAccountConnectivity,
    TestHarness2_RealtimeAnchorScanner,
    TestHarness3_EntryOrderExecutionAndCollarCapGuard,
    TestHarness4_OvernightAndMorningExit,
    TestHarness5_WebSocketAndNoticeReconciliation,
    TestHarness6_DailyRoutineEndToEndOrchestrator,
)

console = Console(force_terminal=True, legacy_windows=False)


def run_harness():
    console.print(Panel.fit(
        "[bold green]🛡️ Korea Market v3 - Live Trading Feature Test Harnesses[/bold green]\n"
        "[cyan]목적:[/cyan] 실전매매(Live/Mock) 핵심 6대 기능 하니스 직접 검증\n"
        "[cyan]대상:[/cyan] 인증, 실시간 스캐너, 동시호가 진입, 오버나잇 익일청산, 웹소켓 체결통보, 일일 루틴",
        border_style="green",
    ))

    harnesses = [
        ("Harness 1: Auth & Account Connectivity", TestHarness1_AuthAndAccountConnectivity, "OAuth2 토큰/승인키/주문가능현금(ord_psbl_cash) 우선 추출"),
        ("Harness 2: Realtime Anchor Scanner", TestHarness2_RealtimeAnchorScanner, "15:15~15:20 실시간 거래대금 상위 조회, 양봉 몸통 필터링, 알파 점수 산출 및 후보 주입"),
        ("Harness 3: Entry Execution & Collar Cap", TestHarness3_EntryOrderExecutionAndCollarCapGuard, "15:20 종가베팅 진입 호가(+0.5% 칼라캡) 및 KRX 단독 라우팅, 주문 거부 롤백"),
        ("Harness 4: Overnight & Morning Call Auction", TestHarness4_OvernightAndMorningExit, "15:20 당일 청산 예외 보호, 08:50 시가 동시호가 시장가 매도 발주, 09:00 시가 청산"),
        ("Harness 5: WebSocket Feed & Notice Reconcile", TestHarness5_WebSocketAndNoticeReconciliation, "H0UNCNT0 체결통보 오프셋 파싱, 실시간 예수금 차감, 비체결 통보 필터링"),
        ("Harness 6: Daily Routine End-to-End", TestHarness6_DailyRoutineEndToEndOrchestrator, "Stage 1~4 전자동 일일 퀀트 루틴 전체 파이프라인 통합 정합성 검증"),
    ]

    table = Table(title="📋 실전매매 기능별 하니스 검증 결과 (Live Trading Verification Report)", show_header=True, header_style="bold magenta")
    table.add_column("하니스 구분 (Harness)", style="bold white", width=36)
    table.add_column("검증 항목 (Scope)", style="cyan", width=55)
    table.add_column("테스트 수", justify="center", style="bold yellow", width=12)
    table.add_column("소요 시간", justify="right", style="bold blue", width=12)
    table.add_column("판정 (Status)", justify="center", width=14)

    all_passed = True
    total_tests = 0
    start_total = time.time()

    for name, test_case_cls, scope in harnesses:
        suite = unittest.TestLoader().loadTestsFromTestCase(test_case_cls)
        num_tests = suite.countTestCases()
        total_tests += num_tests

        t0 = time.time()
        result = unittest.TestResult()
        suite.run(result)
        el = (time.time() - t0) * 1000

        if result.wasSuccessful():
            status_str = "[bold green]PASS ✓[/bold green]"
        else:
            status_str = f"[bold red]FAIL ✗ ({len(result.failures) + len(result.errors)})[/bold red]"
            all_passed = False

        table.add_row(name, scope, f"{num_tests} tests", f"{el:.1f} ms", status_str)

    total_el = (time.time() - start_total) * 1000
    console.print(table)

    summary_color = "bold green" if all_passed else "bold red"
    summary_text = (
        f"[{summary_color}]🎉 전체 {total_tests}개 실전매매 기능 하니스 전수 통과 (100% Pass) - 소요 시간: {total_el:.1f}ms[/{summary_color}]\n"
        "[white]모든 실전매매 API 연동, 호가 접수, 리스크 칼라캡, 오버나잇 청산 로직이 완전히 검증되었습니다.[/white]"
        if all_passed else
        f"[{summary_color}]⚠️ 일부 하니스에서 실패가 발생했습니다. 로그를 점검하세요.[/{summary_color}]"
    )
    console.print(Panel(summary_text, border_style="green" if all_passed else "red"))


if __name__ == "__main__":
    run_harness()
