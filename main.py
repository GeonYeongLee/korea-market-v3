"""
Korea Market v3: Modular Systematic Trading Platform.
CLI Entry point for pure 1-minute bar backtesting, contract verification audit, and strategy assembly.
"""
from __future__ import annotations

import os
import sys

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import argparse
from typing import Optional
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from core.logging import setup_logger
from engine.clock import SimulatedClock
from engine.broker.backtest import BacktestBroker
from engine.datafeed.historical import HistoricalDataFeed
from engine.risk.risk_manager import RiskManager
from engine.engine import TradingEngine
from engine.metrics import (
    PerformanceSummary,
    render_dual_bounds_table,
    render_exit_distribution_table,
)
from engine.trade_reporter import (
    export_trades_to_csv,
    render_trades_table,
    save_backtest_reports,
)
from strategy.registry import build_strategy, SCANNERS, ENTRIES, EXITS
from audit.universal_auditor import UniversalStrategyAuditor

console = Console()
logger = setup_logger()


def resolve_data_dir(custom_path: Optional[str] = None) -> str:
    """Finds the available 1-minute partitioned dataset directory with fallback."""
    candidates = [
        custom_path,
        "data/dataset_2y/daily",
        os.path.join("..", "korea-market_v2", "data", "dataset_2y", "daily"),
    ]
    for p in candidates:
        if p and os.path.isdir(p):
            return p
    return "data/dataset_2y/daily"


def run_single_backtest(
    args: argparse.Namespace,
    ambiguity_mode: str,
    data_dir: str,
) -> PerformanceSummary:
    """Runs a single pure 1-minute bar backtest run under specified ambiguity mode."""
    dynamic_slots = args.slots is None
    max_daily_positions = args.slots if args.slots is not None else 1

    scanner_kwargs = {}
    if hasattr(args, "min_gap") and args.min_gap is not None:
        scanner_kwargs["min_gap_pct"] = args.min_gap
    if hasattr(args, "max_gap") and args.max_gap is not None:
        scanner_kwargs["max_gap_pct"] = args.max_gap
    if hasattr(args, "max_mae") and args.max_mae is not None:
        scanner_kwargs["max_mae_pct"] = args.max_mae
    if hasattr(args, "priority") and args.priority is not None and args.scanner == "open_window":
        scanner_kwargs["race_priority_mode"] = args.priority
        scanner_kwargs["priority_policy"] = args.priority

    entry_kwargs = {}
    if args.entry == "open_window":
        if hasattr(args, "entry_start") and args.entry_start is not None:
            entry_kwargs["start_time_min"] = args.entry_start
        if hasattr(args, "entry_end") and args.entry_end is not None:
            entry_kwargs["end_time_min"] = args.entry_end
        if hasattr(args, "entry_style") and args.entry_style is not None:
            entry_kwargs["entry_style"] = args.entry_style

    exit_kwargs = {}
    if hasattr(args, "trailing_buffer") and args.trailing_buffer is not None:
        exit_kwargs["trailing_buffer_pct"] = args.trailing_buffer
    if args.exit == "trailing_lock":
        if hasattr(args, "tp1") and args.tp1 is not None:
            exit_kwargs["tp1_target_pct"] = args.tp1
        if hasattr(args, "sl") and args.sl is not None:
            exit_kwargs["hard_stop_pct"] = args.sl
        if hasattr(args, "trailing_offset") and args.trailing_offset is not None:
            exit_kwargs["trailing_offset_pct"] = args.trailing_offset

    strategy = build_strategy(
        scanner_name=args.scanner,
        entry_name=args.entry,
        exit_name=args.exit,
        scanner_kwargs=scanner_kwargs,
        entry_kwargs=entry_kwargs,
        exit_kwargs=exit_kwargs,
        max_daily_positions=max_daily_positions,
        total_capital=args.capital,
        dynamic_slots=dynamic_slots,
        ambiguity_mode=ambiguity_mode,
        dynamic=getattr(args, "dynamic", True),
        regime_mode=getattr(args, "regime_mode", "all"),
    )
    broker = BacktestBroker(initial_capital=args.capital)
    clock = SimulatedClock()
    feed = HistoricalDataFeed(
        data_dir=data_dir,
        start_date=args.start,
        end_date=args.end,
    )
    risk_manager = RiskManager()

    engine = TradingEngine(
        strategy=strategy,
        broker=broker,
        feed=feed,
        clock=clock,
        risk_manager=risk_manager,
    )
    return engine.run()


