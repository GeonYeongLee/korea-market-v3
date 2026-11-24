"""
Trading Engine Orchestrator.
Coordinates Strategy, Broker, DataFeed, Clock, and RiskManager with zero environment coupling.
"""
from __future__ import annotations

import logging
from datetime import datetime, date, time
from typing import Optional, List, Set, Tuple, TYPE_CHECKING

from engine.models import MinuteBar, TickData, OrderEvent, OrderSide, OrderStatus
from engine.broker.base import IBroker
from engine.datafeed.base import IDataFeed
from engine.clock import IClock, SimulatedClock
from engine.risk.risk_manager import RiskManager
from engine.context import StrategyContext
from engine.metrics import calculate_performance, PerformanceSummary

if TYPE_CHECKING:
    from strategy.base import BaseStrategy

logger = logging.getLogger("korea_market.engine")


class TradingEngine:
    def __init__(
        self,
        strategy: BaseStrategy,
        broker: IBroker,
        feed: IDataFeed,
        clock: IClock,
        risk_manager: Optional[RiskManager] = None,
    ):
        self.strategy = strategy
        self.broker = broker
        self.feed = feed
        self.clock = clock
        self.risk_manager = risk_manager

        self.context = StrategyContext(broker=self.broker, clock=self.clock, risk_manager=self.risk_manager)
        self.broker.register_event_listener(self._handle_order_event)

        if hasattr(self.feed, "register_filter"):
            self.feed.register_filter(self._check_feed_filter)

        self._current_date: Optional[date] = None

    def _rollover_date(self, new_date: date) -> None:
        if self._current_date != new_date:
            if self._current_date is not None:
                self.strategy.on_day_close(self.context, self._current_date)
            self._current_date = new_date
            if self.risk_manager:
                self.risk_manager.on_day_open(new_date)
            self.strategy.on_day_open(self.context, new_date)

    def _check_feed_filter(self, current_dt: datetime) -> Tuple[bool, Optional[Set[str]]]:
        """
        Coordinated high-performance event stream filter.
        Returns:
            (is_day_done: bool, active_symbols: Optional[Set[str]])
        """
        if self._current_date != current_dt.date():
            self._rollover_date(current_dt.date())

        broker_positions = getattr(self.broker, "positions", {})
        has_positions = bool(broker_positions)
        strat_completed = getattr(self.strategy, "is_daily_trading_completed", False)

        # Condition 1: Daily trading quota reached and all positions liquidated
        if strat_completed and not has_positions:
            return True, None

        # Condition 2: Morning entry window passed and no positions held
        base_cutoff = getattr(self.strategy, "cutoff_time", time(9, 20))
        cutoff_time = base_cutoff
        if current_dt.hour >= 10 and base_cutoff.hour < 10:
            cutoff_time = time(base_cutoff.hour + 1, base_cutoff.minute, base_cutoff.second)

        if current_dt.time() > cutoff_time and not has_positions:
            return True, None

        # Condition 3: Afternoon holding phase (or quota reached with open position)
        if (current_dt.time() > cutoff_time or strat_completed) and has_positions:
            return False, set(broker_positions.keys())

        # High-performance coordinated afternoon pushdown: load only active positions and candidate stocks
        if current_dt.time() > time(9, 30):
            strat_cands: Set[str] = set()
            scanner = getattr(self.strategy, "scanner", None)
            if scanner and hasattr(scanner, "filter_universe"):
                u_list = scanner.filter_universe(current_dt.date())
                if u_list:
                    strat_cands.update(u_list)
            if strat_cands or has_positions:
                return False, set(broker_positions.keys()) | strat_cands
            return True, None

        # Standard morning scanning & entry window: stream all universe symbols
        return False, None

    def _handle_order_event(self, event: OrderEvent) -> None:
        if (
            self.risk_manager
            and event.side == OrderSide.SELL
            and event.status == OrderStatus.FILLED
        ):
            self.risk_manager.on_trade_completed(event.net_pnl)
        self.strategy.on_order_event(self.context, event)

    def run(self) -> PerformanceSummary:
        logger.info(f"⚡ [Engine Started] Strategy: {self.strategy.name}")
        self.strategy.on_start(self.context)

        for event in self.feed.stream_events():
            event_dt = event.dt
            event_date = event_dt.date()

            # Date Rollover Detection
            self._rollover_date(event_date)

            # Advance Virtual Clock
            if isinstance(self.clock, SimulatedClock):
                self.clock.set_time(event_dt)

            # Dispatch Bar or Tick
            if isinstance(event, MinuteBar):
                self.broker.on_bar(event)
                self.strategy.on_bar(self.context, event)
            elif isinstance(event, TickData):
                self.broker.on_tick(event)
                self.strategy.on_tick(self.context, event)

        if self._current_date is not None:
            self.strategy.on_day_close(self.context, self._current_date)

        self.strategy.on_stop(self.context)
        logger.info("🏁 [Engine Finished]")

        # Return performance summary if backtesting broker
        trades = getattr(self.broker, "completed_trades", [])
        return calculate_performance(trades)

