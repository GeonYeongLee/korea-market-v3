"""
Rich Console & CSV Report Rendering for Korea Market v4 Backtest CLI.
"""
from __future__ import annotations

import argparse
import os

from rich.console import Console
from rich.panel import Panel

from engine.metrics import (
    PerformanceSummary,
    render_dual_bounds_table,
    render_exit_distribution_table,
)
from engine.trade_reporter import render_trades_table, save_backtest_reports


def render_init_banner(
    out: Console,
    args: argparse.Namespace,
    data_dir: str,
    capital: float,
) -> None:
    """Prints the v4 backtest initialization Rich panel."""
    slots = getattr(args, "slots", None)
    strict_parity = bool(getattr(args, "strict_v3_parity", False))
    slots_info = "Dynamic (2 slots / All-Weather)" if slots is None else f"Fixed ({slots} slots)"
    parity_info = " | Parity: [red]STRICT_V3[/red]" if strict_parity else ""

    out.print(
        Panel(
            f"[bold cyan]Korea Market v4 Unified Backtest Engine[/bold cyan]\n"
            f"• Exec Mode : [yellow]{args.mode.upper()}[/yellow] | Strategy: [yellow]{args.scanner}[/yellow]{parity_info}\n"
            f"• Regime    : [magenta]{getattr(args, 'regime_mode', 'all_weather')}[/magenta] | Sizing: [green]{slots_info}[/green]\n"
            f"• Range     : [white]{args.start or 'Full'} ~ {args.end or 'Latest'}[/white]\n"
            f"• Capital   : [white]{capital:,.0f} KRW[/white]\n"
            f"• Data Dir  : [dim]{data_dir}[/dim]",
            title="[bold green]🚀 Initializing Unified v4 Simulation[/bold green]",
            expand=False,
        )
    )


def render_single_report(
    out: Console,
    args: argparse.Namespace,
    summary: PerformanceSummary,
) -> None:
    """Renders single-mode Rich tables and optional CSV export."""
    out.print(summary.render_rich_table())
    out.print(render_exit_distribution_table(summary))
    out.print(render_trades_table(summary.trades))
    if not args.no_export:
        save_backtest_reports(summary.trades, args.scanner, args.start, args.end, args.mode, args.export_csv, out)


def render_dual_report(
    out: Console,
    args: argparse.Namespace,
    pess: PerformanceSummary,
    opti: PerformanceSummary,
    capital: float,
) -> None:
    """Renders Dual-Bounds Rich tables, ambiguity verdict panel, and optional CSV exports."""
    out.print("")
    out.print(render_dual_bounds_table(pess, opti, initial_capital=capital))
    out.print("")
    out.print(render_exit_distribution_table(pess, opti))

    pnl_spread = opti.total_net_pnl - pess.total_net_pnl
    spread_pct = (pnl_spread / capital) * 100.0 if capital > 0.0 else 0.0
    if spread_pct <= 3.0:
        verdict = f"[bold green]🛡️ High Robustness Passed (Ambiguity Spread: {spread_pct:.2f}%)[/bold green]"
    elif spread_pct <= 10.0:
        verdict = f"[bold yellow]⚠️ Moderate Sensitivity (Ambiguity Spread: {spread_pct:.2f}%)[/bold yellow]"
    else:
        verdict = f"[bold red]🚨 High Ambiguity Warning (Ambiguity Spread: {spread_pct:.2f}%)[/bold red]"
    out.print(Panel(verdict, title="Verdict", expand=False))

    out.print("")
    out.print(render_trades_table(pess.trades))
    if not args.no_export:
        out.print("")
        base_csv = getattr(args, "export_csv", None)
        p_custom = f"{os.path.splitext(base_csv)[0]}_pessimistic{os.path.splitext(base_csv)[1]}" if base_csv else None
        o_custom = f"{os.path.splitext(base_csv)[0]}_optimistic{os.path.splitext(base_csv)[1]}" if base_csv else None
        save_backtest_reports(pess.trades, args.scanner, args.start, args.end, "pessimistic", p_custom, out)
        save_backtest_reports(opti.trades, args.scanner, args.start, args.end, "optimistic", o_custom, out)
