"""
Deterministic Backtest Broker Adapter for Korea Market v4.
Implements `IBrokerPort` with exact v3 `BacktestBroker` cost, slippage,
KRX tick rounding, shared mutable `Position`, and `OrderEvent.net_pnl` parity.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional

from core.krx_ticks import round_to_krx_tick
from v4.adapters._broker_common import (
    compute_sell_net_pnl,
    emit_order_event,
    make_reject_event,
    update_position_mtm,
)
from v4.interfaces import ExecutionCallback, IBrokerPort
from v4.models import (
    AccountBalance,
    ExecutionMode,
    MinuteBar,
    OrderEvent,
    OrderRequest,
    OrderSide,
    OrderStatus,
    Position,
    TickEvent,
    TradeRecord,
)

logger = logging.getLogger("korea_market.v4.adapters.backtest.broker")


class BacktestBrokerAdapter(IBrokerPort):
    """Deterministic backtest execution broker matching v3 `BacktestBroker` math."""

    def __init__(
        self,
        initial_capital: float = 10_000_000.0,
        commission_rate: float = 0.00014,
        tax_rate: float = 0.0018,
        slippage_rate: float = 0.00035,
        execution_mode: ExecutionMode = "pessimistic",
    ) -> None:
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)
        self.commission_rate = float(commission_rate)
        self.tax_rate = float(tax_rate)
        self.slippage_rate = float(slippage_rate)
        self.execution_mode: ExecutionMode = execution_mode

        self.positions: Dict[str, Position] = {}
        self.pending_orders: Dict[str, OrderEvent] = {}
        self.latest_prices: Dict[str, float] = {}
        self.current_dt: Optional[datetime] = None
        self.completed_trades: List[TradeRecord] = []
        self._trade_id_counter: int = 1
        self._order_seq: int = 1
        self._callbacks: List[ExecutionCallback] = []

    def on_execution_event(self, cb: ExecutionCallback) -> None:
        self._callbacks.append(cb)

    def _emit_event(self, event: OrderEvent) -> None:
        emit_order_event(self._callbacks, event, logger)

    def set_execution_mode(self, mode: ExecutionMode) -> None:
        self.execution_mode = mode

    def get_balance(self) -> AccountBalance:
        equity = self.cash + sum(p.current_price * p.quantity for p in self.positions.values())
        return AccountBalance(
            total_equity=equity,
            cash=self.cash,
            positions=dict(self.positions),
            initial_capital=self.initial_capital,
        )

    def get_positions(self) -> Dict[str, Position]:
        return dict(self.positions)

    def get_position(self, symbol: str) -> Optional[Position]:
        return self.positions.get(symbol)

    def get_pending_orders(self) -> List[OrderEvent]:
        return list(self.pending_orders.values())

    def get_completed_trades(self) -> List[TradeRecord]:
        return list(self.completed_trades)

    def on_tick(self, tick: TickEvent) -> None:
        self.latest_prices[tick.symbol] = tick.price
        self.current_dt = tick.dt
        update_position_mtm(self.positions, tick.symbol, tick.price, tick.dt)

    def on_bar(self, bar: MinuteBar) -> None:
        self.latest_prices[bar.symbol] = bar.close
        self.current_dt = bar.dt
        update_position_mtm(self.positions, bar.symbol, bar.close, bar.dt, high=bar.high, low=bar.low)

    def _reject(self, order_id: str, req: OrderRequest, dt: datetime, reason: str) -> OrderEvent:
        ev = make_reject_event(req, dt, reason, order_id=order_id)
        self._emit_event(ev)
        return ev

    async def submit_order(self, req: OrderRequest) -> OrderEvent:
        oid = req.client_order_id or f"ord_{self._order_seq}"
        self._order_seq += 1
        ev_dt = req.created_at or self.current_dt or datetime.now()

        base_price = float(req.price) if (req.price is not None and req.price > 0.0) else self.latest_prices.get(req.symbol, 0.0)
        if base_price <= 0.0 or req.quantity <= 0:
            return self._reject(oid, req, ev_dt, "REJECTED_ZERO_PRICE")

        if req.side == OrderSide.BUY:
            raw_exec = base_price * (1.0 + self.slippage_rate)
            exec_price = float(round_to_krx_tick(raw_exec, side="BUY", round_mode="ceil"))
            fill_qty = req.quantity
        else:
            pos = self.positions.get(req.symbol)
            if pos is None or pos.quantity <= 0:
                return self._reject(oid, req, ev_dt, "REJECTED_NO_POSITION")
            raw_exec = base_price * (1.0 - self.slippage_rate)
            exec_price = float(round_to_krx_tick(raw_exec, side="SELL", round_mode="floor"))
            fill_qty = min(req.quantity, pos.quantity)

        trade_amount = exec_price * fill_qty
        commission = trade_amount * self.commission_rate
        tax = trade_amount * self.tax_rate if req.side == OrderSide.SELL else 0.0
        total_cost = trade_amount + commission + tax

        if req.side == OrderSide.BUY and self.cash < total_cost:
            return self._reject(oid, req, ev_dt, "REJECTED_INSUFFICIENT_FUNDS")

        net_pnl = 0.0
        if req.side == OrderSide.BUY:
            self.cash -= total_cost
            pos = self.positions.get(req.symbol)
            if pos is None:
                self.positions[req.symbol] = Position(
                    symbol=req.symbol,
                    quantity=fill_qty,
                    avg_price=exec_price,
                    current_price=exec_price,
                    high_water_mark=exec_price,
                    low_water_mark=exec_price,
                    entry_dt=ev_dt,
                )
            else:
                new_qty = pos.quantity + fill_qty
                pos.avg_price = (pos.avg_price * pos.quantity + trade_amount) / new_qty
                pos.quantity = new_qty
                pos.current_price = exec_price
                pos.high_water_mark = max(pos.high_water_mark, exec_price)
                if pos.low_water_mark <= 0.0 or exec_price < pos.low_water_mark:
                    pos.low_water_mark = exec_price
        else:
            pos = self.positions[req.symbol]
            self.cash += trade_amount - commission - tax
            gross_pnl, commission, tax, net_pnl = compute_sell_net_pnl(
                pos.avg_price, exec_price, fill_qty, self.commission_rate, self.tax_rate
            )
            ret_pct = (exec_price - pos.avg_price) / pos.avg_price if pos.avg_price > 0.0 else 0.0
            entry_dt = pos.entry_dt or ev_dt
            holding_sec = (ev_dt - entry_dt).total_seconds()
            mfe_pct = (pos.high_water_mark - pos.avg_price) / pos.avg_price if pos.avg_price > 0.0 else 0.0
            mae_pct = (pos.low_water_mark - pos.avg_price) / pos.avg_price if pos.avg_price > 0.0 else 0.0
            self.completed_trades.append(
                TradeRecord(
                    trade_id=self._trade_id_counter,
                    symbol=req.symbol,
                    entry_dt=entry_dt,
                    exit_dt=ev_dt,
                    entry_price=pos.avg_price,
                    exit_price=exec_price,
                    quantity=fill_qty,
                    side="BUY",
                    gross_pnl=gross_pnl,
                    net_pnl=net_pnl,
                    return_pct=ret_pct,
                    holding_seconds=holding_sec,
                    exit_reason=req.tag or "SELL",
                    mfe_pct=mfe_pct,
                    mae_pct=mae_pct,
                )
            )
            self._trade_id_counter += 1
            if fill_qty >= pos.quantity:
                self.positions.pop(req.symbol, None)
            else:
                pos.quantity -= fill_qty

        ev = OrderEvent(
            order_id=oid,
            symbol=req.symbol,
            side=req.side,
            status=OrderStatus.FILLED,
            filled_price=exec_price,
            filled_quantity=fill_qty,
            unfilled_quantity=0,
            cum_filled_quantity=fill_qty,
            requested_price=float(req.price or 0.0),
            requested_quantity=req.quantity,
            dt=ev_dt,
            submitted_at=ev_dt,
            commission=commission,
            tax=tax,
            net_pnl=net_pnl,
            client_order_id=req.client_order_id,
            message="FILLED",
            tag=req.tag,
        )
        self._emit_event(ev)
        return ev

    async def cancel_order(self, order_id: str, symbol: str = "") -> bool:
        pend = self.pending_orders.pop(order_id, None)
        if pend is None:
            return False
        ev = OrderEvent(
            order_id=pend.order_id,
            symbol=pend.symbol,
            side=pend.side,
            status=OrderStatus.CANCELLED,
            cum_filled_quantity=pend.cum_filled_quantity,
            dt=self.current_dt or datetime.now(),
            submitted_at=pend.submitted_at,
            client_order_id=pend.client_order_id,
            message="CANCELLED",
        )
        self._emit_event(ev)
        return True

    async def cancel_pending(self, symbol: Optional[str] = None) -> List[str]:
        ids = [oid for oid, ev in self.pending_orders.items() if symbol is None or ev.symbol == symbol]
        for oid in ids:
            await self.cancel_order(oid, symbol or "")
        return ids

    async def sync_open_orders(self) -> List[OrderEvent]:
        return []
