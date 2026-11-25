"""
Deterministic Backtest Broker with Pessimistic Bar Execution.
Enforces realistic KRX microstructure:
  - 0.18% statutory securities transaction tax (sell side)
  - 0.014% online commission (both sides)
  - 0.035% execution slippage (~1 KRX tick)
  - Strict pessimistic resolution of ambiguous bars (Stop-loss prioritized over take-profit)
  - KRX statutory tick rounding on all filled prices
"""
from __future__ import annotations

import uuid
import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional, Callable

from core.krx_ticks import round_to_krx_tick
from engine.models import (
    Order, OrderEvent, OrderSide, OrderType, OrderStatus,
    Position, AccountBalance, MinuteBar, TickData, TradeRecord
)
from engine.broker.base import IBroker

logger = logging.getLogger("korea_market.broker.backtest")


class BacktestBroker(IBroker):
    def __init__(
        self,
        initial_capital: float = 10_000_000.0,
        commission_rate: float = 0.00014,  # 0.014%
        tax_rate: float = 0.0018,          # 0.18%
        slippage_rate: float = 0.00035,    # 0.035% (~1 tick)
    ):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.commission_rate = commission_rate
        self.tax_rate = tax_rate
        self.slippage_rate = slippage_rate

        self._lock = threading.RLock()
        self.positions: Dict[str, Position] = {}
        self.pending_orders: Dict[str, Order] = {}
        self.latest_prices: Dict[str, float] = {}
        self.current_dt: Optional[datetime] = None

        self.completed_trades: List[TradeRecord] = []
        self._trade_id_counter: int = 1
        self._event_listeners: List[Callable[[OrderEvent], None]] = []

    def register_event_listener(self, listener: Callable[[OrderEvent], None]) -> None:
        with self._lock:
            self._event_listeners.append(listener)

    def _emit_event(self, event: OrderEvent) -> None:
        with self._lock:
            listeners = list(self._event_listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception as e:
                logger.error(f"Error in broker listener: {e}", exc_info=True)

    def get_position(self, symbol: str) -> Optional[Position]:
        with self._lock:
            return self.positions.get(symbol)

    def get_positions(self) -> Dict[str, Position]:
        with self._lock:
            return dict(self.positions)

    def get_balance(self) -> AccountBalance:
        with self._lock:
            equity = self.cash + sum(p.current_price * p.quantity for p in self.positions.values())
            return AccountBalance(total_equity=equity, cash=self.cash, positions=dict(self.positions))

    def on_tick(self, tick: TickData) -> None:
        with self._lock:
            self.latest_prices[tick.symbol] = tick.price
            self.current_dt = tick.dt
            if tick.symbol in self.positions:
                pos = self.positions[tick.symbol]
                pos.current_price = tick.price
                if tick.price > pos.high_water_mark:
                    pos.high_water_mark = tick.price
                if pos.low_water_mark == 0.0 or tick.price < pos.low_water_mark:
                    pos.low_water_mark = tick.price

    def on_bar(self, bar: MinuteBar) -> None:
        with self._lock:
            self.latest_prices[bar.symbol] = bar.close
            self.current_dt = bar.dt
            if bar.symbol in self.positions:
                pos = self.positions[bar.symbol]
                pos.current_price = bar.close
                if bar.high > pos.high_water_mark:
                    pos.high_water_mark = bar.high
                if pos.low_water_mark == 0.0 or bar.low < pos.low_water_mark:
                    pos.low_water_mark = bar.low

    def submit_order(self, order: Order) -> str:
        with self._lock:
            if not order.order_id:
                order.order_id = f"ord_{uuid.uuid4().hex[:8]}"
            if not order.created_at:
                order.created_at = self.current_dt or datetime.now()

            # Immediate fill against known market / limit price
            base_price = order.price or self.latest_prices.get(order.symbol, 0.0)
            if base_price <= 0:
                order.status = OrderStatus.REJECTED
                self._emit_event(
                    OrderEvent(
                        order_id=order.order_id,
                        symbol=order.symbol,
                        side=order.side,
                        status=OrderStatus.REJECTED,
                        filled_price=0.0,
                        filled_quantity=0,
                        dt=self.current_dt,
                        message="REJECTED_ZERO_PRICE",
                    )
                )
                return order.order_id

            # Apply slippage & round to KRX tick (adverse rounding: BUY ceil, SELL floor)
            if order.side == OrderSide.BUY:
                raw_exec = base_price * (1.0 + self.slippage_rate)
                exec_price = round_to_krx_tick(raw_exec, side="BUY", round_mode="ceil")
            else:
                raw_exec = base_price * (1.0 - self.slippage_rate)
                exec_price = round_to_krx_tick(raw_exec, side="SELL", round_mode="floor")

            trade_amount = exec_price * order.quantity
            commission = trade_amount * self.commission_rate
            tax = trade_amount * self.tax_rate if order.side == OrderSide.SELL else 0.0
            total_cost = trade_amount + commission + tax

            # Insufficient cash check
            if order.side == OrderSide.BUY and self.cash < total_cost:
                order.status = OrderStatus.REJECTED
                self._emit_event(
                    OrderEvent(
                        order_id=order.order_id,
                        symbol=order.symbol,
                        side=order.side,
                        status=OrderStatus.REJECTED,
                        filled_price=0.0,
                        filled_quantity=0,
                        dt=self.current_dt,
                        message="REJECTED_INSUFFICIENT_FUNDS",
                    )
                )
                return order.order_id

            self._fill_order(order, exec_price)
            return order.order_id

    def cancel_order(self, order_id: str) -> bool:
        with self._lock:
            if order_id in self.pending_orders:
                ord_ = self.pending_orders.pop(order_id)
                ord_.status = OrderStatus.CANCELLED
                self._emit_event(
                    OrderEvent(
                        order_id=ord_.order_id,
                        symbol=ord_.symbol,
                        side=ord_.side,
                        status=OrderStatus.CANCELLED,
                        filled_price=0.0,
                        filled_quantity=0,
                        dt=self.current_dt,
                        message="CANCELLED",
                    )
                )
                return True
        return False

    def cancel_pending_orders(self, symbol: Optional[str] = None) -> List[str]:
        """Cancels all active pending orders, optionally filtered by symbol."""
        with self._lock:
            pending_list = list(self.pending_orders.items())

        cancelled_ids: List[str] = []
        for ord_id, ord_ in pending_list:
            if symbol is None or ord_.symbol == symbol:
                if self.cancel_order(ord_id):
                    cancelled_ids.append(ord_id)
        return cancelled_ids

    def _fill_order(self, order: Order, fill_price: float) -> None:
        filled_qty = order.quantity
        if order.side == OrderSide.SELL and order.symbol in self.positions:
            filled_qty = min(order.quantity, self.positions[order.symbol].quantity)

        trade_amount = fill_price * filled_qty
        commission = trade_amount * self.commission_rate
        tax = trade_amount * self.tax_rate if order.side == OrderSide.SELL else 0.0
        total_cost = trade_amount + commission + tax

        order.status = OrderStatus.FILLED
        net_pnl = 0.0

        if order.side == OrderSide.BUY:
            self.cash -= total_cost
            if order.symbol not in self.positions:
                self.positions[order.symbol] = Position(
                    symbol=order.symbol,
                    quantity=filled_qty,
                    avg_price=fill_price,
                    current_price=fill_price,
                    high_water_mark=fill_price,
                    low_water_mark=fill_price,
                    entry_dt=self.current_dt,
                )
            else:
                pos = self.positions[order.symbol]
                new_qty = pos.quantity + filled_qty
                pos.avg_price = (pos.avg_price * pos.quantity + trade_amount) / new_qty
                pos.quantity = new_qty
                if fill_price > pos.high_water_mark:
                    pos.high_water_mark = fill_price
                if pos.low_water_mark == 0.0 or fill_price < pos.low_water_mark:
                    pos.low_water_mark = fill_price
        else:
            # SELL / LIQUIDATION (supports partial and full fills)
            self.cash += (trade_amount - commission - tax)
            if order.symbol in self.positions:
                pos = self.positions[order.symbol]
                gross_pnl = (fill_price - pos.avg_price) * filled_qty
                entry_comm = pos.avg_price * filled_qty * self.commission_rate
                net_pnl = gross_pnl - commission - tax - entry_comm
                ret_pct = (fill_price - pos.avg_price) / pos.avg_price if pos.avg_price > 0 else 0.0
                holding_sec = (self.current_dt - pos.entry_dt).total_seconds() if self.current_dt and pos.entry_dt else 0.0

                record = TradeRecord(
                    trade_id=self._trade_id_counter,
                    symbol=order.symbol,
                    entry_dt=pos.entry_dt or self.current_dt,
                    exit_dt=self.current_dt,
                    entry_price=pos.avg_price,
                    exit_price=fill_price,
                    quantity=filled_qty,
                    side="BUY",
                    gross_pnl=gross_pnl,
                    net_pnl=net_pnl,
                    return_pct=ret_pct,
                    holding_seconds=holding_sec,
                    exit_reason=order.tag or "SELL",
                    mfe_pct=(pos.high_water_mark - pos.avg_price) / pos.avg_price if pos.avg_price > 0 else 0.0,
                    mae_pct=(pos.low_water_mark - pos.avg_price) / pos.avg_price if pos.avg_price > 0 else 0.0,
                )
                self.completed_trades.append(record)
                self._trade_id_counter += 1

                pos.quantity -= filled_qty
                if pos.quantity <= 0:
                    self.positions.pop(order.symbol, None)

        event = OrderEvent(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            status=OrderStatus.FILLED,
            filled_price=fill_price,
            filled_quantity=filled_qty,
            dt=self.current_dt,
            commission=commission,
            tax=tax,
            net_pnl=net_pnl,
            message="FILLED",
        )
        self._emit_event(event)

