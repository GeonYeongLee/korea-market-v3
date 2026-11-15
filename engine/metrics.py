"""
Performance Metrics and Reporting Engine.
Calculates key quant metrics: Net PnL, Win Rate, Profit Factor, MDD, and Expectancy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Optional
from rich.table import Table
from rich.panel import Panel
from rich.console import Console

from engine.models import TradeRecord


@dataclass(slots=True)
class PerformanceSummary:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float
    total_gross_pnl: float
    total_net_pnl: float
    max_drawdown_amount: float
    max_drawdown_pct: float
    avg_trade_pnl: float
    avg_holding_minutes: float
    exit_reasons: Dict[str, int] = field(default_factory=dict)
    trades: List[TradeRecord] = field(default_factory=list)

    def render_rich_table(self) -> Panel:
        table = Table(show_header=True, header_style="bold cyan", border_style="dim")
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")

        table.add_row("Total Trades", f"{self.total_trades}")
        win_color = "green" if self.win_rate >= 0.5 else "yellow"
        table.add_row("Win Rate", f"[{win_color}]{self.win_rate * 100:.1f}%[/{win_color}] ({self.winning_trades}W / {self.losing_trades}L)")
        pnl_color = "green" if self.total_net_pnl >= 0 else "red"
        table.add_row("Total Net PnL", f"[{pnl_color}]{self.total_net_pnl:+,.0f} KRW[/{pnl_color}]")
        table.add_row("Profit Factor", f"{self.profit_factor:.2f}")
        table.add_row("Max Drawdown (MDD)", f"[red]-{self.max_drawdown_amount:,.0f} KRW ({self.max_drawdown_pct * 100:.2f}%)[/red]")
        table.add_row("Avg Trade PnL", f"{self.avg_trade_pnl:+,.0f} KRW")
        table.add_row("Avg Holding Time", f"{self.avg_holding_minutes:.1f} min")

        return Panel(table, title="[bold green]📊 Quantitative Performance Summary[/bold green]", expand=False)


def render_dual_bounds_table(
    pess: PerformanceSummary,
    opti: PerformanceSummary,
    initial_capital: float,
) -> Table:
    """Renders a side-by-side comparison table between Pessimistic and Optimistic runs."""
    table = Table(
        title="⚖️ Dual-Bounds Ambiguity Audit (Pessimistic vs Optimistic)",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Metric", style="bold cyan")
    table.add_column("Pessimistic (SL First)", justify="right", style="bold red")
    table.add_column("Optimistic (TP First)", justify="right", style="bold green")
    table.add_column("Spread (Δ)", justify="right", style="bold yellow")

    t_diff = opti.total_trades - pess.total_trades
    table.add_row("Total Trades", str(pess.total_trades), str(opti.total_trades), f"{t_diff:+d}")

    wr_diff = (opti.win_rate - pess.win_rate) * 100
    table.add_row(
        "Win Rate",
        f"{pess.win_rate * 100:.1f}% ({pess.winning_trades}W/{pess.losing_trades}L)",
        f"{opti.win_rate * 100:.1f}% ({opti.winning_trades}W/{opti.losing_trades}L)",
        f"{wr_diff:+.1f}%p",
    )

    pnl_diff = opti.total_net_pnl - pess.total_net_pnl
    table.add_row(
        "Total Net PnL",
        f"{pess.total_net_pnl:+,.0f} KRW",
        f"{opti.total_net_pnl:+,.0f} KRW",
        f"{pnl_diff:+,.0f} KRW",
    )

    pf_diff = opti.profit_factor - pess.profit_factor
    table.add_row(
        "Profit Factor",
        f"{pess.profit_factor:.2f}",
        f"{opti.profit_factor:.2f}",
        f"{pf_diff:+.2f}",
    )

    mdd_pct_diff = (opti.max_drawdown_pct - pess.max_drawdown_pct) * 100
    table.add_row(
        "Max Drawdown (MDD)",
        f"{pess.max_drawdown_amount:,.0f} KRW ({pess.max_drawdown_pct * 100:.2f}%)",
        f"{opti.max_drawdown_amount:,.0f} KRW ({opti.max_drawdown_pct * 100:.2f}%)",
        f"{mdd_pct_diff:+.2f}%p",
    )

    hold_diff = opti.avg_holding_minutes - pess.avg_holding_minutes
    table.add_row(
        "Avg Holding Time",
        f"{pess.avg_holding_minutes:.1f} min",
        f"{opti.avg_holding_minutes:.1f} min",
        f"{hold_diff:+.1f} min",
    )

    return table


def render_exit_distribution_table(
    pess: PerformanceSummary,
    opti: Optional[PerformanceSummary] = None,
) -> Table:
    """Renders a breakdown table of exit reasons and their proportions."""
    table = Table(
        title="🚪 Exit Reason Distribution",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Exit Reason", style="bold white")
    if opti is None:
        table.add_column("Count", justify="right", style="bold yellow")
        table.add_column("Share (%)", justify="right")
        for reason, count in sorted(pess.exit_reasons.items(), key=lambda x: x[1], reverse=True):
            pct = (count / pess.total_trades * 100) if pess.total_trades > 0 else 0.0
            table.add_row(reason, str(count), f"{pct:.1f}%")
    else:
        table.add_column("Pessimistic Count", justify="right", style="bold red")
        table.add_column("Pess Share", justify="right")
        table.add_column("Optimistic Count", justify="right", style="bold green")
        table.add_column("Opti Share", justify="right")

        all_reasons = sorted(set(list(pess.exit_reasons.keys()) + list(opti.exit_reasons.keys())))
        for reason in all_reasons:
            p_cnt = pess.exit_reasons.get(reason, 0)
            o_cnt = opti.exit_reasons.get(reason, 0)
            p_pct = (p_cnt / pess.total_trades * 100) if pess.total_trades > 0 else 0.0
            o_pct = (o_cnt / opti.total_trades * 100) if opti.total_trades > 0 else 0.0
            table.add_row(reason, str(p_cnt), f"{p_pct:.1f}%", str(o_cnt), f"{o_pct:.1f}%")

    return table


def calculate_performance(trades: List[TradeRecord], initial_capital: float = 10_000_000.0) -> PerformanceSummary:
    if not trades:
        return PerformanceSummary(
            total_trades=0, winning_trades=0, losing_trades=0,
            win_rate=0.0, profit_factor=0.0, total_gross_pnl=0.0,
            total_net_pnl=0.0, max_drawdown_amount=0.0, max_drawdown_pct=0.0,
            avg_trade_pnl=0.0, avg_holding_minutes=0.0,
            exit_reasons={}, trades=[],
        )

    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]

    gross_wins = sum(t.net_pnl for t in wins)
    gross_losses = abs(sum(t.net_pnl for t in losses))
    pf = (gross_wins / gross_losses) if gross_losses > 0 else (99.9 if gross_wins > 0 else 0.0)

    equity = initial_capital
    peak = initial_capital
    max_dd_amt = 0.0
    max_dd_pct = 0.0

    for t in trades:
        equity += t.net_pnl
        if equity > peak:
            peak = equity
        dd_amt = peak - equity
        dd_pct = dd_amt / peak if peak > 0 else 0.0
        if dd_amt > max_dd_amt:
            max_dd_amt = dd_amt
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct

    total_net = sum(t.net_pnl for t in trades)
    total_gross = sum(t.gross_pnl for t in trades)
    avg_pnl = total_net / len(trades)
    avg_hold = sum(t.holding_seconds for t in trades) / (len(trades) * 60.0)

    exit_reasons: Dict[str, int] = {}
    for t in trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

    return PerformanceSummary(
        total_trades=len(trades),
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate=len(wins) / len(trades),
        profit_factor=pf,
        total_gross_pnl=total_gross,
        total_net_pnl=total_net,
        max_drawdown_amount=max_dd_amt,
        max_drawdown_pct=max_dd_pct,
        avg_trade_pnl=avg_pnl,
        avg_holding_minutes=avg_hold,
        exit_reasons=exit_reasons,
        trades=trades,
    )
