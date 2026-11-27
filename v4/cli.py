"""
Unified CLI Backtest Orchestrator for Korea Market v4.
Wires `ParquetRegimeDataAdapter`, `BacktestConditionSearchAdapter`,
`BacktestRealtimeFeed`, `BacktestBrokerAdapter`, `DailyRiskGuard`,
and `UnifiedTradingRunner` with `EngineMetrics` / `TradeReporter` output.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, time
from pathlib import Path
from typing import Optional

from rich.console import Console

from config.settings import settings
from engine.metrics import PerformanceSummary, calculate_performance
from v4.adapters.backtest import (
    BacktestBrokerAdapter, BacktestClock, BacktestConditionSearchAdapter, BacktestRealtimeFeed,
    CurrentDayStore, ParquetRegimeDataAdapter, list_trading_dates, parse_date_arg, resolve_data_dir,
)
from v4.audit_sink import JsonlAuditSink, NullAuditSink
from v4.cli_report import render_dual_report, render_init_banner, render_single_report
from v4.interfaces import IAuditSink
from v4.models import ExecutionMode
from v4.risk import DailyRiskGuard
from v4.runner import UnifiedTradingRunner
from v4.strategy.factory import build_v4_strategy


def build_v4_runner(
    args: argparse.Namespace,
    execution_mode: ExecutionMode,
    data_dir: str,
    day_store: CurrentDayStore,
    regime_adapter: ParquetRegimeDataAdapter,
    audit_sink: Optional[IAuditSink] = None,
) -> UnifiedTradingRunner:
    """Wires an isolated `UnifiedTradingRunner` sharing `day_store` and `regime_adapter`."""
    capital = float(getattr(args, "capital", 10_000_000.0))
    strict_parity = bool(getattr(args, "strict_v3_parity", False))
    strategy = build_v4_strategy(args, execution_mode)
    clock = BacktestClock()
    broker = BacktestBrokerAdapter(initial_capital=capital, execution_mode=execution_mode)
    risk_guard = DailyRiskGuard()

    cond = BacktestConditionSearchAdapter(
        regime_data=regime_adapter,
        execution_mode=execution_mode,
        min_gap_pct=strategy.scanner.min_gap_pct,
        max_gap_pct=strategy.scanner.max_gap_pct,
        max_mae_pct=strategy.scanner.max_mae_pct,
        min_surge_pct=strategy.scanner.min_body_pct,
        data_dir=data_dir,
        day_store=day_store,
        optimistic_prior_mae=not strict_parity,
    )

    runner_ref: list[UnifiedTradingRunner] = []

    def _protected_symbols() -> set[str]:
        syms = set(broker.positions.keys())
        syms.update(o.symbol for o in broker.pending_orders.values())
        return syms

    def _should_continue_day(m_dt: datetime) -> bool:
        if not runner_ref or runner_ref[0]._current_date != m_dt.date():
            return True
        if broker.positions or broker.pending_orders:
            return True
        if strategy.is_daily_trading_completed or m_dt.time() > strategy.cutoff_time:
            return False
        return True

    feed = BacktestRealtimeFeed(
        data_dir=data_dir,
        start_date=getattr(args, "start", None),
        end_date=getattr(args, "end", None),
        condition_emulator=cond,
        day_store=day_store,
        protected_symbols_fn=_protected_symbols,
        should_continue_day_fn=_should_continue_day,
        scan_cutoff_time=time(9, 20),
        shift_scan_cutoff=not strict_parity,
    )

    runner = UnifiedTradingRunner(
        strategy=strategy,
        clock=clock,
        regime_data=regime_adapter,
        condition_search=cond,
        feed=feed,
        broker=broker,
        risk_guard=risk_guard,
        execution_mode=execution_mode,
        audit_sink=audit_sink,
    )
    runner_ref.append(runner)
    return runner


async def run_v4_backtest_async(
    args: argparse.Namespace,
    execution_mode: ExecutionMode,
    data_dir: str,
    day_store: Optional[CurrentDayStore] = None,
    regime_adapter: Optional[ParquetRegimeDataAdapter] = None,
    audit_sink: Optional[IAuditSink] = None,
) -> PerformanceSummary:
    """Executes one deterministic async `UnifiedTradingRunner` backtest pass."""
    capital = float(getattr(args, "capital", 10_000_000.0))
    store = day_store or CurrentDayStore()
    regime = regime_adapter or ParquetRegimeDataAdapter(
        data_dir=data_dir,
        start_date=getattr(args, "start", None),
        end_date=getattr(args, "end", None),
        day_store=store,
    )
    runner = build_v4_runner(args, execution_mode, data_dir, store, regime, audit_sink=audit_sink)
    s_dt = parse_date_arg(getattr(args, "start", None))
    e_dt = parse_date_arg(getattr(args, "end", None))
    trades = await runner.run(start_date=s_dt, end_date=e_dt)
    return calculate_performance(trades, initial_capital=capital)


async def run_v4_dual_backtest_async(
    args: argparse.Namespace,
    data_dir: str,
    day_store: CurrentDayStore,
    regime_adapter: ParquetRegimeDataAdapter,
    pess_audit_sink: Optional[IAuditSink] = None,
    opti_audit_sink: Optional[IAuditSink] = None,
) -> tuple[PerformanceSummary, PerformanceSummary]:
    """Executes 1-pass day-by-day dual-bounds backtest sharing each day's `DayBarIndex`."""
    capital = float(getattr(args, "capital", 10_000_000.0))
    pess_runner = build_v4_runner(args, "pessimistic", data_dir, day_store, regime_adapter, audit_sink=pess_audit_sink)
    opti_runner = build_v4_runner(args, "optimistic", data_dir, day_store, regime_adapter, audit_sink=opti_audit_sink)
    s_dt = parse_date_arg(getattr(args, "start", None))
    e_dt = parse_date_arg(getattr(args, "end", None))
    for d in list_trading_dates(data_dir, s_dt, e_dt):
        await pess_runner.run(start_date=d, end_date=d)
        await opti_runner.run(start_date=d, end_date=d)
    pess = calculate_performance(pess_runner.broker.get_completed_trades(), initial_capital=capital)
    opti = calculate_performance(opti_runner.broker.get_completed_trades(), initial_capital=capital)
    return pess, opti


