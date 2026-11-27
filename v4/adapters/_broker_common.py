"""
Shared pure broker helpers for Korea Market v4 (Backtest & KIS Live adapters).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Dict, Iterable, Optional, Tuple

from v4.interfaces import ExecutionCallback
from v4.models import OrderEvent, OrderRequest, OrderStatus, Position


def emit_order_event(
    listeners: Iterable[ExecutionCallback],
    ev: OrderEvent,
    logger: logging.Logger,
) -> None:
    """Dispatches an `OrderEvent` to registered sync or async execution callbacks."""
    for cb in listeners:
        try:
            res = cb(ev)
            if asyncio.iscoroutine(res):
                asyncio.get_running_loop().create_task(res)
        except Exception as exc:
            logger.error("Execution callback error: %s", exc, exc_info=True)


def update_position_mtm(
    positions: Dict[str, Position],
    symbol: str,
    price: float,
    dt: Optional[datetime] = None,
    *,
    high: Optional[float] = None,
    low: Optional[float] = None,
) -> None:
    """Updates `current_price`, `high_water_mark`, and `low_water_mark` for an open position."""
    _ = dt
    pos = positions.get(symbol)
    if pos is None:
        return
    hi = price if high is None else high
    lo = price if low is None else low
    pos.current_price = price
    if hi > pos.high_water_mark:
        pos.high_water_mark = hi
    if pos.low_water_mark <= 0.0 or lo < pos.low_water_mark:
        pos.low_water_mark = lo


def make_reject_event(
    req: OrderRequest,
    dt: datetime,
    reason: str,
    *,
    order_id: str = "",
    requested_price: Optional[float] = None,
) -> OrderEvent:
    """Constructs a canonical `REJECTED` `OrderEvent` from an `OrderRequest`."""
    px = float(req.price or 0.0) if requested_price is None else float(requested_price)
    return OrderEvent(
        order_id=order_id or req.client_order_id,
        symbol=req.symbol,
        side=req.side,
        status=OrderStatus.REJECTED,
        filled_price=0.0,
        filled_quantity=0,
        requested_price=px,
        requested_quantity=req.quantity,
        dt=dt,
        submitted_at=dt,
        client_order_id=req.client_order_id,
        message=reason,
        tag=req.tag,
    )


def compute_sell_net_pnl(
    avg_price: float,
    exec_price: float,
    qty: int,
    commission_rate: float,
    tax_rate: float,
) -> Tuple[float, float, float, float]:
    """Returns `(gross_pnl, commission, tax, net_pnl)` for a SELL fill (including entry buy commission)."""
    sell_amt = exec_price * qty
    commission = sell_amt * commission_rate
    tax = sell_amt * tax_rate
    buy_comm = avg_price * qty * commission_rate
    gross_pnl = (exec_price - avg_price) * qty
    net_pnl = gross_pnl - commission - tax - buy_comm
    return gross_pnl, commission, tax, net_pnl
