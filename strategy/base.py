"""
Base Interfaces for the Korea Market v3 Modular Strategy Architecture.
Enforces total separation of concerns:
  1. IScanner     : Universe screening & Candidate selection
  2. IEntryRule   : Entry trigger & Collar-cap calculation
  3. IExitPolicy  : Position management & Stop/Trailing exit
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Optional, List, Dict, Any

from engine.models import MinuteBar, TickData, Position, EntrySignal, ExitSignal, OrderEvent


class IScanner(ABC):
    """Component responsible for identifying trading candidate symbols."""

    name: str = "base_scanner"

    @abstractmethod
    def filter_universe(self, current_date: date) -> Optional[List[str]]:
        """Pre-market daily static filtering (e.g. market cap, blacklist, initial universe)."""
        pass

    @abstractmethod
    def on_bar(self, current_date: date, bar: MinuteBar) -> Optional[List[str]]:
        """
        Intraday stream evaluation. Returns a list of symbols that meet scanning criteria
        (e.g. cumulative turnover >= 10B KRW, 1-min turnover >= 300M KRW).
        """
        pass

    def on_day_open(self, current_date: date) -> None:
        """Called at 09:00:00 to reset daily counters."""
        pass


class IEntryRule(ABC):
    """Component responsible for signal generation and entry order parameters."""

    name: str = "base_entry_rule"

    @abstractmethod
    def evaluate_entry(
        self,
        symbol: str,
        bar: MinuteBar,
        tick: Optional[TickData] = None,
        context_state: Optional[Dict[str, Any]] = None,
    ) -> Optional[EntrySignal]:
        """
        Evaluates whether a symbol triggers an entry condition.
        Returns EntrySignal with trigger price and collar-cap price, or None.
        """
        pass

    def on_day_open(self, current_date: date) -> None:
        """Called at 09:00:00 to reset daily entry states."""
        pass


class IExitPolicy(ABC):
    """Component responsible for managing risk, stop-loss, and profit-taking."""

    name: str = "base_exit_policy"

    @abstractmethod
    def evaluate_exit(
        self,
        position: Position,
        bar: MinuteBar,
        tick: Optional[TickData] = None,
    ) -> Optional[ExitSignal]:
        """
        Evaluates whether an active position should be liquidated.
        Returns ExitSignal (e.g. STOP_LOSS, TRAILING_STOP, TIME_STOP) or None.
        """
        pass

    def on_position_opened(self, position: Position) -> None:
        """Hook called immediately when an order is filled and position created."""
        pass

    def on_day_open(self, current_date: date) -> None:
        """Called at 09:00:00 to reset daily exit states."""
        pass


class BaseStrategy(ABC):
    """Top-level strategy interface orchestrated by TradingEngine."""

    name: str = "base_strategy"

    @abstractmethod
    def on_start(self, context: Any) -> None:
        pass

    @abstractmethod
    def on_stop(self, context: Any) -> None:
        pass

    @abstractmethod
    def on_day_open(self, context: Any, current_date: date) -> None:
        pass

    @abstractmethod
    def on_day_close(self, context: Any, current_date: date) -> None:
        pass

    @abstractmethod
    def on_bar(self, context: Any, bar: MinuteBar) -> None:
        pass

    @abstractmethod
    def on_tick(self, context: Any, tick: TickData) -> None:
        pass

    @abstractmethod
    def on_order_event(self, context: Any, event: OrderEvent) -> None:
        pass

