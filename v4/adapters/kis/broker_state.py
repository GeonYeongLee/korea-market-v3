"""
Broker Position, Balance Reconciliation & Idempotent Order Fill Tracker for Korea Market v4.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from v4.adapters._broker_common import compute_sell_net_pnl
from v4.adapters.kis._parsers import parse_optional_float
from v4.models import AccountBalance, OrderSide, Position, TradeRecord


def apply_fill_state(
    positions: Dict[str, Position],
    completed_trades: List[TradeRecord],
    trade_seq: int,
    latest_balance: Optional[AccountBalance],
    initial_capital: float,
    commission_rate: float,
    tax_rate: float,
    symbol: str,
    qty: int,
    price: float,
    side: OrderSide,
    dt: datetime,
) -> Tuple[int, AccountBalance, float, float, float]:
    """Mutates `positions`/`completed_trades` on fill; returns `(trade_seq, balance, comm, tax, net_pnl)`."""
    comm = price * qty * commission_rate
    tax = (price * qty * tax_rate) if side == OrderSide.SELL else 0.0
    net_pnl = 0.0

    if side == OrderSide.BUY:
        pos = positions.get(symbol)
        if pos is None:
            positions[symbol] = Position(
                symbol=symbol, quantity=qty, avg_price=price, current_price=price,
                high_water_mark=price, low_water_mark=price, entry_dt=dt,
            )
        else:
            tot_qty = pos.quantity + qty
            pos.avg_price = ((pos.avg_price * pos.quantity) + (price * qty)) / tot_qty if tot_qty > 0 else price
            pos.quantity = tot_qty
            pos.current_price = price
            pos.high_water_mark = max(pos.high_water_mark, price)
            pos.low_water_mark = min(pos.low_water_mark, price) if pos.low_water_mark > 0.0 else price
    elif side == OrderSide.SELL and symbol in positions:
        pos = positions[symbol]
        sell_qty = min(qty, pos.quantity) if pos.quantity > 0 else qty
        gross, comm, tax, net_pnl = compute_sell_net_pnl(pos.avg_price, price, sell_qty, commission_rate, tax_rate)
        entry_dt = pos.entry_dt or dt
        completed_trades.append(
            TradeRecord(
                trade_id=trade_seq, symbol=symbol, entry_dt=entry_dt, exit_dt=dt,
                entry_price=pos.avg_price, exit_price=price, quantity=sell_qty, side="SELL",
                gross_pnl=gross, net_pnl=net_pnl,
                return_pct=((price - pos.avg_price) / pos.avg_price) if pos.avg_price > 0.0 else 0.0,
                holding_seconds=(dt - entry_dt).total_seconds(), exit_reason="LIVE_SELL",
            )
        )
        trade_seq += 1
        pos.quantity = max(0, pos.quantity - qty)
        if pos.quantity == 0:
            positions.pop(symbol, None)

    cash = latest_balance.cash if latest_balance is not None else initial_capital
    cash = max(0.0, cash - price * qty - comm) if side == OrderSide.BUY else (cash + price * qty - comm - tax)
    pos_val = sum(p.current_price * p.quantity for p in positions.values())
    balance = AccountBalance(
        total_equity=cash + pos_val, cash=cash, positions=dict(positions), initial_capital=initial_capital,
    )
    return trade_seq, balance, comm, tax, net_pnl


def reconcile_balance_state(
    positions: Dict[str, Position],
    bal: Optional[Dict[str, Any]],
    initial_capital: float,
    now_dt: datetime,
) -> AccountBalance:
    """Reconciles `get_balance()` holdings (`output1`) and cash/equity (`output2`) into `positions`."""
    holdings = (bal or {}).get("holdings", []) or []
    summary = (bal or {}).get("summary", {}) or {}

    cash_val = parse_optional_float(summary.get("ord_psbl_cash"))
    if cash_val is None:
        cash_val = parse_optional_float(summary.get("dnca_tot_amt"))
    cash = cash_val if cash_val is not None else initial_capital

    eq_val = parse_optional_float(summary.get("tot_evlu_amt"))
    equity = eq_val if eq_val is not None else cash

    seen: set[str] = set()
    for item in holdings:
        qty = int(float(item.get("hldg_qty") or item.get("hld_qty") or 0))
        code = str(item.get("pdno") or "").strip()
        if not code or qty <= 0:
            continue
        seen.add(code)
        avg_p = float(item.get("pchs_avg_pric") or 0.0)
        cur_p = float(item.get("prpr") or avg_p)
        if code in positions:
            pos = positions[code]
            pos.quantity = qty
            pos.avg_price = avg_p
            pos.current_price = cur_p
            pos.high_water_mark = max(pos.high_water_mark, cur_p)
            pos.low_water_mark = min(pos.low_water_mark, cur_p) if pos.low_water_mark > 0.0 else cur_p
        else:
            positions[code] = Position(
                symbol=code, quantity=qty, avg_price=avg_p, current_price=cur_p,
                high_water_mark=cur_p, low_water_mark=cur_p, entry_dt=now_dt,
            )

    for code in list(positions.keys()):
        if code not in seen:
            positions.pop(code, None)

    return AccountBalance(
        total_equity=equity, cash=cash, positions=dict(positions), initial_capital=initial_capital,
    )


def build_fill_event(
    order_id: str,
    symbol: str,
    side: OrderSide,
    status: Any,
    price: float,
    delta_qty: int,
    unfilled_qty: int,
    cum_qty: int,
    req_price: float,
    req_qty: int,
    cum_comm: float,
    cum_tax: float,
    cum_pnl: float,
    dt: datetime,
    msg: str,
    client_order_id: str = "",
    tag: str = "",
) -> Any:
    from v4.models import OrderEvent

    return OrderEvent(
        order_id=order_id, symbol=symbol, side=side, status=status,
        filled_price=price, filled_quantity=delta_qty, unfilled_quantity=unfilled_qty,
        cum_filled_quantity=cum_qty, requested_price=req_price, requested_quantity=req_qty,
        commission=cum_comm, tax=cum_tax, net_pnl=cum_pnl, dt=dt, submitted_at=dt,
        client_order_id=client_order_id, message=msg, tag=tag,
    )
