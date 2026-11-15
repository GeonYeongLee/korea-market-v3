"""Broker Base Interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Callable
from engine.models import Order, OrderEvent, Position, AccountBalance, MinuteBar, TickData


class IBroker(ABC):
    @abstractmethod
    def submit_order(self, order: Order) -> str:
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        pass

    @abstractmethod
    def get_position(self, symbol: str) -> Optional[Position]:
        pass

    @abstractmethod
    def get_positions(self) -> Dict[str, Position]:
        pass

    @abstractmethod
    def get_balance(self) -> AccountBalance:
        pass

    @abstractmethod
    def register_event_listener(self, listener: Callable[[OrderEvent], None]) -> None:
        pass

    @abstractmethod
    def on_bar(self, bar: MinuteBar) -> None:
        pass

    @abstractmethod
    def on_tick(self, tick: TickData) -> None:
        pass

