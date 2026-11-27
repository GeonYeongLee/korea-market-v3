"""
REAL-Live CLI Orchestrator for Korea Market v4.
Wires `KisLiveClock`, `KisRegimeDataAdapter`, `KisConditionSearchAdapter`,
`KisRealtimeFeed`, `KisLiveBrokerAdapter`, `DailyRiskGuard`, and
`UnifiedTradingRunner` for `python main.py live --v4 --yes`.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel

from config.settings import settings
from core.rest_client import KISRestClient
from v4.adapters.kis import (
    KisConditionSearchAdapter,
    KisLiveBrokerAdapter,
    KisLiveClock,
    KisRealtimeFeed,
    KisRegimeDataAdapter,
)
from v4.audit_sink import JsonlAuditSink
from v4.risk import DailyRiskGuard
from v4.runner import UnifiedTradingRunner
from v4.strategy.factory import build_v4_strategy

logger = logging.getLogger("korea_market.v4.live_cli")


async def _wait_until_preopen(
    clock: KisLiveClock,
    market_open_time: time,
    out: Console,
) -> None:
    """Sleeps until 5 minutes before `market_open_time` if started early."""
    now_dt = clock.now()
    open_dt = datetime.combine(now_dt.date(), market_open_time)
    wake_dt = open_dt - timedelta(minutes=5)
    if now_dt >= wake_dt:
        return
    wait_sec = (wake_dt - now_dt).total_seconds()
    out.print(
        f"[cyan]⏳ Waiting {wait_sec:.0f}s until pre-open ({wake_dt.strftime('%H:%M:%S')})...[/cyan]"
    )
    while True:
        rem = (wake_dt - clock.now()).total_seconds()
        if rem <= 0.0:
            break
        await clock.sleep(min(rem, 5.0))


async def run_v4_live_async(
    args: argparse.Namespace,
    console: Optional[Console] = None,
) -> None:
    """Initializes KIS v4 REAL adapters, runs pre-market sync, and executes `UnifiedTradingRunner`."""
    out = console or Console()
    cfg = settings.switch_env("real")
    rest_client = KISRestClient(cfg)
    clock = KisLiveClock()
    capital = float(getattr(args, "capital", 10_000_000.0))

    regime = KisRegimeDataAdapter(client=rest_client)
    broker = KisLiveBrokerAdapter(
        cfg=cfg,
        client=rest_client,
        clock=clock,
        initial_capital=capital,
        venue=regime.venue,
    )
    cond = KisConditionSearchAdapter(client=rest_client, venue=regime.venue)
    feed = KisRealtimeFeed(
        cfg=cfg,
        broker=broker,
        clock=clock,
        venue=regime.venue,
    )
    risk_guard = DailyRiskGuard()
    strategy = build_v4_strategy(args, execution_mode="pessimistic")

    # Pre-market state & regime synchronization
    open_events = await broker.sync_open_orders()
    today = clock.now().date()
    index_regime = await regime.get_index_regime(today)
    cond.set_market_open_time(index_regime.market_open_time)
    feed.market_close_time = index_regime.market_close_time

    protected_syms = set(broker.get_positions().keys()) | {
        ev.symbol for ev in open_events if ev.symbol
    }
    for sym in sorted(protected_syms):
        await feed.subscribe(sym)

    out.print(
        Panel(
            f"[bold cyan]Korea Market v4 Unified Live Engine[/bold cyan]\n"
            f"• Environment : [yellow]REAL[/yellow] | Strategy: [yellow]{strategy.name}[/yellow]\n"
            f"• Capital     : [white]{capital:,.0f} KRW[/white] | Open Orders: [white]{len(open_events)}[/white] | Holdings: [white]{len(broker.get_positions())}[/white]\n"
            f"• Session     : [white]{index_regime.market_open_time.strftime('%H:%M')} ~ {index_regime.market_close_time.strftime('%H:%M')}[/white]\n"
            f"• Index Gap   : KOSPI {index_regime.ks_gap_pct:+.2f}% | KOSDAQ {index_regime.kq_gap_pct:+.2f}%",
            title="[bold green]🚀 v4 Pre-Market Sync Complete[/bold green]",
            expand=False,
        )
    )

    if getattr(args, "dry_run", False):
        out.print("[yellow]Dry-run mode active (--dry-run); skipping event loop.[/yellow]")
        return

    if not getattr(args, "nowait", False):
        await _wait_until_preopen(clock, index_regime.market_open_time, out)

    audit_sink = JsonlAuditSink(Path(cfg.logs_dir) / f"v4_audit_live_{today:%Y%m%d}.jsonl")
    runner = UnifiedTradingRunner(
        strategy=strategy,
        clock=clock,
        regime_data=regime,
        condition_search=cond,
        feed=feed,
        broker=broker,
        risk_guard=risk_guard,
        execution_mode="pessimistic",
        is_live_tick_driven=True,
        audit_sink=audit_sink,
    )
    try:
        trades = await runner.run(start_date=today, end_date=today)
        out.print(f"[bold green]✓ Session completed. Total closed trades: {len(trades)}[/bold green]")
    finally:
        audit_sink.close()
        await feed.stop()


def cmd_v4_live(
    args: argparse.Namespace,
    console: Optional[Console] = None,
) -> None:
    """Entrypoint for `python main.py live --v4 --yes` (REAL-only)."""
    out = console or Console()
    if not getattr(args, "yes", False):
        out.print(
            Panel(
                "[bold red]🚨 Safety Guard: Live real cash trading requires the '--yes' confirmation flag.[/bold red]\n"
                "Execution aborted to prevent accidental real-money orders.\n\n"
                "To confirm live real trading, run:\n"
                "  [bold yellow]python main.py live --v4 --yes[/bold yellow]",
                title="[bold red]Live Trading Refused[/bold red]",
                border_style="red",
            )
        )
        sys.exit(1)

    try:
        asyncio.run(run_v4_live_async(args, console=out))
    except KeyboardInterrupt:
        out.print("\n[yellow]Interrupted by user; live session stopped cleanly.[/yellow]")