def cmd_strategies() -> None:
    """Lists available plug-and-play components."""
    table = Table(title="🧩 Available Strategy Lego-Blocks", show_header=True, header_style="bold magenta")
    table.add_column("Type", style="bold cyan")
    table.add_column("Component Name", style="bold white")
    table.add_column("Class", style="dim")

    for k, v in SCANNERS.items():
        table.add_row("Scanner (발굴)", k, v.__name__)
    for k, v in ENTRIES.items():
        table.add_row("Entry (진입)", k, v.__name__)
    for k, v in EXITS.items():
        table.add_row("Exit (청산)", k, v.__name__)

    console.print(table)


def cmd_backtest(args: argparse.Namespace) -> None:
    """Runs deterministic backtest with assembled CompositeStrategy."""
    data_dir = resolve_data_dir(args.data_dir)

    slots_info = (
        "Dynamic (Dual-Index Regime 2 vs 5 slots)"
        if args.slots is None
        else f"Fixed ({args.slots} slots)"
    )

    console.print(Panel(
        f"[bold cyan]Korea Market v3 Backtest Engine[/bold cyan]\n"
        f"• Exec Mode : [yellow]{args.mode.upper()}[/yellow] | Scanner: [yellow]{args.scanner}[/yellow]\n"
        f"• Entry     : [yellow]{args.entry}[/yellow] | Exit: [yellow]{args.exit}[/yellow]\n"
        f"• Regime    : [magenta]{getattr(args, 'regime_mode', 'all')}[/magenta] | Sizing: [green]{slots_info}[/green]\n"
        f"• Range     : [white]{args.start or 'Full'} ~ {args.end or 'Latest'}[/white]\n"
        f"• Capital   : [white]{args.capital:,.0f} KRW[/white]\n"
        f"• Data Dir  : [dim]{data_dir}[/dim]",
        title="[bold green]🚀 Initializing Pure Minute-Bar Simulation[/bold green]",
        expand=False,
    ))

    if args.mode in ("pessimistic", "optimistic"):
        summary = run_single_backtest(args, ambiguity_mode=args.mode, data_dir=data_dir)
        console.print(summary.render_rich_table())
        console.print(render_exit_distribution_table(summary))
        console.print(render_trades_table(summary.trades))
        if not args.no_export:
            save_backtest_reports(
                summary.trades, args.scanner, args.start, args.end, args.mode, args.export_csv, console
            )
    elif args.mode == "dual":
        pess_summary = run_single_backtest(args, ambiguity_mode="pessimistic", data_dir=data_dir)
        opti_summary = run_single_backtest(args, ambiguity_mode="optimistic", data_dir=data_dir)
        console.print("")
        console.print(render_dual_bounds_table(pess_summary, opti_summary, initial_capital=args.capital))
        console.print("")
        console.print(render_exit_distribution_table(pess_summary, opti_summary))

        pnl_spread = opti_summary.total_net_pnl - pess_summary.total_net_pnl
        spread_pct = (pnl_spread / args.capital) * 100

        if spread_pct <= 3.0:
            verdict = f"[bold green]🛡️ High Robustness Passed (Ambiguity Spread: {spread_pct:.2f}%)[/bold green]"
        elif spread_pct <= 10.0:
            verdict = f"[bold yellow]⚠️ Moderate Sensitivity (Ambiguity Spread: {spread_pct:.2f}%)[/bold yellow]"
        else:
            verdict = f"[bold red]🚨 High Ambiguity Warning (Ambiguity Spread: {spread_pct:.2f}%)[/bold red]"
        console.print(Panel(verdict, title="Verdict", expand=False))

        console.print("")
        console.print(render_trades_table(pess_summary.trades))
        if not args.no_export:
            console.print("")
            p_custom = f"{os.path.splitext(args.export_csv)[0]}_pessimistic{os.path.splitext(args.export_csv)[1]}" if args.export_csv else None
            o_custom = f"{os.path.splitext(args.export_csv)[0]}_optimistic{os.path.splitext(args.export_csv)[1]}" if args.export_csv else None
            save_backtest_reports(pess_summary.trades, args.scanner, args.start, args.end, "pessimistic", p_custom, console)
            save_backtest_reports(opti_summary.trades, args.scanner, args.start, args.end, "optimistic", o_custom, console)


