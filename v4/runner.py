"""
Unified Async Trading Loop for Korea Market v4 (Live & Backtest).
Communicates strictly through `v4/interfaces.py` ports and `CompositeStrategyV4`.
"""
from __future__ import annotations

from datetime import date, time
from typing import Dict, List, Optional

from v4.audit_sink import NullAuditSink
from v4.event_handlers import (
    entry_blocked,
    handle_bar_close,
    handle_minute_pulse,
    handle_tick,
    prepare_exit_position,
    unsubscribe_if_idle,
)
from v4.interfaces import (
    IAuditSink, IBrokerPort, IClock, IConditionSearchPort, IRealtimeFeedPort, IRegimeDataPort, IRiskGuard,
)
from v4.models import (
    BarCloseEvent, ExecutionMode, MarketEvent, MinutePulseEvent, Position, SymbolFeatures, TickEvent, TradeRecord,
)
from v4.order_executor import OrderExecutor
from v4.strategy.composite import CompositeStrategyV4


class UnifiedTradingRunner:
    """Single canonical asyncio event loop driving both backtest and live KIS execution."""

    def __init__(
        self,
        strategy: CompositeStrategyV4,
        clock: IClock,
        regime_data: IRegimeDataPort,
        condition_search: IConditionSearchPort,
        feed: IRealtimeFeedPort,
        broker: IBrokerPort,
        risk_guard: IRiskGuard,
        execution_mode: ExecutionMode = "pessimistic",
        stale_buy_timeout_sec: float = 45.0,
        is_live_tick_driven: bool = False,
        audit_sink: Optional[IAuditSink] = None,
    ) -> None:
        self.strategy = strategy
        self.clock = clock
        self.regime_data = regime_data
        self.condition_search = condition_search
        self.feed = feed
        self.broker = broker
        self.risk_guard = risk_guard
        self.execution_mode: ExecutionMode = execution_mode
        self.is_live_tick_driven = is_live_tick_driven
        self.audit_sink: IAuditSink = audit_sink if audit_sink is not None else NullAuditSink()

        self.broker.set_execution_mode(self.execution_mode)
        self.executor = OrderExecutor(
            strategy=strategy,
            broker=broker,
            clock=clock,
            risk_guard=risk_guard,
            stale_buy_timeout_sec=stale_buy_timeout_sec,
            unsubscribe_if_idle=self._unsubscribe_if_idle,
            audit_sink=self.audit_sink,
        )

        self._current_date: Optional[date] = None
        self._market_open_time: time = time(9, 0)
        self._market_close_time: time = time(15, 30)
        self._day_features: Dict[str, SymbolFeatures] = {}
        self._day_prev_closes: Dict[str, float] = {}

    async def run(self, start_date: Optional[date] = None, end_date: Optional[date] = None) -> List[TradeRecord]:
        await self.feed.start()
        try:
            async for ev in self.feed.stream_events(start_date=start_date, end_date=end_date):
                await self._dispatch_event(ev)
            if self._current_date is not None:
                await self._on_day_close(self._current_date)
        finally:
            self._current_date = None
            await self.feed.stop()
        return self.broker.get_completed_trades()

    async def _dispatch_event(self, ev: MarketEvent) -> None:
        await self._ensure_day_open(ev.date)
        await self.executor.drain_order_events()
        if isinstance(ev, MinutePulseEvent):
            await self._handle_minute_pulse(ev)
        elif isinstance(ev, TickEvent):
            await self._handle_tick(ev)
        elif isinstance(ev, BarCloseEvent):
            await self._handle_bar_close(ev)
        await self.executor.drain_order_events()

    async def _ensure_day_open(self, d: date) -> None:
        if self._current_date == d:
            return
        if self._current_date is not None:
            await self._on_day_close(self._current_date)
        self._current_date = d
        regime = await self.regime_data.get_index_regime(d)
        self._day_features = await self.regime_data.get_symbol_features(d)
        self._day_prev_closes = await self.regime_data.get_prev_closes(d)
        self._market_open_time = regime.market_open_time
        self._market_close_time = regime.market_close_time

        self.executor.reset_day()
        self.risk_guard.on_day_open(d)
        bal = self.broker.get_balance()
        self.strategy.on_day_open(
            d,
            regime=regime,
            features=self._day_features,
            prev_closes=self._day_prev_closes,
            cash=bal.cash,
            execution_mode=self.execution_mode,
        )

    async def _on_day_close(self, d: date) -> None:
        await self.executor.drain_order_events()
        await self.broker.cancel_pending()
        await self.executor.drain_order_events()
        for sym, pos in list(self.broker.get_positions().items()):
            if pos and pos.quantity > 0:
                exec_p = pos.current_price if pos.current_price > 0.0 else pos.avg_price
                await self.executor.submit_exit_order(
                    sym, pos.quantity, exec_p, "EOD_FAILSAFE_LIQUIDATION", self.clock.now()
                )
        for sym in list(self.feed.active_symbols()):
            await self.feed.unsubscribe(sym)

    async def _unsubscribe_if_idle(self, symbol: str) -> None:
        await unsubscribe_if_idle(self, symbol)

    def _entry_blocked(self, symbol: str) -> bool:
        return entry_blocked(self, symbol)

    async def _prepare_exit_position(self, sym: str, pos: Position) -> Optional[Position]:
        return await prepare_exit_position(self, sym, pos)

    async def _handle_minute_pulse(self, ev: MinutePulseEvent) -> None:
        await handle_minute_pulse(self, ev)

    async def _handle_tick(self, tick: TickEvent) -> None:
        await handle_tick(self, tick)

    async def _handle_bar_close(self, ev: BarCloseEvent) -> None:
        await handle_bar_close(self, ev)
