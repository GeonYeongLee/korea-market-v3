"""
Trade-by-Trade Reporting and CSV Export Engine for Korea Market v3.
Provides Rich console visualization and BOM-encoded CSV export for Excel compatibility.
"""
from __future__ import annotations

import csv
import json
import os
import re
from typing import Dict, List, Optional
from rich.table import Table

from engine.models import TradeRecord

if False:  # TYPE_CHECKING
    from engine.metrics import PerformanceSummary

_SYMBOL_MAP_CACHE: Optional[Dict[str, str]] = None


def load_symbol_map(metadata_path: str = "data/metadata/market_map.json") -> Dict[str, str]:
    """
    Loads ticker-to-name mapping from market_map.json.
    Strips trailing spaces and ticker codes using re.sub(r'\\s{2,}.*$', '', name).strip().
    Caches the dictionary in memory.
    """
    global _SYMBOL_MAP_CACHE
    if _SYMBOL_MAP_CACHE is not None:
        return _SYMBOL_MAP_CACHE

    target_path = metadata_path
    if not os.path.exists(target_path):
        fallback = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data",
            "metadata",
            "market_map.json",
        )
        if os.path.exists(fallback):
            target_path = fallback

    if not os.path.exists(target_path):
        _SYMBOL_MAP_CACHE = {}
        return _SYMBOL_MAP_CACHE

    try:
        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        symbol_map = {}
        for symbol, info in data.items():
            if isinstance(info, dict) and "name" in info:
                raw_name = info["name"]
                clean_name = re.sub(r"\s{2,}.*$", "", raw_name).strip()
                symbol_map[symbol] = clean_name
            elif isinstance(info, str):
                symbol_map[symbol] = re.sub(r"\s{2,}.*$", "", info).strip()
        _SYMBOL_MAP_CACHE = symbol_map
    except Exception:
        _SYMBOL_MAP_CACHE = {}

    return _SYMBOL_MAP_CACHE


def export_trades_to_csv(
    trades: List[TradeRecord],
    output_path: str,
    symbol_map: Optional[Dict[str, str]] = None,
) -> str:
    """
    Exports a list of TradeRecords to a CSV file encoded in UTF-8 with BOM (utf-8-sig).
    Ensures that Korean characters open without corruption in Microsoft Excel.
    """
    if symbol_map is None:
        symbol_map = load_symbol_map()

    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    fieldnames = [
        "trade_id",
        "symbol",
        "name",
        "entry_dt",
        "entry_price",
        "exit_dt",
        "exit_price",
        "quantity",
        "holding_min",
        "return_pct",
        "gross_pnl",
        "net_pnl",
        "exit_reason",
        "mae_pct",
        "mfe_pct",
    ]

    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for t in trades:
            holding_min = round(t.holding_seconds / 60.0, 1)
            row = {
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "name": symbol_map.get(t.symbol, t.symbol),
                "entry_dt": t.entry_dt.strftime("%Y-%m-%d %H:%M"),
                "entry_price": int(round(t.entry_price)) if t.entry_price.is_integer() else round(t.entry_price, 2),
                "exit_dt": t.exit_dt.strftime("%Y-%m-%d %H:%M"),
                "exit_price": int(round(t.exit_price)) if t.exit_price.is_integer() else round(t.exit_price, 2),
                "quantity": t.quantity,
                "holding_min": holding_min,
                "return_pct": round(t.return_pct * 100, 2),
                "gross_pnl": int(round(t.gross_pnl)),
                "net_pnl": int(round(t.net_pnl)),
                "exit_reason": t.exit_reason,
                "mae_pct": round(t.mae_pct * 100, 2),
                "mfe_pct": round(t.mfe_pct * 100, 2),
            }
            writer.writerow(row)

    return output_path


def render_trades_table(
    trades: List[TradeRecord],
    symbol_map: Optional[Dict[str, str]] = None,
    max_display: int = 50,
) -> Table:
    """
    Renders a Rich Table summarizing trade executions.
    If trades exceed max_display, shows the most recent max_display trades.
    """
    if symbol_map is None:
        symbol_map = load_symbol_map()

    table = Table(
        title="📋 Detailed Trade Execution Log (상세 체결 내역)",
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
    )
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("종목코드", justify="center", style="bold", no_wrap=True)
    table.add_column("종목명", justify="left", style="white")
    table.add_column("매수일시", justify="center", style="dim", no_wrap=True)
    table.add_column("매수가", justify="right", no_wrap=True)
    table.add_column("매도일시", justify="center", style="dim", no_wrap=True)
    table.add_column("매도가", justify="right", no_wrap=True)
    table.add_column("수익률(%)", justify="right", style="bold", no_wrap=True)
    table.add_column("실현손익(원)", justify="right", style="bold", no_wrap=True)
    table.add_column("보유(분)", justify="right", no_wrap=True)
    table.add_column("청산사유", justify="left")

    if not trades:
        table.caption = "[yellow]체결된 거래 내역이 없습니다.[/yellow]"
        return table


    total_count = len(trades)
    display_trades = trades[-max_display:] if total_count > max_display else trades
    start_idx = total_count - len(display_trades) + 1

    for i, t in enumerate(display_trades, start=start_idx):
        color = "green" if t.net_pnl >= 0 else "red"
        name = symbol_map.get(t.symbol, t.symbol)
        holding_min = t.holding_seconds / 60.0

        table.add_row(
            str(i),
            t.symbol,
            name[:12],
            t.entry_dt.strftime("%m-%d %H:%M"),
            f"{t.entry_price:,.0f}",
            t.exit_dt.strftime("%m-%d %H:%M"),
            f"{t.exit_price:,.0f}",
            f"[{color}]{t.return_pct * 100:+.2f}%[/{color}]",
            f"[{color}]{t.net_pnl:+,.0f}[/{color}]",
            f"{holding_min:.0f}",
            t.exit_reason,
        )

    if total_count > max_display:
        table.caption = (
            f"[dim]ℹ️ 총 [bold]{total_count}[/bold]건 중 최근 [bold]{len(display_trades)}[/bold]건을 표시합니다. "
            f"전체 상세 내역은 CSV 보고서를 확인하세요.[/dim]"
        )
    else:
        table.caption = f"[dim]ℹ️ 총 [bold]{total_count}[/bold]건의 거래가 완료되었습니다.[/dim]"

    return table


def save_backtest_reports(
    trades: List[TradeRecord],
    scanner: str,
    start: str,
    end: str,
    mode: str,
    custom_path: Optional[str] = None,
    console=None,
) -> str:
    """Saves backtest trades to CSV and prints notification."""
    default_csv = os.path.join("reports", f"trades_{scanner}_{start}_{end}_{mode}.csv")
    csv_path = custom_path or default_csv
    export_trades_to_csv(trades, csv_path)
    if console:
        mode_label = f"{mode.upper()} " if mode != "pessimistic" else ""
        console.print(
            f"[bold green]📁 {mode_label}체결 내역 저장 완료:[/] [cyan]{csv_path}[/] (총 {len(trades)}건)"
        )
    return csv_path

