"""
Korea Market v3 - Automated Daily Trading Routine Orchestrator
==============================================================
Orchestrates the complete 4-stage daily quantitative lifecycle:

[Stage 1] 08:55 ~ 09:00 : 장 시작 대기 및 계좌/마스터 동기화
[Stage 2] 09:00 ~ 09:25 : 실시간 KIS 조건검색 스캐너 가동 및 돌파 매수 체결
[Stage 3] 09:00 ~ 매매종료 : 동적 손절 & 스마트 스텝업 익절 관리 및 15:20 동시호가 청산
[Stage 4] 매매 완료 즉시 : 모든 실시간 API 호출 차단 (스캐너 중지, 웹소켓 연결 해제) 후 안전 종료 (Zero API Idle)

Usage:
  python -m engine.daily_routine --mode mock
  python -m engine.daily_routine --mode live --yes
  python main.py routine --mode mock
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import threading
from datetime import datetime, date, time as dtime
from pathlib import Path
from typing import Optional

# Ensure UTF-8 output on Windows terminals
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from rich.console import Console
from rich.panel import Panel

from config.settings import settings
from core.telemetry import telemetry
from engine.broker.kis_live import KisLiveBroker
from engine.clock import RealtimeClock
from engine.datafeed.kis_live import KisRealtimeFeed
from engine.risk.risk_manager import RiskManager
from engine.engine import TradingEngine
from data_pipeline.realtime_scanner import RealtimeLeaderScanner, RealtimeAnchorScanner
from strategy.registry import build_strategy

logger = logging.getLogger("kis.engine.daily_routine")
console = Console(force_terminal=True, legacy_windows=False)


class DailyRoutineOrchestrator:
    """End-to-End Orchestrator for the Complete Daily Trading Routine."""

    def __init__(
        self,
        mode: str = "mock",
        target_date: Optional[str] = None,
        scanner_name: str = "champion_b",
        entry_name: str = "anchor_close",
        exit_name: str = "anchor_morning",
        capital: float = 10_000_000.0,
    ):
        self.mode = mode.lower()
        self.target_date = target_date or datetime.now().strftime("%Y%m%d")
        self.scanner_name = scanner_name
        self.entry_name = entry_name
        self.exit_name = exit_name
        self.capital = capital

        telemetry.setup_file_logging(level=logging.INFO)
        self.strategy = build_strategy(
            scanner_name=self.scanner_name,
            entry_name=self.entry_name,
            exit_name=self.exit_name,
            total_capital=self.capital,
            dynamic=True,
        )

    @property
    def is_overnight_strategy(self) -> bool:
        """Determines if the active strategy holds positions overnight across market close."""
        return (
            self.scanner_name in ("champion_b", "anchor_body_micro", "anchor_close", "crisis_rebound")
            or "anchor" in getattr(self.strategy, "name", "").lower()
            or "crisis" in getattr(self.strategy, "name", "").lower()
        )

    def run(self, wait_for_market_open: bool = True, single_cycle: bool = False) -> None:
        """Executes the complete 4-stage daily trading routine."""
        date_iso = (
            f"{self.target_date[:4]}-{self.target_date[4:6]}-{self.target_date[6:]}"
            if len(self.target_date) == 8
            else self.target_date
        )

        cutoff_t = getattr(self.strategy, "cutoff_time", dtime(9, 25, 0))
        cutoff_str = cutoff_t.strftime("%H:%M")

        console.print(Panel.fit(
            f"[bold green]🌅 [DAILY ROUTINE] 한국 주식시장 일일 퀀트 트레이딩 전자동 루틴 가동[/bold green]\n"
            f"[cyan]운용 모드:[/cyan] {'[green]모의투자 (MOCK / VTS)[/green]' if self.mode == 'mock' else '[bold red]실전투자 (LIVE / REAL)[/bold red]'}\n"
            f"[cyan]대상 일자:[/cyan] {date_iso} | [cyan]전략:[/cyan] {self.strategy.name}\n"
            f"[cyan]운용 원금:[/cyan] {self.capital:,.0f}원 (단리 고정 1,000만 원)",
            border_style="green",
        ))

        # =====================================================================
        # [STAGE 1] 장 시작 전 대기 (09:00:01까지 대기) 및 계좌 상태 동기화
        # =====================================================================
        env_mode = "vts" if self.mode == "mock" else "real"
        cfg = settings.switch_env(env_mode)
        stage1_broker = KisLiveBroker(mode=env_mode, cfg=cfg)
        try:
            stage1_broker.sync_account_state()
            bal = stage1_broker.get_balance()
            if bal and bal.cash > 0:
                if bal.cash < self.capital:
                    console.print(
                        f"[bold yellow]💡 [예수금 비례 1/N 배분] 계좌 예수금({bal.cash:,.0f}원)이 기준 원금({self.capital:,.0f}원) 미만입니다.[/bold yellow]\n"
                        f"   ➜ 가용 예수금을 기준으로 당일 슬롯에 1/N 배분하여 안전 매매합니다."
                    )
                if hasattr(self.strategy, "sync_capital_with_balance"):
                    self.strategy.sync_capital_with_balance(bal.cash)
            logger.info("Pre-market account state synced successfully.")
        except Exception as e:
            logger.debug(f"Could not pre-sync account state: {e}")

        try:
            import core.krx_symbols
            core.krx_symbols.get_krx_name_map()
            logger.info("KRX symbol map loaded successfully.")
        except Exception as e:
            logger.debug(f"Could not pre-load KRX symbol map: {e}")

        is_overnight_strat = self.scanner_name in ("champion_b", "anchor_body_micro", "anchor_close") or "anchor" in getattr(self.strategy, "name", "").lower()
        now = datetime.now()
        market_open_time = dtime(9, 0, 0)
        auction_order_time = dtime(8, 50, 0)

        # For overnight strategies, participate in the 08:30~09:00 Call Auction at 08:50:00 to achieve exact 09:00 open price (0% slippage)
        if is_overnight_strat and now.time() < market_open_time:
            open_pos = stage1_broker.get_positions()
            if open_pos:
                if now.time() < auction_order_time:
                    wait_sec = (datetime.combine(now.date(), auction_order_time) - now).total_seconds()
                    console.print(f"[yellow]⏳ [STAGE 1.5] 08:50 장개시전 동시호가 청산 접수까지 대기합니다 ({wait_sec:.0f}초)...[/yellow]")
                    time.sleep(max(0, wait_sec))

                console.print(f"[bold yellow]📢 [STAGE 1.5] 08:50 KRX 장개시전 동시호가(08:30~09:00) 시장가 청산 주문 전송[/bold yellow]")
                from engine.models import Order, OrderSide, OrderType
                for sym, p in list(open_pos.items()):
                    console.print(f"   ➜ 동시호가 매도 발주: {sym} x {p.quantity}주 (09:00 시초가 단일가 무슬리피지 0.0% 체결)")
                    stage1_broker.submit_order(Order(
                        order_id=f"OPEN_AUCTION_{sym}_{int(time.time())}",
                        symbol=sym,
                        side=OrderSide.SELL,
                        order_type=OrderType.MARKET,
                        quantity=p.quantity,
                        price=0.0,
                        tag="KRX_0900_OPEN_AUCTION",
                    ))

        if wait_for_market_open and now.time() < market_open_time:
            sec_to_wait = (datetime.combine(now.date(), market_open_time) - now).total_seconds()
            if sec_to_wait > 0:
                console.print(f"[yellow]⏳ [STAGE 1] 정규장 시작(09:00)까지 대기합니다 ({sec_to_wait:.0f}초 남음)...[/yellow]")
                time.sleep(max(0, sec_to_wait + 1))

        # =====================================================================
        # [STAGE 2 & 3] 09:00 ~ {cutoff_str} 실시간 스캐너 발굴 & 돌파 매수/청산 관리
        # =====================================================================
        should_skip_trading = False
        now = datetime.now()
        if not is_overnight_strat and now.time() >= cutoff_t:
            env_mode = "vts" if self.mode == "mock" else "real"
            cfg = settings.switch_env(env_mode)
            check_broker = KisLiveBroker(mode=env_mode, cfg=cfg)
            try:
                check_broker.sync_account_state()
            except Exception as e:
                logger.debug(f"Could not query account state on late start: {e}")

            open_pos = check_broker.get_positions()
            if not open_pos:
                should_skip_trading = True
                console.print(Panel.fit(
                    f"[bold yellow]⏭️ [STAGE 2 & 3 건너뜀] 당일 아침 주도주 진입 시간(09:00~{cutoff_str})이 이미 종료되었습니다.[/bold yellow]\n"
                    f"[cyan]현재 시각:[/cyan] {now.strftime('%H:%M:%S')} (진입 마감 시각: {cutoff_str}) | [cyan]보유 잔고:[/cyan] 0종목\n"
                    f"[green]✓ 신규 매매를 건너뛰고 대사(Reconciliation) 단계로 이동합니다.[/green]",
                    border_style="yellow"
                ))

        if not should_skip_trading:
            console.print(Panel.fit(
                f"[bold cyan]⚡ [STAGE 2 & 3] 09:00 ~ {cutoff_str} KIS 조건검색 실시간 돌파 매매 가동[/bold cyan]\n"
                f"• [bold]진입 조건[/bold]: 당일 시초가 대비 돌파 (누적대금>=10억, MAE>-1.5%%)\n"
                f"• [bold]주문 방식[/bold]: 추격 매수 방지 칼라 캡(Collar Cap) 시장성 지정가\n"
                f"• [bold]손절 관리[/bold]: 기준봉 중심선/바닥 긴축 즉시 청산\n"
                f"• [bold]익절 관리[/bold]: 스마트 스텝업 트레일링 래칫\n"
                f"• [bold]세션 종료[/bold]: 당일 매매 완료 즉시 API 완전 차단 ➔ 15:20 장 마감 100% 전량 청산",
                border_style="cyan"
            ))

            env_mode = "vts" if self.mode == "mock" else "real"
            cfg = settings.switch_env(env_mode)
            broker = KisLiveBroker(mode=env_mode, cfg=cfg)
            clock = RealtimeClock()
            risk_manager = RiskManager()

            from core.ws_client import KISWebSocketClient
            ws_client = KISWebSocketClient(cfg)
            datafeed = KisRealtimeFeed(ws_client=ws_client, broker=broker)
            datafeed.execution_callback = broker.handle_execution_notice

            engine = TradingEngine(
                strategy=self.strategy,
                broker=broker,
                feed=datafeed,
                clock=clock,
                risk_manager=risk_manager,
            )
            if is_overnight_strat:
                from data_pipeline.realtime_scanner import RealtimeAnchorScanner
                scanner = RealtimeAnchorScanner(
                    client=broker.client,
                    datafeed=datafeed,
                    strategy=self.strategy,
                    context=engine.context,
                )
            else:
                scanner = RealtimeLeaderScanner(
                    client=broker.client,
                    datafeed=datafeed,
                    strategy=self.strategy,
                    context=engine.context,
                )

            try:
                broker.sync_account_state()
                # Proactively subscribe all held positions to WebSocket feed
                held_positions = broker.get_positions()
                for sym in held_positions.keys():
                    logger.info(f"Subscribing existing holding to live feed: {sym}")
                    datafeed.subscribe_symbol(sym)

                bal = broker.get_balance()
                if bal and bal.cash > 0 and hasattr(self.strategy, "sync_capital_with_balance"):
                    self.strategy.sync_capital_with_balance(bal.cash)
                scanner.start()
                datafeed.start()

                engine_thread = threading.Thread(target=engine.run, daemon=True, name="TradingEngineThread")
                engine_thread.start()

                console.print("[green]✓ 실시간 스캐너 및 웹소켓 호가 엔진이 가동되었습니다. 매매 완료를 실시간 감시합니다...[/green]")

                eod_liquidated_symbols = set()
                while True:
                    if single_cycle:
                        break
                    time.sleep(1.0)
                    curr_t = datetime.now().time()

                    # 15:20 KRX 장마감 동시호가 전량 청산 (데이트레이딩 전용, 오버나잇 전략은 청산 제외!)
                    if not is_overnight_strat and curr_t >= dtime(15, 20, 0):
                        open_pos = broker.get_positions()
                        if open_pos:
                            for sym, p in list(open_pos.items()):
                                if sym not in eod_liquidated_symbols:
                                    eod_liquidated_symbols.add(sym)
                                    console.print(f"\n[bold yellow]⚠️ [STAGE 3] 15:20 KRX 장마감 동시호가 청산 집행: {sym}[/bold yellow]")
                                    from engine.models import Order, OrderSide, OrderType
                                    broker.submit_order(Order(
                                        order_id=f"EOD_{sym}_{int(time.time())}",
                                        symbol=sym,
                                        side=OrderSide.SELL,
                                        order_type=OrderType.MARKET,
                                        quantity=p.quantity,
                                        tag="EOD_AUCTION_1520",
                                    ))

                    # Check daily trading completion
                    is_done = getattr(self.strategy, "is_daily_trading_completed", False)
                    if callable(is_done):
                        is_done = is_done()
                    open_pos = broker.get_positions()

                    if is_done and not open_pos:
                        console.print(
                            f"\n[bold green]🛑 [STAGE 4] 당일 목표 매매 완료 감지: 전량 청산 완료 및 쿼터 소진![/bold green]"
                        )
                        break

                    # Overnight close-betting clean exit after 15:30:10 call auction completion
                    if is_overnight_strat and curr_t >= dtime(15, 30, 10):
                        console.print(
                            f"\n[bold green]🛑 [STAGE 4] 15:30 KRX 장마감 동시호가 체결 완료: 오버나잇 잔고 확인 후 세션을 정상 종료합니다.[/bold green]"
                        )
                        break

                    if not is_overnight_strat and curr_t > cutoff_t and not open_pos:
                        console.print(
                            f"\n[bold yellow]🛑 [STAGE 4] 골든 윈도우 마감 ({cutoff_str}) 및 보유 잔고 0: 당일 매매 세션을 종료합니다.[/bold yellow]"
                        )
                        break

            except KeyboardInterrupt:
                console.print("\n[yellow]⚠️ 사용자 중단 신호(Ctrl+C) 수신: 당일 매매 세션을 종료합니다.[/yellow]")
            finally:
                # =====================================================================
                # [STAGE 4] 매매 완료 즉시 모든 API 통신 완전 차단 (Zero API Idle)
                # =====================================================================
                console.print("[bold yellow]🔒 [STAGE 4] 스캐너 정지, 웹소켓 연결 해제 및 잔여 주문 정리 중...[/bold yellow]")
                try:
                    broker.cancel_pending_orders()
                    scanner.stop()
                    datafeed.stop()
                except Exception as e:
                    logger.debug(f"Error stopping feeds/cancelling orders: {e}")

                console.print("[bold green]✓ 모든 실시간 API 호출이 완전히 차단되었습니다 (Zero API Idle 달성).[/bold green]\n")

        # =====================================================================
        # 당일 매매 백테스트 vs 실전 자동 대사 (Automated Daily Reconciliation)
        # =====================================================================
        try:
            from engine.reconciliation import reconcile_daily_trading, display_reconciliation_table
            console.print("\n[bold cyan]📊 당일 매매 백테스트 vs 실전 자동 대사 (Reconciliation) 분석을 수행합니다...[/bold cyan]")
            rec_res = reconcile_daily_trading(
                target_date=self.target_date,
                scanner_name=self.scanner_name,
                entry_name=self.entry_name,
                exit_name=self.exit_name,
            )
            display_reconciliation_table(rec_res)
        except Exception as re:
            logger.debug(f"Daily reconciliation skipped or failed: {re}")

        console.print(Panel.fit(
            f"[bold green]✨ [ROUTINE COMPLETE] {self.target_date} 일일 퀀트 트레이딩 세션이 안전하게 종료되었습니다![/bold green]\n"
            f"[dim]• 추가 대사가 필요한 경우: [bold yellow]python main.py reconcile --date {self.target_date}[/bold yellow][/dim]",
            border_style="green"
        ))


def main():
    parser = argparse.ArgumentParser(description="Korea Market v3 - Full Day Trading Routine Orchestrator")
    parser.add_argument("--mode", type=str, choices=["mock", "live"], default="mock", help="Trading mode: mock (VTS) or live (REAL)")
    parser.add_argument("--scanner", type=str, default="champion_b", help="Scanner module name (default: champion_b)")
    parser.add_argument("--entry", type=str, default="anchor_close", help="Entry module name (default: anchor_close)")
    parser.add_argument("--exit", type=str, default="anchor_morning", help="Exit module name (default: anchor_morning)")
    parser.add_argument("--date", type=str, default=datetime.now().strftime("%Y%m%d"), help="Target date YYYYMMDD")
    parser.add_argument("--capital", type=float, default=10_000_000.0, help="Initial trading capital in KRW")
    parser.add_argument("--nowait", action="store_true", help="Skip waiting for 09:00 market open")
    parser.add_argument("--yes", action="store_true", help="Confirm real money execution for live mode")
    args = parser.parse_args()

    if args.mode == "live" and not args.yes:
        console.print(Panel.fit(
            "[bold red]⚠️ 경고: 실전투자 (REAL) 모드입니다.[/bold red]\n"
            "실제 자금으로 한국투자증권 실주문이 집행됩니다.\n"
            "실행하시려면 --yes 플래그를 추가해주세요.\n"
            "예: python -m engine.daily_routine --mode live --yes",
            border_style="red"
        ))
        sys.exit(1)

    orchestrator = DailyRoutineOrchestrator(
        mode=args.mode,
        target_date=args.date,
        scanner_name=args.scanner,
        entry_name=args.entry,
        exit_name=args.exit,
        capital=args.capital,
    )
    orchestrator.run(
        wait_for_market_open=not args.nowait,
    )


if __name__ == "__main__":
    main()