def cmd_audit(args: argparse.Namespace) -> None:
    """Runs Universal Strategy Contract Verification Suite on assembled strategy."""
    console.print(Panel(
        f"[bold cyan]Universal Strategy Contract Suite[/bold cyan]\n"
        f"• Target Strategy : [bold white]Composite_{args.scanner}_{args.entry}_{args.exit}[/bold white]\n"
        f"• Scanner Block   : [yellow]{args.scanner}[/yellow]\n"
        f"• Entry Block     : [yellow]{args.entry}[/yellow]\n"
        f"• Exit Block      : [yellow]{args.exit}[/yellow] (Mode: {args.ambiguity_mode})\n"
        f"• North Star Goal : [bold green]0% Simulation-to-Live Discrepancy[/bold green]",
        title="[bold magenta]🛡️ Initializing Strategy Contract Audit[/bold magenta]",
        expand=False,
    ))

    exit_kwargs = {}
    if hasattr(args, "trailing_buffer") and args.trailing_buffer is not None:
        exit_kwargs["trailing_buffer_pct"] = args.trailing_buffer

    strategy = build_strategy(
        scanner_name=args.scanner,
        entry_name=args.entry,
        exit_name=args.exit,
        exit_kwargs=exit_kwargs,
        ambiguity_mode=args.ambiguity_mode,
    )

    auditor = UniversalStrategyAuditor()
    report = auditor.audit(strategy)

    console.print("")
    console.print(report.render_table())
    console.print("")

    if report.passed:
        verdict_msg = (
            f"[bold green]✅ ALL CONTRACTS PASSED ({report.passed_checks}/{report.total_checks})[/bold green]\n"
            f"Lookahead 0%, KRX 호가 규격, 래칫 SL 단조성 등 실전 배포 계약을 100% 충족하였습니다."
        )
        console.print(Panel(verdict_msg, title="[bold green]🏆 Audit Approved[/bold green]", expand=False))
        sys.exit(0)
    failed_count = report.total_checks - report.passed_checks
    verdict_msg = f"[bold red]❌ AUDIT FAILED ({failed_count} Violations)[/bold red]\n치명적 불변 계약 위반 발견."
    console.print(Panel(verdict_msg, title="[bold red]🚨 Audit Rejected[/bold red]", expand=False))
    sys.exit(1)


def cmd_review_failures() -> None:
    """Prints the Failure & Lessons checklist for quick review."""
    path = os.path.join(os.path.dirname(__file__), "docs", "00_FAILURES_AND_LESSONS.md")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            console.print(f.read())
    else:
        console.print("[red]docs/00_FAILURES_AND_LESSONS.md not found![/red]")


def cmd_mock(args: argparse.Namespace) -> None:
    """Runs live mock paper trading on KIS VTS."""
    from engine.daily_routine import DailyRoutineOrchestrator
    orchestrator = DailyRoutineOrchestrator(
        mode="mock",
        scanner_name=args.scanner,
        entry_name=args.entry,
        exit_name=args.exit,
        capital=args.capital,
    )
    orchestrator.run(wait_for_market_open=not args.nowait)


def cmd_live(args: argparse.Namespace) -> None:
    """Runs live real cash trading on KIS REAL (requires --yes)."""
    if not args.yes:
        console.print(Panel(
            "[bold red]🚨 Safety Guard: Live real cash trading requires the '--yes' confirmation flag.[/bold red]\n"
            "Execution aborted to prevent accidental real-money orders.\n\n"
            "To confirm live real trading, run:\n"
            "  [bold yellow]python main.py live --yes[/bold yellow]",
            title="[bold red]Live Trading Refused[/bold red]",
            border_style="red",
        ))
        sys.exit(1)

    from engine.daily_routine import DailyRoutineOrchestrator
    orchestrator = DailyRoutineOrchestrator(
        mode="live",
        scanner_name=args.scanner,
        entry_name=args.entry,
        exit_name=args.exit,
        capital=args.capital,
    )
    orchestrator.run(wait_for_market_open=not args.nowait)


