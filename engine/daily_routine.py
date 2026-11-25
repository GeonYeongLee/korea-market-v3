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
from data_pipeline.realtime_scanner import RealtimeLeaderScanner
from strategy.registry import build_strategy

logger = logging.getLogger("kis.engine.daily_routine")
console = Console(force_terminal=True, legacy_windows=False)


class DailyRoutineOrchestrator:
    """End-to-End Orchestrator for the Complete Daily Trading Routine."""

    def __init__(
        self,
        mode: str = "mock",
        target_date: Optional[str] = None,
        scanner_name: str = "all_weather",
        entry_name: str = "all_weather",
        exit_name: str = "all_weather",
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

    def run(self, wait_for_market_open: bool = True) -> None:
        """Executes the complete 4-stage daily trading routine."""
        date_iso = (
            f"{self.target_date[:4]}-{self.target_date[4:6]}-{self.target_date[6:]}"
            if len(self.target_date) == 8
            else self.target_date
        )
        target_date_obj = (
            datetime.strptime(self.target_date, "%Y%m%d").date()
            if len(self.target_date) == 8
            else date.fromisoformat(self.target_date)
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
        broker = KisLiveBroker(mode=env_mode, cfg=cfg)
        synced = False
        for attempt in range(1, 4):
            try:
                broker.sync_account_state()
                bal = broker.get_balance()
                if bal and bal.cash > 0:
                    if bal.cash < self.capital:
                        console.print(
                            f"[bold yellow]💡 [예수금 비례 1/N 배분] 계좌 예수금({bal.cash:,.0f}원)이 기준 원금({self.capital:,.0f}원) 미만입니다.[/bold yellow]\n"
                            f"   ➜ 가용 예수금을 기준으로 당일 슬롯에 1/N 배분하여 안전 매매합니다."
                        )
                    if hasattr(self.strategy, "sync_capital_with_balance"):
                        self.strategy.sync_capital_with_balance(bal.cash)
                logger.info("Pre-market account state synced successfully.")
                synced = True
                break
            except Exception as e:
                logger.warning(f"Pre-market account state sync attempt {attempt}/3 failed: {e}")
                if attempt < 3:
                    time.sleep(1.0)
        if not synced:
            logger.warning("Could not pre-sync account state after 3 attempts.")

        try:
            import core.krx_symbols
            core.krx_symbols.get_krx_name_map()
            logger.info("KRX symbol map loaded successfully.")
        except Exception as e:
            logger.debug(f"Could not pre-load KRX symbol map: {e}")

        now = datetime.now()
        market_open_time = dtime(9, 0, 0)
        if wait_for_market_open and now.time() < market_open_time:
            sec_to_wait = (datetime.combine(now.date(), market_open_time) - now).total_seconds()
            if sec_to_wait > 0:
                console.print(f"[yellow]⏳ [STAGE 1] 정규장 시작(09:00)까지 대기합니다 ({sec_to_wait:.0f}초 남음)...[/yellow]")
                time.sleep(max(0, sec_to_wait + 1))

        # =====================================================================
        # [STAGE 1.5] 09:00:01 실시간 시장 레짐 산출 & 라이프사이클 100% 확정
        # =====================================================================
        console.print("[cyan]🔍 [09:00:01] KOSPI/KOSDAQ 실시간 갭률 및 시장 레짐(20MA 괴리율/동적 갭) 산출 중...[/cyan]")
        regime_data = broker.client.get_kospi_gap_and_market_regime()
        ks_gap = float(regime_data.get("ks_gap_pct", regime_data.get("kospi_gap_pct", 0.0)))
        kq_gap = float(regime_data.get("kq_gap_pct", regime_data.get("kosdaq_gap_pct", 0.0)))
        ks_trend = float(regime_data.get("ks_trend", 0.0))
        kq_trend = float(regime_data.get("kq_trend", 0.0))
        is_bear = bool(regime_data.get("is_bear_market", False))
        dynamic_max_gap = float(regime_data.get("dynamic_max_gap", 0.75))
        gap_std = float(regime_data.get("gap_std", 0.65))

        from core.market_regime import get_default_market_regime_provider
        from core.dynamic_features import get_default_dynamic_feature_provider

        regime_provider = getattr(self.strategy, "regime_provider", None) or get_default_market_regime_provider()
        dyn_provider = getattr(self.strategy, "dynamic_feature_provider", None) or get_default_dynamic_feature_provider()

        regime_provider.register_live_regime(current_date=target_date_obj, ks_gap=ks_gap, kq_gap=kq_gap)
        dyn_provider.register_live_index_features(
            current_date=target_date_obj,
            ks_trend=ks_trend,
            kq_trend=kq_trend,
            is_bear_market=is_bear,
            dynamic_max_gap=dynamic_max_gap,
            market_gap_std20=gap_std,
        )

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

        # 09:00:01 스캐너 가동 및 틱 수신 전에 전략 라이프사이클 명시적 호출
        self.strategy.on_day_open(engine.context, target_date_obj)
        engine._current_date = target_date_obj

        max_slots = self.strategy.max_daily_positions
        slot_notional = getattr(self.strategy, "current_slot_notional", 0.0)

        console.print(Panel.fit(
            f"[bold cyan]🧭 [09:00:01 MARKET REGIME 확정][/bold cyan]\n"
            f"• [bold]KOSPI 갭률[/bold]: {ks_gap:+.2f}% | [bold]KOSDAQ 갭률[/bold]: {kq_gap:+.2f}%\n"
            f"• [bold]20MA 괴리율[/bold]: KOSPI {ks_trend:+.2%}, KOSDAQ {kq_trend:+.2%}\n"
            f"• [bold]하락장 방어(Bear Market)[/bold]: {'[bold red]ACTIVE (하락장 감지)[/bold red]' if is_bear else '[green]INACTIVE (정상 시장)[/green]'}\n"
            f"• [bold]동적 갭 한도[/bold]: {dynamic_max_gap:.2f}% (지수 20일 변동성: {gap_std:.2f}%)\n"
            f"• [bold]당일 운용 슬롯[/bold]: [bold yellow]{max_slots}슬롯[/bold yellow] ({slot_notional:,.0f}원/슬롯)",
            border_style="magenta" if max_slots == 0 else "cyan",
        ))
        logger.info(
            f"09:00:01 Regime finalized: KS_gap={ks_gap:+.2f}%, KQ_gap={kq_gap:+.2f}%, "
            f"KS_trend={ks_trend:+.2%}, KQ_trend={kq_trend:+.2%}, bear={is_bear}, "
            f"max_gap={dynamic_max_gap:.2f}%, slots={max_slots}, slot_notional={slot_notional:,.0f}"
        )

        # =====================================================================
        # [STAGE 2 & 3] 09:00 ~ {cutoff_str} 실시간 스캐너 발굴 & 돌파 매수/청산 관리
        # =====================================================================
        should_skip_trading = False
        now = datetime.now()
        open_pos = broker.get_positions()

        if now.time() >= cutoff_t and not open_pos:
            should_skip_trading = True
            console.print(Panel.fit(
                f"[bold yellow]⏭️ [STAGE 2 & 3 건너뜀] 당일 아침 주도주 진입 시간(09:00~{cutoff_str})이 이미 종료되었습니다.[/bold yellow]\n"
                f"[cyan]현재 시각:[/cyan] {now.strftime('%H:%M:%S')} (진입 마감 시각: {cutoff_str}) | [cyan]보유 잔고:[/cyan] 0종목\n"
                f"[green]✓ 신규 매매를 종료하고 일일 루틴 마감 단계로 이동합니다.[/green]",
                border_style="yellow"
            ))
        elif max_slots == 0 and not open_pos:
            should_skip_trading = True
            console.print(Panel.fit(
                f"[bold red]🛡️ [TRADING BLOCKED] 시장 위험 또는 하락장 방어 필터 발동으로 당일 매매가 0슬롯으로 차단되었습니다.[/bold red]\n"
                f"[yellow]• 사유: 하락장 레짐(MA20 이탈) 또는 극단적 갭(동적 갭 한도 초과/극단적 갭하락)[/yellow]\n"
                f"[green]✓ 불필요한 스캐너 폴링을 건너뛰고 자금을 100% 안전 보존(Safe Observation Mode)합니다.[/green]",
                border_style="red"
            ))

        if not should_skip_trading:
            mae_disp = getattr(getattr(self.strategy, "scanner", None), "max_mae_pct", -0.015) * 100
            is_bear = getattr(getattr(self.strategy, "scanner", None), "is_bear_market", False)
            sl_desc = "하드 스탑(-1.5%) & 이익 보존 래칫" if is_bear else "기준봉 중심선/바닥 긴축 즉시 청산"
            tp_desc = "+2.5% 1차 익절(+1.0% 락) ➔ 1.5% 트레일링" if is_bear else "스마트 스텝업 트레일링 래칫"
            eod_t = getattr(getattr(self.strategy, "exit_policy", None), "hard_timeout", None)
            eod_str = eod_t.strftime("%H:%M") if eod_t else "15:20"
            console.print(Panel.fit(
                f"[bold cyan]⚡ [STAGE 2 & 3] 09:00 ~ {cutoff_str} KIS 조건검색 실시간 돌파 매매 가동[/bold cyan]\n"
                f"• [bold]진입 조건[/bold]: 당일 시초가 대비 돌파 (동적 누적대금>=10억, MAE>{mae_disp:+.1f}%)\n"
                f"• [bold]주문 방식[/bold]: 추격 매수 방지 칼라 캡(Collar Cap) 시장성 지정가\n"
                f"• [bold]손절 관리[/bold]: {sl_desc}\n"
                f"• [bold]익절 관리[/bold]: {tp_desc}\n"
                f"• [bold]세션 종료[/bold]: 당일 매매 완료 즉시 API 완전 차단 ➔ {eod_str} 장 마감 100% 전량 청산",
                border_style="cyan"
            ))

            scanner = RealtimeLeaderScanner(
                client=broker.client,
                datafeed=datafeed,
                strategy=self.strategy,
                context=engine.context,
            )

            try:
                datafeed.start()
                time.sleep(1.0)
                if max_slots > 0:
                    scanner.start()

                engine_thread = threading.Thread(target=engine.run, daemon=True, name="TradingEngineThread")
                engine_thread.start()

                console.print("[green]✓ 실시간 스캐너 및 웹소켓 호가 엔진이 가동되었습니다. 매매 완료를 실시간 감시합니다...[/green]")

                last_eod_attempt: Dict[str, float] = {}
                loop_start_ts: float = time.time()
                last_watchdog_reboot: float = 0.0

                while True:
                    time.sleep(1.0)
                    curr_t = datetime.now().time()
                    now_ts = time.time()

                    # Feed Watchdog (09:00:15 ~ 15:20)
                    if curr_t >= dtime(9, 0, 15) and curr_t < dtime(15, 20, 0):
                        ws_c = getattr(datafeed, "ws_client", None)
                        ws_state = getattr(ws_c, "connection_state", "CONNECTED") if ws_c else "CONNECTED"
                        active_syms = getattr(datafeed, "active_symbols", [])
                        last_tick = getattr(datafeed, "last_tick_time", 0.0)

                        silence_sec = (now_ts - last_tick) if last_tick > 0 else (now_ts - loop_start_ts)
                        is_stalled = len(active_syms) > 0 and silence_sec > 30.0
                        is_auth_failed = ws_state == "AUTH_FAILED"

                        if (is_stalled or is_auth_failed) and (now_ts - last_watchdog_reboot >= 30.0):
                            last_watchdog_reboot = now_ts
                            diag_reason = (
                                "AUTH_FAILED (인증 실패/approval_key 만료)"
                                if is_auth_failed
                                else f"시세 틱 수신 중단 ({silence_sec:.1f}초 침묵, 구독종목 {len(active_syms)}개)"
                            )
                            console.print(Panel.fit(
                                f"[bold red]🚨 [FEED WATCHDOG] 실시간 시세 피드 장애 감지![/bold red]\n"
                                f"• [yellow]원인[/yellow]: {diag_reason}\n"
                                f"• [cyan]조치[/cyan]: approval_key 무효화 및 WebSocket 하드 재기동(Reboot) 수행\n"
                                f"• [white]구독 종목 복구[/white]: {', '.join(active_syms) if active_syms else '없음'}",
                                border_style="red"
                            ))
                            logger.error(f"[FeedWatchdog] Triggering feed reboot: {diag_reason}")
                            if hasattr(datafeed, "reboot"):
                                datafeed.reboot()
                            elif hasattr(datafeed, "hard_reconnect"):
                                datafeed.hard_reconnect()

                    # 15:20 KRX 장마감 동시호가 전량 청산 (오버나잇 0% 불변량, 15:28:00까지 지속 재시도)
                    if curr_t >= dtime(15, 20, 0) and curr_t <= dtime(15, 28, 0):
                        open_pos = broker.get_positions()
                        if open_pos:
                            from engine.models import Order, OrderSide, OrderType
                            for sym, p in list(open_pos.items()):
                                if p.quantity > 0:
                                    last_att = last_eod_attempt.get(sym, 0.0)
                                    if time.time() - last_att >= 2.0:
                                        last_eod_attempt[sym] = time.time()
                                        console.print(
                                            f"\n[bold yellow]⚠️ [STAGE 3] 15:20 KRX 장마감 동시호가 청산 집행: {sym} ({p.quantity}주)[/bold yellow]"
                                        )
                                        # First cancel resting orders to avoid '매도가능수량 부족'
                                        broker.cancel_pending_orders(sym)
                                        time.sleep(0.5)
                                        curr_p = broker.get_position(sym)
                                        if curr_p and curr_p.quantity > 0:
                                            broker.submit_order(Order(
                                                order_id=f"EOD_{sym}_{int(time.time())}",
                                                symbol=sym,
                                                side=OrderSide.SELL,
                                                order_type=OrderType.MARKET,
                                                quantity=curr_p.quantity,
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

                    if curr_t > cutoff_t and not open_pos:
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
                    if max_slots > 0:
                        scanner.stop()
                    datafeed.stop()
                except Exception as e:
                    logger.debug(f"Error stopping feeds/cancelling orders: {e}")

                console.print("[bold green]✓ 모든 실시간 API 호출이 완전히 차단되었습니다 (Zero API Idle 달성).[/bold green]\n")

        console.print(Panel.fit(
            f"[bold green]✨ [ROUTINE COMPLETE] {self.target_date} 일일 퀀트 트레이딩 세션이 안전하게 종료되었습니다![/bold green]",
            border_style="green"
        ))


def main():
    parser = argparse.ArgumentParser(description="Korea Market v3 - Full Day Trading Routine Orchestrator")
    parser.add_argument("--mode", type=str, choices=["mock", "live"], default="mock", help="Trading mode: mock (VTS) or live (REAL)")
    parser.add_argument("--scanner", type=str, default="all_weather", help="Scanner module name")
    parser.add_argument("--entry", type=str, default="all_weather", help="Entry module name")
    parser.add_argument("--exit", type=str, default="all_weather", help="Exit module name")
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
