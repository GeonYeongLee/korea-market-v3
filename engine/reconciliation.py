"""
Korea Market v3 - Backtest vs Live Execution Reconciliation Engine
===================================================================
Compares post-market completed 1-minute bar backtest results against
actual intraday execution logs (logs/trades_audit_YYYYMMDD.jsonl).

Audits and diagnoses:
1. Candidate Selection Match (Backtest selected stocks vs Live executed stocks)
2. Entry Timing & Slippage (Target breakout price vs Actual filled price)
3. Exit Reason & Execution Slippage (Stop/Target trigger vs Fill price)
4. Net Realized PnL Deviation (KRW and %) across all executed symbols
5. Discrepancy Diagnostics & Root Cause Classification

Outputs:
- logs/reconciliation_YYYYMMDD.json
- logs/reconciliation_YYYYMMDD.md
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import os
import sys

# Ensure UTF-8 output on Windows terminals
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dataclasses import dataclass, field, asdict
from datetime import datetime, date, time
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from config.settings import settings
from core.krx_symbols import get_krx_name_map

logger = logging.getLogger("kis.engine.reconciliation")
console = Console(force_terminal=True, legacy_windows=False)


@dataclass
class TradeComparisonItem:
    """Side-by-side comparison for a single trade metric."""
    metric: str
    backtest_value: Any
    live_value: Any
    difference: str
    status: str  # "MATCH", "SLIPPAGE", "DIFF", "INFO"


@dataclass
class LiveTradeExecution:
    """Consolidated intraday execution details for a single symbol."""
    symbol: str
    name: str = ""
    has_trade: bool = False
    entry_time: Optional[str] = None
    entry_px: float = 0.0
    shares: int = 0
    sl_px: float = 0.0
    tp_px: float = 0.0
    exit_time: Optional[str] = None
    exit_px: float = 0.0
    exit_reason: str = "N/A"
    realized_pnl: float = 0.0
    order_ids: List[str] = field(default_factory=list)
    blocked_events: List[Dict[str, Any]] = field(default_factory=list)
    raw_events: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class SymbolReconciliationDetail:
    """Per-symbol side-by-side backtest vs live execution audit."""
    symbol: str
    name: str
    status: str  # "MATCH", "ACCEPTABLE_SLIPPAGE", "DISCREPANCY", "BACKTEST_ONLY", "LIVE_ONLY"
    backtest_executed: bool = False
    live_executed: bool = False

    bt_entry_time: Optional[str] = None
    bt_entry_px: float = 0.0
    bt_shares: int = 0
    bt_exit_time: Optional[str] = None
    bt_exit_px: float = 0.0
    bt_exit_reason: str = "N/A"
    bt_pnl: float = 0.0

    live_entry_time: Optional[str] = None
    live_entry_px: float = 0.0
    live_shares: int = 0
    live_exit_time: Optional[str] = None
    live_exit_px: float = 0.0
    live_exit_reason: str = "N/A"
    live_pnl: float = 0.0

    entry_slippage_krw: float = 0.0
    entry_slippage_pct: float = 0.0
    exit_slippage_krw: float = 0.0
    exit_slippage_pct: float = 0.0
    exit_slippage_krw: float = 0.0
    exit_slippage_pct: float = 0.0
    pnl_diff_krw: float = 0.0
    diagnosis: str = ""


@dataclass
class ReconciliationResult:
    """Full post-market reconciliation audit report."""
    target_date: str
    reconciled_at: str
    backtest_executed: bool
    live_executed: bool
    status: str  # "MATCH", "ACCEPTABLE_SLIPPAGE", "DISCREPANCY", "NO_TRADE_MATCH"
    backtest_symbol: Optional[str] = None
    live_symbol: Optional[str] = None
    symbol_match: bool = False

    # Financials
    backtest_pnl: float = 0.0
    live_pnl: float = 0.0
    pnl_diff_krw: float = 0.0

    # Prices
    backtest_entry_px: float = 0.0
    live_entry_px: float = 0.0
    entry_slippage_krw: float = 0.0
    entry_slippage_pct: float = 0.0
    exit_slippage_krw: float = 0.0
    exit_slippage_pct: float = 0.0

    backtest_exit_reason: str = "N/A"
    live_exit_reason: str = "N/A"
    exit_reason_match: bool = False

    # Diagnostics & Multi-symbol details
    root_causes: List[str] = field(default_factory=list)
    items: List[TradeComparisonItem] = field(default_factory=list)
    symbol_details: List[SymbolReconciliationDetail] = field(default_factory=list)
    raw_backtest_records: List[Dict[str, Any]] = field(default_factory=list)
    raw_live_trades: Dict[str, Any] = field(default_factory=dict)
    scanner_match_rate_pct: float = 0.0
    avg_slippage_bp: float = 0.0
    avg_execution_latency_ms: float = 0.0
    telemetry_summary: Dict[str, Any] = field(default_factory=dict)


def is_regular_market_hours(timestamp_str: str) -> bool:
    """Checks whether an ISO timestamp falls strictly within regular KRX hours (09:00:00 ~ 15:30:00)."""
    if not timestamp_str or "T" not in timestamp_str:
        return False
    try:
        t_str = timestamp_str.split("T")[-1][:8]
        h, m, s = map(int, t_str.split(":"))
        return time(9, 0, 0) <= time(h, m, s) <= time(15, 30, 0)
    except Exception:
        return False


def is_real_kis_order_id(order_id: Optional[str]) -> bool:
    """
    Identifies genuine KIS live/mock broker order IDs.
    Filters out backtest UUIDs ('ord_xxx') and synthetic virtual test IDs ('0000100xxx').
    """
    if not order_id or not isinstance(order_id, str):
        return False
    if order_id.startswith("ord_") or order_id.startswith("0000100"):
        return False
    return bool(re.match(r"^\d{8,10}$", order_id))


def parse_live_audit_log(date_compact: str) -> Tuple[List[Dict[str, Any]], Dict[str, LiveTradeExecution]]:
    """
    Parses logs/trades_audit_{date_compact}.jsonl, strictly filtering for
    regular trading hours (09:00~15:30) and genuine KIS order IDs.
    """
    log_file = settings.logs_dir / f"trades_audit_{date_compact}.jsonl"
    if not log_file.exists():
        logger.warning(f"Live trades audit log not found: {log_file}")
        return [], {}

    raw_lines: List[Dict[str, Any]] = []
    with open(log_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    raw_lines.append(json.loads(line))
                except Exception:
                    pass

    name_map = get_krx_name_map()
    valid_market_events: List[Dict[str, Any]] = []
    trades_by_symbol: Dict[str, LiveTradeExecution] = {}

    for e in raw_lines:
        ts = e.get("timestamp", "")
        if not is_regular_market_hours(ts):
            continue

        etype = e.get("event_type")
        sym = e.get("symbol")
        if not sym:
            continue

        oid = str(e.get("order_id", ""))

        if etype == "ANTI_CHASE_BLOCKED":
            valid_market_events.append(e)
            trade = trades_by_symbol.setdefault(sym, LiveTradeExecution(symbol=sym, name=name_map.get(sym, sym)))
            trade.blocked_events.append(e)
            continue

        if etype in ("ORDER_SUBMITTED", "ORDER_EVENT") and not is_real_kis_order_id(oid):
            continue

        valid_market_events.append(e)
        trade = trades_by_symbol.setdefault(sym, LiveTradeExecution(symbol=sym, name=name_map.get(sym, sym)))
        trade.raw_events.append(e)
        if oid and oid not in trade.order_ids:
            trade.order_ids.append(oid)

        time_part = ts.split("T")[-1][:8]

        if etype == "ORDER_SUBMITTED" and e.get("side") == "BUY":
            trade.has_trade = True
            trade.entry_time = time_part
            trade.entry_px = float(e.get("price", trade.entry_px))
            trade.shares = int(e.get("qty", trade.shares))
            trade.sl_px = float(e.get("sl_px", trade.sl_px))
            trade.tp_px = float(e.get("tp1_px", trade.tp_px))

        elif etype == "ORDER_EVENT" and e.get("status") == "FILLED" and e.get("side") == "BUY":
            trade.has_trade = True
            trade.entry_time = time_part
            trade.entry_px = float(e.get("price", trade.entry_px))
            trade.shares = int(e.get("qty", trade.shares))

        elif etype in ("STOP_LIQUIDATION", "EOD_TIME_STOP"):
            trade.exit_time = time_part
            trade.exit_px = float(e.get("price", trade.exit_px))
            trade.exit_reason = e.get("exit_tag", etype)

        elif etype == "ORDER_SUBMITTED" and e.get("side") == "SELL":
            trade.exit_time = time_part
            trade.exit_px = float(e.get("price", trade.exit_px))
            trade.exit_reason = e.get("tag", trade.exit_reason or "SELL_SUBMITTED")

        elif etype == "ORDER_EVENT" and e.get("status") == "FILLED" and e.get("side") == "SELL":
            trade.exit_time = time_part
            trade.exit_px = float(e.get("price", trade.exit_px))
            trade.realized_pnl += float(e.get("realized_pnl", 0.0))

    # Calculate net realized PnL with standard KRX transaction costs if broker pnl was 0
    for sym, trade in trades_by_symbol.items():
        if trade.has_trade and trade.realized_pnl == 0.0 and trade.exit_px > 0 and trade.entry_px > 0 and trade.shares > 0:
            entry_val = trade.entry_px * trade.shares
            exit_val = trade.exit_px * trade.shares
            cost = (entry_val * 0.00014) + (exit_val * (0.00014 + 0.0018))
            trade.realized_pnl = (exit_val - entry_val) - cost

    active_trades = {s: t for s, t in trades_by_symbol.items() if t.has_trade}
    return valid_market_events, active_trades


def parse_telemetry_logs(date_compact: str) -> Dict[str, Any]:
    """
    Parses live_api_YYYYMMDD.jsonl, live_strategy_eval_YYYYMMDD.jsonl,
    and live_executions_YYYYMMDD.jsonl to extract diagnostic telemetry metrics.
    """
    summary: Dict[str, Any] = {
        "scanner_evaluated": set(),
        "scanner_passed": set(),
        "scanner_rejections": {},
        "entry_evaluated": set(),
        "entry_passed": set(),
        "exit_evaluations": [],
        "api_total_calls": 0,
        "api_failed_calls": 0,
        "api_latencies": [],
        "api_avg_latency_ms": 0.0,
        "api_max_latency_ms": 0.0,
        "ws_frames_count": 0,
        "order_latencies_ms": [],
        "order_slippages_bp": [],
        "avg_execution_latency_ms": 0.0,
        "avg_slippage_bp": 0.0,
    }

    # 1. Parse Strategy Evaluation Log
    eval_file = settings.logs_dir / f"live_strategy_eval_{date_compact}.jsonl"
    if eval_file.exists():
        with open(eval_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    etype = row.get("eval_type")
                    sym = row.get("symbol")
                    dec = row.get("decision")
                    reason = row.get("reason", "")
                    if etype == "SCANNER" and sym:
                        summary["scanner_evaluated"].add(sym)
                        if dec in ("PASS", "PROMOTED"):
                            summary["scanner_passed"].add(sym)
                        else:
                            summary["scanner_rejections"].setdefault(sym, []).append(reason)
                    elif etype == "ENTRY" and sym:
                        summary["entry_evaluated"].add(sym)
                        if dec in ("PASS", "PASS_BREAKOUT_ENTRY"):
                            summary["entry_passed"].add(sym)
                    elif etype == "EXIT" and sym:
                        summary["exit_evaluations"].append(row)
                except Exception:
                    pass

    # 2. Parse Execution Log
    exec_file = settings.logs_dir / f"live_executions_{date_compact}.jsonl"
    submissions: Dict[str, datetime] = {}
    if exec_file.exists():
        with open(exec_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    ev = row.get("event_type")
                    ono = row.get("order_no")
                    ts_str = row.get("timestamp")
                    dt = datetime.fromisoformat(ts_str) if ts_str else None

                    if ev == "ORDER_SUBMITTED" and ono and dt:
                        submissions[ono] = dt
                    elif ev in ("ORDER_FILLED", "ORDER_PARTIALLY_FILLED"):
                        if ono and ono in submissions and dt:
                            lat = (dt - submissions[ono]).total_seconds() * 1000.0
                            summary["order_latencies_ms"].append(lat)
                        slip_bp = row.get("slippage_bp")
                        if slip_bp is not None:
                            summary["order_slippages_bp"].append(float(slip_bp))
                except Exception:
                    pass

    if summary["order_latencies_ms"]:
        summary["avg_execution_latency_ms"] = round(sum(summary["order_latencies_ms"]) / len(summary["order_latencies_ms"]), 2)
    if summary["order_slippages_bp"]:
        summary["avg_slippage_bp"] = round(sum(summary["order_slippages_bp"]) / len(summary["order_slippages_bp"]), 2)

    # 3. Parse API Log
    api_file = settings.logs_dir / f"live_api_{date_compact}.jsonl"
    if api_file.exists():
        with open(api_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    chan = row.get("channel")
                    if chan == "REST":
                        summary["api_total_calls"] += 1
                        lat = float(row.get("latency_ms", 0.0))
                        summary["api_latencies"].append(lat)
                        if row.get("rt_cd") not in ("0", "") or row.get("status_code", 200) >= 400:
                            summary["api_failed_calls"] += 1
                    elif chan == "WS":
                        summary["ws_frames_count"] += 1
                except Exception:
                    pass

    if summary["api_latencies"]:
        summary["api_avg_latency_ms"] = round(sum(summary["api_latencies"]) / len(summary["api_latencies"]), 2)
        summary["api_max_latency_ms"] = round(max(summary["api_latencies"]), 2)

    return summary


def run_reconciliation_backtest(
    target_date: str,
    scanner_name: str = "champion_b",
    entry_name: str = "anchor_close",
    exit_name: str = "anchor_morning",
    capital: float = 10_000_000.0,
    data_dir: Optional[str] = None,
) -> Tuple[Optional[Any], List[Any]]:
    """
    Executes backtest for the specified target date using the unified v3 TradingEngine.
    """
    from engine.engine import TradingEngine
    from engine.broker.backtest import BacktestBroker
    from engine.clock import SimulatedClock
    from engine.datafeed.historical import HistoricalDataFeed
    from engine.risk.risk_manager import RiskManager
    from strategy.registry import build_strategy
    from core.telemetry import telemetry

    telemetry.set_audit_enabled(False)

    candidates = [
        data_dir,
        "data/dataset_2y/daily",
        os.path.join("..", "korea-market_v2", "data", "dataset_2y", "daily"),
    ]
    resolved_dir = "data/dataset_2y/daily"
    for p in candidates:
        if p and os.path.isdir(p):
            resolved_dir = p
            break

    strategy = build_strategy(
        scanner_name=scanner_name,
        entry_name=entry_name,
        exit_name=exit_name,
        total_capital=capital,
        ambiguity_mode="pessimistic",
        dynamic=True,
    )
    broker = BacktestBroker(initial_capital=capital)
    clock = SimulatedClock()
    feed = HistoricalDataFeed(
        data_dir=resolved_dir,
        start_date=target_date,
        end_date=target_date,
    )
    risk_manager = RiskManager()
    engine = TradingEngine(
        strategy=strategy,
        broker=broker,
        feed=feed,
        clock=clock,
        risk_manager=risk_manager,
    )
    perf = engine.run()
    return perf, getattr(broker, "completed_trades", [])


def reconcile_daily_trading(
    target_date: Optional[str] = None,
    scanner_name: str = "champion_b",
    entry_name: str = "anchor_close",
    exit_name: str = "anchor_morning",
    strategy_name: Optional[str] = None,
) -> ReconciliationResult:
    """
    Executes comprehensive reconciliation between post-market 1-minute backtest
    and actual live/mock trading execution logs for the given target date.
    """
    if target_date is None:
        target_date = datetime.now().strftime("%Y-%m-%d")
    elif len(target_date) == 8 and target_date.isdigit():
        target_date = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:]}"

    date_compact = target_date.replace("-", "")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Backtest Simulation for today
    summary, bt_trades = run_reconciliation_backtest(
        target_date=target_date,
        scanner_name=scanner_name,
        entry_name=entry_name,
        exit_name=exit_name,
    )
    name_map = get_krx_name_map()
    bt_by_sym: Dict[str, Any] = {t.symbol: t for t in bt_trades}
    bt_has_trade = len(bt_trades) > 0

    # 2. Live Trading Logs for today
    live_events, live_by_sym = parse_live_audit_log(date_compact)
    live_has_trade = len(live_by_sym) > 0

    all_symbols = sorted(list(set(bt_by_sym.keys()) | set(live_by_sym.keys())))
    symbol_details: List[SymbolReconciliationDetail] = []
    root_causes: List[str] = []

    total_bt_pnl = sum(t.net_pnl for t in bt_trades)
    total_live_pnl = sum(t.realized_pnl for t in live_by_sym.values())
    total_pnl_diff = total_live_pnl - total_bt_pnl

    for sym in all_symbols:
        bt_t = bt_by_sym.get(sym)
        live_t = live_by_sym.get(sym)
        sym_name = name_map.get(sym, sym)

        if bt_t and live_t:
            slip_krw = live_t.entry_px - bt_t.entry_price
            slip_pct = (slip_krw / bt_t.entry_price * 100.0) if bt_t.entry_price > 0 else 0.0
            exit_slip_krw = live_t.exit_px - bt_t.exit_price
            exit_slip_pct = (exit_slip_krw / bt_t.exit_price * 100.0) if bt_t.exit_price > 0 else 0.0
            pnl_diff = live_t.realized_pnl - bt_t.net_pnl
            st = "MATCH" if abs(slip_pct) <= 0.5 else "ACCEPTABLE_SLIPPAGE"
            diag = f"동일 종목 진입 일치 (진입 슬리피지: {slip_pct:+.2f}%, 청산 슬리피지: {exit_slip_pct:+.2f}%)"

            bt_in_dt = getattr(bt_t, "entry_dt", getattr(bt_t, "entry_time", None))
            bt_out_dt = getattr(bt_t, "exit_dt", getattr(bt_t, "exit_time", None))
            bt_in_time = bt_in_dt.strftime("%H:%M") if hasattr(bt_in_dt, "strftime") else str(bt_in_dt)[:5]
            bt_out_time = bt_out_dt.strftime("%H:%M") if hasattr(bt_out_dt, "strftime") else str(bt_out_dt)[:5]
            bt_qty = getattr(bt_t, "quantity", getattr(bt_t, "shares", 0))

            symbol_details.append(SymbolReconciliationDetail(
                symbol=sym, name=sym_name, status=st,
                backtest_executed=True, live_executed=True,
                bt_entry_time=bt_in_time, bt_entry_px=bt_t.entry_price,
                bt_shares=bt_qty, bt_exit_time=bt_out_time,
                bt_exit_px=bt_t.exit_price, bt_exit_reason=bt_t.exit_reason, bt_pnl=bt_t.net_pnl,
                live_entry_time=live_t.entry_time, live_entry_px=live_t.entry_px,
                live_shares=live_t.shares, live_exit_time=live_t.exit_time,
                live_exit_px=live_t.exit_px, live_exit_reason=live_t.exit_reason, live_pnl=live_t.realized_pnl,
                entry_slippage_krw=slip_krw, entry_slippage_pct=slip_pct,
                exit_slippage_krw=exit_slip_krw, exit_slippage_pct=exit_slip_pct,
                pnl_diff_krw=pnl_diff,
                diagnosis=diag
            ))
            root_causes.append(f"[{sym} {sym_name}] {diag}")

        elif bt_t and not live_t:
            diag = "백테스트 돌파 체결 vs 실전 스캐너 발견 지연/미선정 또는 안티체이스(+0.5%) 칼라캡 차단"
            bt_in_dt = getattr(bt_t, "entry_dt", getattr(bt_t, "entry_time", None))
            bt_out_dt = getattr(bt_t, "exit_dt", getattr(bt_t, "exit_time", None))
            bt_in_time = bt_in_dt.strftime("%H:%M") if hasattr(bt_in_dt, "strftime") else str(bt_in_dt)[:5]
            bt_out_time = bt_out_dt.strftime("%H:%M") if hasattr(bt_out_dt, "strftime") else str(bt_out_dt)[:5]
            bt_qty = getattr(bt_t, "quantity", getattr(bt_t, "shares", 0))

            symbol_details.append(SymbolReconciliationDetail(
                symbol=sym, name=sym_name, status="BACKTEST_ONLY",
                backtest_executed=True, live_executed=False,
                bt_entry_time=bt_in_time, bt_entry_px=bt_t.entry_price,
                bt_shares=bt_qty, bt_exit_time=bt_out_time,
                bt_exit_px=bt_t.exit_price, bt_exit_reason=bt_t.exit_reason, bt_pnl=bt_t.net_pnl,
                pnl_diff_krw=-bt_t.net_pnl,
                diagnosis=diag
            ))
            root_causes.append(f"[{sym} {sym_name}] 백테스트만 체결: {diag}")

        elif not bt_t and live_t:
            diag = "실전 장중 실시간 틱 돌파 선체결 vs 1분봉 집계 데이터 조건 미달"
            symbol_details.append(SymbolReconciliationDetail(
                symbol=sym, name=sym_name, status="LIVE_ONLY",
                backtest_executed=False, live_executed=True,
                live_entry_time=live_t.entry_time, live_entry_px=live_t.entry_px,
                live_shares=live_t.shares, live_exit_time=live_t.exit_time,
                live_exit_px=live_t.exit_px, live_exit_reason=live_t.exit_reason, live_pnl=live_t.realized_pnl,
                pnl_diff_krw=live_t.realized_pnl,
                diagnosis=diag
            ))
            root_causes.append(f"[{sym} {sym_name}] 실전만 체결: {diag}")

    # Determine Overall Report Status
    if not bt_has_trade and not live_has_trade:
        final_status = "NO_TRADE_MATCH"
        root_causes.append("양측 모두 진입 조건 미발생 (정상 관망 유지)")
    elif all(d.status in ("MATCH", "ACCEPTABLE_SLIPPAGE") for d in symbol_details) and len(symbol_details) > 0:
        final_status = "MATCH"
    elif any(d.status in ("MATCH", "ACCEPTABLE_SLIPPAGE") for d in symbol_details):
        final_status = "ACCEPTABLE_SLIPPAGE"
    else:
        final_status = "DISCREPANCY"

    # Top-level slippage metrics: match symbols by common symbol key
    common_syms = set(bt_by_sym.keys()) & set(live_by_sym.keys())
    matched_sym = sorted(list(common_syms))[0] if common_syms else None
    matched_bt = bt_by_sym.get(matched_sym) if matched_sym else (bt_trades[0] if bt_trades else None)
    matched_live = live_by_sym.get(matched_sym) if matched_sym else (list(live_by_sym.values())[0] if live_by_sym else None)

    entry_slip_krw = (matched_live.entry_px - matched_bt.entry_price) if (matched_sym and matched_live and matched_bt) else 0.0
    entry_slip_pct = ((matched_live.entry_px - matched_bt.entry_price) / matched_bt.entry_price * 100.0) if (matched_sym and matched_live and matched_bt and matched_bt.entry_price > 0) else 0.0
    exit_slip_krw = (matched_live.exit_px - matched_bt.exit_price) if (matched_sym and matched_live and matched_bt) else 0.0
    exit_slip_pct = ((matched_live.exit_px - matched_bt.exit_price) / matched_bt.exit_price * 100.0) if (matched_sym and matched_live and matched_bt and matched_bt.exit_price > 0) else 0.0

    first_bt = bt_trades[0] if bt_trades else None
    first_live = list(live_by_sym.values())[0] if live_by_sym else None

    bt_sym_str = ", ".join(f"{t.symbol}({name_map.get(t.symbol, '')})" for t in bt_trades) if bt_trades else "-"
    live_sym_str = ", ".join(f"{s}({t.name})" for s, t in live_by_sym.items()) if live_by_sym else "-"

    # Telemetry and Execution Diagnostics
    telemetry_data = parse_telemetry_logs(date_compact)
    bt_symbols = set(bt_by_sym.keys())
    live_scanned = telemetry_data["scanner_passed"]
    if bt_symbols and live_scanned:
        scan_match_rate = round(len(bt_symbols & live_scanned) / len(bt_symbols) * 100.0, 1)
    elif not bt_symbols and not live_scanned:
        scan_match_rate = 100.0
    else:
        scan_match_rate = 0.0 if bt_symbols else 100.0

    avg_slip_bp = telemetry_data["avg_slippage_bp"]
    if avg_slip_bp == 0.0 and entry_slip_pct != 0.0:
        avg_slip_bp = round(entry_slip_pct * 100.0, 1)

    avg_exec_lat = telemetry_data["avg_execution_latency_ms"]

    items: List[TradeComparisonItem] = [
        TradeComparisonItem(
            metric="진입 여부 (Trade Executed)",
            backtest_value=f"{len(bt_trades)}건 체결" if bt_has_trade else "미체결 (No Trade)",
            live_value=f"{len(live_by_sym)}건 체결" if live_has_trade else "미체결 (No Trade)",
            difference="일치" if bt_has_trade == live_has_trade else "불일치",
            status="MATCH" if bt_has_trade == live_has_trade else "DIFF",
        ),
        TradeComparisonItem(
            metric="선정 종목 (Symbols)",
            backtest_value=bt_sym_str,
            live_value=live_sym_str,
            difference="일치" if set(bt_by_sym.keys()) == set(live_by_sym.keys()) and bt_has_trade else ("관망 일치" if not bt_has_trade and not live_has_trade else "불일치"),
            status="MATCH" if (set(bt_by_sym.keys()) == set(live_by_sym.keys())) else "DIFF",
        ),
        TradeComparisonItem(
            metric="스캔 종목 일치율 (Scanner Match)",
            backtest_value=f"{len(bt_symbols)}종목 감시대상" if bt_symbols else "0종목",
            live_value=f"{len(live_scanned)}종목 발굴/통과" if live_scanned else f"{len(telemetry_data['scanner_evaluated'])}종목 평가",
            difference=f"일치율 {scan_match_rate:.1f}%",
            status="MATCH" if scan_match_rate >= 80.0 else "DIFF",
        ),
        TradeComparisonItem(
            metric="체결 슬리피지 (Execution Slippage)",
            backtest_value="0.0 bp (모델 기준)",
            live_value=f"{avg_slip_bp:+.1f} bp" if (live_has_trade or avg_slip_bp != 0.0) else "0.0 bp",
            difference=f"{avg_slip_bp:+.1f} bp" if (live_has_trade or avg_slip_bp != 0.0) else "0.0 bp",
            status="MATCH" if abs(avg_slip_bp) <= 25.0 else "SLIPPAGE",
        ),
        TradeComparisonItem(
            metric="주문 체결 지연 (Execution Latency)",
            backtest_value="0.0 ms (시뮬레이션)",
            live_value=f"{avg_exec_lat:.1f} ms" if avg_exec_lat > 0 else "N/A",
            difference=f"{avg_exec_lat:.1f} ms" if avg_exec_lat > 0 else "N/A",
            status="MATCH" if (avg_exec_lat == 0 or avg_exec_lat < 500.0) else "INFO",
        ),
        TradeComparisonItem(
            metric="실현 순손익 (Net PnL)",
            backtest_value=f"{total_bt_pnl:+,.0f}원" if bt_has_trade else "0원",
            live_value=f"{total_live_pnl:+,.0f}원" if live_has_trade else "0원",
            difference=f"{total_pnl_diff:+,.0f}원",
            status="MATCH" if abs(total_pnl_diff) < 10_000 else "DIFF",
        ),
    ]

    if telemetry_data["api_total_calls"] > 0:
        items.append(TradeComparisonItem(
            metric="REST API 호출 성능 (Latency)",
            backtest_value="N/A",
            live_value=f"총 {telemetry_data['api_total_calls']}건 (평균 {telemetry_data['api_avg_latency_ms']:.1f}ms, 최대 {telemetry_data['api_max_latency_ms']:.1f}ms)",
            difference=f"실패 {telemetry_data['api_failed_calls']}건",
            status="MATCH" if telemetry_data["api_failed_calls"] == 0 else "DIFF",
        ))

    res = ReconciliationResult(
        target_date=target_date,
        reconciled_at=now_str,
        backtest_executed=bt_has_trade,
        live_executed=live_has_trade,
        status=final_status,
        backtest_symbol=matched_bt.symbol if matched_bt else (first_bt.symbol if first_bt else None),
        live_symbol=matched_live.symbol if matched_live else (first_live.symbol if first_live else None),
        symbol_match=bool(matched_sym is not None),
        backtest_pnl=total_bt_pnl,
        live_pnl=total_live_pnl,
        pnl_diff_krw=total_pnl_diff,
        backtest_entry_px=matched_bt.entry_price if (matched_sym and matched_bt) else (first_bt.entry_price if first_bt else 0.0),
        live_entry_px=matched_live.entry_px if (matched_sym and matched_live) else (first_live.entry_px if first_live else 0.0),
        entry_slippage_krw=entry_slip_krw,
        entry_slippage_pct=entry_slip_pct,
        exit_slippage_krw=exit_slip_krw,
        exit_slippage_pct=exit_slip_pct,
        backtest_exit_reason=matched_bt.exit_reason if (matched_sym and matched_bt) else (first_bt.exit_reason if first_bt else "N/A"),
        live_exit_reason=matched_live.exit_reason if (matched_sym and matched_live) else (first_live.exit_reason if first_live else "N/A"),
        exit_reason_match=bool(matched_sym and matched_bt and matched_live and matched_bt.exit_reason == matched_live.exit_reason),
        root_causes=root_causes,
        items=items,
        symbol_details=symbol_details,
        raw_backtest_records=[t.__dict__ if hasattr(t, "__dict__") else str(t) for t in bt_trades],
        raw_live_trades={s: asdict(t) for s, t in live_by_sym.items()},
        scanner_match_rate_pct=scan_match_rate,
        avg_slippage_bp=avg_slip_bp,
        avg_execution_latency_ms=avg_exec_lat,
        telemetry_summary=telemetry_data,
    )

    save_reconciliation_reports(res, date_compact)
    return res


def save_reconciliation_reports(res: ReconciliationResult, date_compact: str) -> Tuple[Path, Path]:
    """Saves structured reconciliation audit logs (.json and .md)."""
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    json_path = settings.logs_dir / f"reconciliation_{date_compact}.json"
    md_path = settings.logs_dir / f"reconciliation_{date_compact}.md"

    # 1. JSON Report
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(asdict(res), f, ensure_ascii=False, indent=2, default=str)

    # 2. Markdown Report
    status_icon = "✅" if res.status in ("MATCH", "NO_TRADE_MATCH") else ("⚠️" if res.status == "ACCEPTABLE_SLIPPAGE" else "❌")
    md_lines = [
        f"# 📊 [{res.target_date}] 백테스트 vs 실전매매 정밀 대사 리포트",
        f"- **대사 시각**: {res.reconciled_at}",
        f"- **최종 판정**: {status_icon} `{res.status}`",
        "",
        "## 1. 포트폴리오 종합 대사 요약",
        "| 비교 지표 (Metric) | 백테스트 (Backtest) | 실전/모의매매 (Live) | 차이 (Difference) | 판정 |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for it in res.items:
        md_lines.append(f"| {it.metric} | {it.backtest_value} | {it.live_value} | {it.difference} | {it.status} |")

    if res.symbol_details:
        md_lines.extend([
            "",
            "## 2. 종목별 정밀 비교표 (Per-Symbol Execution Breakdown)",
            "| 종목코드 / 종목명 | 구분 | 진입시각 | 진입가격 | 청산시각 | 청산사유 | 실현손익 | 판정 및 진단 |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ])
        for d in res.symbol_details:
            md_lines.append(
                f"| **{d.symbol}** ({d.name}) | **백테스트** | {d.bt_entry_time or '-'} | {d.bt_entry_px:,.0f}원 | {d.bt_exit_time or '-'} | {d.bt_exit_reason} | {d.bt_pnl:+,.0f}원 | {d.status} |"
            )
            md_lines.append(
                f"| | **실전매매** | {d.live_entry_time or '-'} | {d.live_entry_px:,.0f}원 | {d.live_exit_time or '-'} | {d.live_exit_reason} | {d.live_pnl:+,.0f}원 | {d.diagnosis} |"
            )

    md_lines.extend([
        "",
        "## 3. 괴리 원인 및 정밀 진단 (Root Cause Diagnosis)",
    ])
    for c in res.root_causes:
        md_lines.append(f"- {c}")

    md_lines.extend([
        "",
        "## 4. 최종 손익 통계",
        f"- **백테스트 총손익**: {res.backtest_pnl:+,.0f}원",
        f"- **실전 실현 총손익**: {res.live_pnl:+,.0f}원",
        f"- **손익 괴리금액**: {res.pnl_diff_krw:+,.0f}원",
        "",
        "---",
        "*Report auto-generated by Korea Market v3 Reconciliation Engine*",
    ])

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    return json_path, md_path


def display_reconciliation_table(res: ReconciliationResult) -> None:
    """Prints a styled Rich terminal dashboard of the reconciliation audit."""
    status_color = "green" if res.status in ("MATCH", "NO_TRADE_MATCH") else ("yellow" if res.status == "ACCEPTABLE_SLIPPAGE" else "red")
    console.print(Panel.fit(
        f"[bold {status_color}]⚖️ [RECONCILIATION] {res.target_date} 백테스트 vs 실전매매 정밀 대사 결과[/bold {status_color}]\n"
        f"[dim]대사 일시: {res.reconciled_at} | 대사 상태: [bold {status_color}]{res.status}[/bold {status_color}][/dim]",
        border_style=status_color,
    ))

    # Overall Summary Table
    table = Table(title=f"📋 [{res.target_date}] 포트폴리오 종합 대사표", border_style="cyan")
    table.add_column("비교 항목", style="bold white", width=24)
    table.add_column("백테스트 예측 (Backtest)", style="cyan", justify="right", width=30)
    table.add_column("실전/모의 실체결 (Live)", style="magenta", justify="right", width=30)
    table.add_column("차이 / 괴리율 (Difference)", style="yellow", justify="right", width=22)
    table.add_column("상태", style="bold", justify="center", width=8)

    for it in res.items:
        color = "green" if it.status == "MATCH" else ("yellow" if it.status == "SLIPPAGE" else ("cyan" if it.status == "INFO" else "red"))
        table.add_row(it.metric, str(it.backtest_value), str(it.live_value), it.difference, f"[{color}]{it.status}[/{color}]")

    console.print(table)

    # Per-Symbol Table if multiple symbols exist
    if res.symbol_details:
        sym_table = Table(title="🔍 종목별 정밀 대사 내역 (Per-Symbol Execution Breakdown)", border_style="blue")
        sym_table.add_column("종목", style="bold white", width=18)
        sym_table.add_column("구분", style="dim", width=10)
        sym_table.add_column("진입 (시각/가격)", style="cyan", justify="right", width=20)
        sym_table.add_column("청산 (시각/사유)", style="magenta", justify="right", width=22)
        sym_table.add_column("실현순손익", style="yellow", justify="right", width=16)
        sym_table.add_column("판정 및 진단", style="white", width=30)

        for d in res.symbol_details:
            sym_label = f"{d.symbol}\n({d.name})"
            bt_in = f"{d.bt_entry_time or '-'} / {d.bt_entry_px:,.0f}원" if d.backtest_executed else "미체결"
            bt_out = f"{d.bt_exit_time or '-'} / {d.bt_exit_reason}" if d.backtest_executed else "N/A"
            bt_pnl_str = f"{d.bt_pnl:+,.0f}원" if d.backtest_executed else "-"

            live_in = f"{d.live_entry_time or '-'} / {d.live_entry_px:,.0f}원" if d.live_executed else "미체결"
            live_out = f"{d.live_exit_time or '-'} / {d.live_exit_reason}" if d.live_executed else "N/A"
            live_pnl_str = f"{d.live_pnl:+,.0f}원" if d.live_executed else "-"

            sym_table.add_row(sym_label, "[cyan]백테스트[/cyan]", bt_in, bt_out, bt_pnl_str, d.status)
            sym_table.add_row("", "[magenta]실전체결[/magenta]", live_in, live_out, live_pnl_str, d.diagnosis)
            sym_table.add_section()

        console.print(sym_table)

    console.print("\n[bold yellow]🔍 [원인 진단 및 평가][/bold yellow]")
    for cause in res.root_causes:
        console.print(f"  • {cause}")

    console.print(f"\n[dim]📁 감사 리포트 저장 완료: logs/reconciliation_{res.target_date.replace('-', '')}.json 및 .md[/dim]\n")


def main():
    parser = argparse.ArgumentParser(description="Korea Market v3 - Backtest vs Live Reconciliation")
    parser.add_argument("--date", type=str, default=datetime.now().strftime("%Y%m%d"), help="Target date YYYYMMDD")
    parser.add_argument("--scanner", type=str, default="champion_b", help="Scanner module (default: champion_b)")
    parser.add_argument("--entry", type=str, default="anchor_close", help="Entry module (default: anchor_close)")
    parser.add_argument("--exit", type=str, default="anchor_morning", help="Exit module (default: anchor_morning)")
    args = parser.parse_args()

    result = reconcile_daily_trading(
        target_date=args.date,
        scanner_name=args.scanner,
        entry_name=args.entry,
        exit_name=args.exit,
    )
    display_reconciliation_table(result)


if __name__ == "__main__":
    main()