def cmd_routine(args: argparse.Namespace) -> None:
    """Runs the automated daily quantitative routine."""
    if args.mode == "live" and not args.yes:
        console.print(Panel(
            "[bold red]🚨 Safety Guard: Live real cash trading requires the '--yes' confirmation flag.[/bold red]\n"
            "Execution aborted to prevent accidental real-money orders.\n\n"
            "To confirm live routine, run:\n"
            "  [bold yellow]python main.py routine --mode live --yes[/bold yellow]",
            title="[bold red]Live Routine Refused[/bold red]",
            border_style="red",
        ))
        sys.exit(1)

    from engine.daily_routine import DailyRoutineOrchestrator
    orchestrator = DailyRoutineOrchestrator(
        mode=args.mode,
        target_date=args.date,
        scanner_name=args.scanner,
        entry_name=args.entry,
        exit_name=args.exit,
        capital=args.capital,
    )
    orchestrator.run(wait_for_market_open=not args.nowait)


def cmd_check_live(args: argparse.Namespace) -> None:
    """Runs pre-flight readiness checks."""
    from check_live_readiness import run_preflight_checks
    all_passed = run_preflight_checks(env_override=args.env, skip_ws=args.skip_ws)
    sys.exit(0 if all_passed else 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Korea Market v3 Systematic Trading CLI")
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # backtest
    bt_parser = subparsers.add_parser("backtest", help="Run deterministic pure 1-minute bar backtest")
    bt_parser.add_argument("--scanner", default="all_weather", help="Scanner module name")
    bt_parser.add_argument("--entry", default="all_weather", help="Entry module name")
    bt_parser.add_argument("--exit", default="all_weather", help="Exit module name")
    bt_parser.add_argument("--start", default="2026-01-02", help="Start date (YYYY-MM-DD or YYYYMMDD, default: 2026-01-02)")
    bt_parser.add_argument("--end", default="2026-11-02", help="End date (YYYY-MM-DD or YYYYMMDD, default: 2026-11-02)")
    bt_parser.add_argument("--capital", type=float, default=10_000_000.0, help="Initial capital in KRW (default: 10,000,000)")
    bt_parser.add_argument("--mode", choices=["pessimistic", "optimistic", "dual"], default="pessimistic", help="Ambiguity mode (default: pessimistic)")
    bt_parser.add_argument("--regime-mode", choices=["all", "bear_only", "all_weather", "unified"], default="all", help="Regime gating mode: 'all', 'bear_only', 'all_weather', or 'unified' (default: all)")
    bt_parser.add_argument("--slots", type=int, default=None, help="Fixed daily slots (overrides dynamic slots)")
    bt_parser.add_argument("--data-dir", default=None, help="Custom path to daily 1-min parquet data directory")
    bt_parser.add_argument("--min-gap", type=float, default=0.00, help="Min morning gap (default: 0.00 = 0.0%%)")
    bt_parser.add_argument("--max-gap", type=float, default=0.10, help="Max morning gap (default: 0.10 = +10.0%%)")
    bt_parser.add_argument("--max-mae", type=float, default=-0.015, help="Max pre-breakout drop (default: -0.015 = -1.5%%)")
    bt_parser.add_argument("--trailing-buffer", type=float, default=None, help="Trailing buffer pct for continuous HWM exit (e.g. 0.015 or 0.020)")
    bt_parser.add_argument("--export-csv", default=None, help="Custom output path for trade details CSV")
    bt_parser.add_argument("--dynamic", dest="dynamic", action="store_true", default=True, help="Adaptive variables (default: True)")
    bt_parser.add_argument("--fixed", dest="dynamic", action="store_false", help="Legacy fixed constants")
    bt_parser.add_argument("--no-export", action="store_true", help="Disable automatic CSV export")
    bt_parser.add_argument("--entry-start", type=int, default=0, help="Entry start minute (0 = 09:00:00)")
    bt_parser.add_argument("--entry-end", type=int, default=10, help="Entry end minute (10 = 09:10:00)")
    bt_parser.add_argument("--entry-style", choices=["orb", "pullback", "volume_surge"], default="orb", help="Entry style")
    bt_parser.add_argument("--priority", choices=["turnover", "momentum", "gap_stability"], default="turnover", help="Race condition priority")
    bt_parser.add_argument("--tp1", type=float, default=0.02, help="TP1 profit lock target pct (e.g. 0.02 = +2.0%%)")
    bt_parser.add_argument("--sl", type=float, default=-0.015, help="Hard stop loss pct (e.g. -0.015 = -1.5%%)")
    bt_parser.add_argument("--trailing-offset", type=float, default=0.010, help="Trailing offset pct from peak (e.g. 0.010 = -1.0%%)")

    # audit
    audit_parser = subparsers.add_parser("audit", help="Run Universal Strategy Contract Suite audit")
    audit_parser.add_argument("--scanner", default="morning_leader", help="Scanner module name")
    audit_parser.add_argument("--entry", default="morning_breakout", help="Entry module name")
    audit_parser.add_argument("--exit", default="morning_leader", help="Exit module name")
    audit_parser.add_argument(
        "--ambiguity-mode",
        choices=["pessimistic", "optimistic"],
        default="pessimistic",
        help="Ambiguity mode to test",
    )

    # strategies
    subparsers.add_parser("strategies", help="List all available modular Lego blocks")

    # review-failures
    subparsers.add_parser("review-failures", help="Show past issues and failure checklist")

    # mock
    mock_parser = subparsers.add_parser("mock", help="Run live mock paper trading on KIS VTS")
    mock_parser.add_argument("--scanner", default="all_weather", help="Scanner module name")
    mock_parser.add_argument("--entry", default="all_weather", help="Entry module name")
    mock_parser.add_argument("--exit", default="all_weather", help="Exit module name")
    mock_parser.add_argument("--capital", type=float, default=10_000_000.0, help="Initial capital in KRW")
    mock_parser.add_argument("--nowait", action="store_true", help="Skip waiting for 09:00 market open")

    # live
    live_parser = subparsers.add_parser("live", help="Run live cash trading on KIS REAL (requires --yes)")
    live_parser.add_argument("--yes", action="store_true", help="Confirm real cash execution safety gate")
    live_parser.add_argument("--scanner", default="all_weather", help="Scanner module name")
    live_parser.add_argument("--entry", default="all_weather", help="Entry module name")
    live_parser.add_argument("--exit", default="all_weather", help="Exit module name")
    live_parser.add_argument("--capital", type=float, default=10_000_000.0, help="Initial capital in KRW")
    live_parser.add_argument("--nowait", action="store_true", help="Skip waiting for 09:00 market open")

    # routine
    routine_parser = subparsers.add_parser("routine", help="Run daily automated orchestrator")
    routine_parser.add_argument("--mode", choices=["mock", "live"], default="mock", help="Trading mode (mock/live)")
    routine_parser.add_argument("--yes", action="store_true", help="Confirm real money execution for live mode")
    routine_parser.add_argument("--date", default=None, help="Target date YYYYMMDD")
    routine_parser.add_argument("--scanner", default="all_weather", help="Scanner module name")
    routine_parser.add_argument("--entry", default="all_weather", help="Entry module name")
    routine_parser.add_argument("--exit", default="all_weather", help="Exit module name")
    routine_parser.add_argument("--capital", type=float, default=10_000_000.0, help="Initial capital in KRW")
    routine_parser.add_argument("--nowait", action="store_true", help="Skip waiting for 09:00 market open")

    # check-live
    check_parser = subparsers.add_parser("check-live", help="Run pre-flight live readiness checks")
    check_parser.add_argument("--env", choices=["vts", "real"], default="vts", help="Environment to check (vts/real)")
    check_parser.add_argument("--skip-ws", action="store_true", help="Skip WebSocket connectivity handshake")

    args = parser.parse_args()

    if args.command == "strategies":
        cmd_strategies()
    elif args.command == "backtest":
        cmd_backtest(args)
    elif args.command == "audit":
        cmd_audit(args)
    elif args.command == "review-failures":
        cmd_review_failures()
    elif args.command == "mock":
        cmd_mock(args)
    elif args.command == "live":
        cmd_live(args)
    elif args.command == "routine":
        cmd_routine(args)
    elif args.command == "check-live":
        cmd_check_live(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
