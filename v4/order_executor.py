"""
Canonical Order Execution & Lifecycle Queue Manager for Korea Market v4.
Handles single-stream OrderEvent draining, BUY/SELL submission, stale BUY timeout,
and IRiskGuard feedback via v4/interfaces.py ports only.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable, Dict, List, Optional

from v4.interfaces import IBrokerPort, IClock, IRiskGuard
from v4.models import (
    EntrySignal,
    MinuteBar,
    OrderEvent,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from v4.strategy.composite import CompositeStrategyV4

logger = logging.getLogger("korea_market.v4.order_executor")


class OrderExecutor:
    """Owns the canonical OrderEvent asyncio.Queue and coordinates order lifecycle with IBrokerPort."""

    def __init__(
        self,
        strategy: CompositeStrategyV4,
        broker: IBrokerPort,
        clock: IClock,
        risk_guard: IRiskGuard,
        stale_buy_timeout_sec: float = 45.0,
        unsubscribe_if_idle: Optional[Callable[[str], object]] = None,
    ) -> None:
        self.strategy = strategy
        self.broker = broker
        self.clock = clock
        self.risk_guard = risk_guard
        self.stale_buy_timeout_sec = stale_buy_timeout_sec
        self._unsubscribe_if_idle = unsubscribe_if_idle

        self._order_queue: asyncio.Queue[OrderEvent] = asyncio.Queue()
        self._buy_submit_times: Dict[str, datetime] = {}
        self.broker.on_execution_event(self._enqueue_order_event)

    def reset_day(self) -> None:
        self._buy_submit_times.clear()

    def _enqueue_order_event(self, event: OrderEvent) -> None:
        self._order_queue.put_nowait(event)

    @staticmethod
    def _matches_event(ev: OrderEvent, ret: OrderEvent) -> bool:
        return (
            ev.order_id == ret.order_id
            and ev.status == ret.status
            and ev.symbol == ret.symbol
            and ev.side == ret.side
        )

    async def _maybe_unsubscribe_idle(self, symbol: str) -> None:
        pos = self.broker.get_position(symbol)
        if pos is None or pos.quantity <= 0:
            self.strategy._active_positions.discard(symbol)
            if self._unsubscribe_if_idle is not None:
                res = self._unsubscribe_if_idle(symbol)
                if asyncio.iscoroutine(res):
                    await res

    async def drain_order_events(self) -> List[OrderEvent]:
        drained: List[OrderEvent] = []
        while not self._order_queue.empty():
            ev = self._order_queue.get_nowait()
            drained.append(ev)
            self._process_order_event(ev)
            if ev.side == OrderSide.SELL and ev.status == OrderStatus.FILLED:
                await self._maybe_unsubscribe_idle(ev.symbol)
        return drained

    def _process_order_event(self, ev: OrderEvent) -> None:
        if ev.side == OrderSide.SELL and ev.status == OrderStatus.FILLED:
            self.risk_guard.on_trade_completed(ev.net_pnl)
        if ev.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            self._buy_submit_times.pop(ev.order_id, None)
        pos = self.broker.get_position(ev.symbol)
        self.strategy.handle_order_event(ev, pos)

    async def cancel_stale_buy_orders(self, now_dt: datetime) -> None:
        for pend in self.broker.get_pending_orders():
            if pend.side != OrderSide.BUY:
                continue
            ref_dt = self._buy_submit_times.get(pend.order_id) or pend.submitted_at or pend.dt
            if ref_dt is not None and (now_dt - ref_dt).total_seconds() >= self.stale_buy_timeout_sec:
                await self.broker.cancel_order(pend.order_id, symbol=pend.symbol)
                await self.drain_order_events()

    async def submit_entry_order(
        self, symbol: str, ref_bar: MinuteBar, signal: EntrySignal
    ) -> Optional[Position]:
        bal = self.broker.get_balance()
        avail_cash = bal.cash if bal is not None else None
        qty = self.strategy.compute_order_quantity(signal.collar_cap_price, avail_cash)
        if qty <= 0:
            self.strategy.entry.rollback_entered_symbol(symbol)
            return None

        req = OrderRequest(
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=qty,
            price=signal.collar_cap_price,
            tag=signal.reason,
            created_at=self.clock.now(),
        )
        if not self.risk_guard.validate_order(req, bal):
            self.strategy.entry.rollback_entered_symbol(symbol)
            return None

        self.strategy._pending_ref_bars[symbol] = self.strategy.make_ref_bar_dict(ref_bar, signal)
        self.strategy._daily_trade_count += 1
        self.strategy._active_positions.add(symbol)
        self.strategy._active_candidates.discard(symbol)
        self.strategy._pending_buy_symbols.add(symbol)

        try:
            ret = await self.broker.submit_order(req)
        except Exception as exc:
            logger.error(f"[ORDER SUBMIT ERROR] BUY {symbol} failed: {exc}", exc_info=True)
            drained = await self.drain_order_events()
            pos_now = self.broker.get_position(symbol)
            has_pos = pos_now is not None and pos_now.quantity > 0
            if not has_pos and not any(
                ev.symbol == symbol and ev.side == OrderSide.BUY
                and ev.status in (
                    OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED,
                )
                for ev in drained
            ):
                self.strategy._pending_buy_symbols.discard(symbol)
                self.strategy._daily_trade_count = max(0, self.strategy._daily_trade_count - 1)
                self.strategy._active_positions.discard(symbol)
                self.strategy._pending_ref_bars.pop(symbol, None)
                self.strategy.entry.rollback_entered_symbol(symbol)
            return None

        if ret.order_id and ret.status in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED):
            self._buy_submit_times[ret.order_id] = ret.submitted_at or self.clock.now()
        drained = await self.drain_order_events()
        if ret.status != OrderStatus.PENDING and not any(self._matches_event(ev, ret) for ev in drained):
            self._process_order_event(ret)
        return self.broker.get_position(symbol)

    async def submit_exit_order(
        self, symbol: str, quantity: int, price: Optional[float], tag: str, dt: datetime
    ) -> None:
        bal = self.broker.get_balance()
        req = OrderRequest(
            symbol=symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=quantity,
            price=price,
            tag=tag,
            created_at=dt,
        )
        if not self.risk_guard.validate_order(req, bal):
            return

        self.strategy._pending_sell_symbols.add(symbol)
        try:
            ret = await self.broker.submit_order(req)
        except Exception as exc:
            logger.error(f"[ORDER SUBMIT ERROR] SELL {symbol} failed: {exc}", exc_info=True)
            self.strategy._pending_sell_symbols.discard(symbol)
            await self.drain_order_events()
            return

        drained = await self.drain_order_events()
        matched = any(self._matches_event(ev, ret) for ev in drained)
        if ret.status != OrderStatus.PENDING and not matched:
            self._process_order_event(ret)
        if not (matched and ret.status == OrderStatus.FILLED):
            await self._maybe_unsubscribe_idle(symbol)