def run_v4_backtest(
    args: argparse.Namespace,
    execution_mode: ExecutionMode,
    data_dir: str,
    day_store: Optional[CurrentDayStore] = None,
    regime_adapter: Optional[ParquetRegimeDataAdapter] = None,
    audit_sink: Optional[IAuditSink] = None,
) -> PerformanceSummary:
    """Executes one deterministic `UnifiedTradingRunner` backtest pass."""
    return asyncio.run(
        run_v4_backtest_async(
            args,
            execution_mode=execution_mode,
            data_dir=data_dir,
            day_store=day_store,
            regime_adapter=regime_adapter,
            audit_sink=audit_sink,
        )
    )


def cmd_v4_backtest(args: argparse.Namespace, console: Optional[Console] = None) -> None:
    """Runs unified v4 backtest (pessimistic | optimistic | dual) with Rich & CSV output."""
    out = console or Console()
    data_dir = resolve_data_dir(getattr(args, "data_dir", None))
    capital = float(getattr(args, "capital", 10_000_000.0))
    render_init_banner(out, args, data_dir, capital)

    shared_store = CurrentDayStore()
    shared_regime = ParquetRegimeDataAdapter(
        data_dir=data_dir, start_date=getattr(args, "start", None), end_date=getattr(args, "end", None), day_store=shared_store
    )
    tag = f"{getattr(args, 'start', 'all')}_{getattr(args, 'end', 'all')}".replace("-", "")
    use_audit = bool(getattr(args, "audit_log", False))

    def _make_sink(suffix: str) -> IAuditSink:
        if not use_audit:
            return NullAuditSink()
        return JsonlAuditSink(Path(settings.logs_dir) / f"v4_audit_backtest_{tag}_{suffix}.jsonl")

    if args.mode in ("pessimistic", "optimistic"):
        audit_sink = _make_sink(args.mode)
        try:
            summary = run_v4_backtest(
                args, execution_mode=args.mode, data_dir=data_dir,
                day_store=shared_store, regime_adapter=shared_regime, audit_sink=audit_sink,
            )
            render_single_report(out, args, summary)
        finally:
            audit_sink.close()
        return

    pess_sink, opti_sink = _make_sink("pessimistic"), _make_sink("optimistic")
    try:
        pess, opti = asyncio.run(
            run_v4_dual_backtest_async(
                args, data_dir, shared_store, shared_regime,
                pess_audit_sink=pess_sink, opti_audit_sink=opti_sink,
            )
        )
        render_dual_report(out, args, pess, opti, capital)
    finally:
        pess_sink.close()
        opti_sink.close()
