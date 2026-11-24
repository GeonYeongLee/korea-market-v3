"""
Strategy Execution Context.
Provides a unified facade for strategies to interact with Broker, Clock, Risk, and Engine.
"""
from __future__ import annotations

from datetime import datetime, date
from typing import Optional, Dict, Any, Callable

from engine.models import Order, OrderSide, OrderType, Position, AccountBalance
from engine.broker.base import IBroker
from engine.clock import IClock
from engine.risk.risk_manager import RiskManager


class StrategyContext:
    def __init__(
        self,
        broker: IBroker,
        clock: IClock,
        risk_manager: Optional[RiskManager] = None,
    ):
        self.broker = broker
        self.clock = clock
        self.risk_manager = risk_manager
        self.memory: Dict[str, Any] = {}

    @property
    def current_dt(self) -> datetime:
        return self.clock.now()

    @property
    def current_date(self) -> date:
        return self.clock.now().date()

    def get_position(self, symbol: str) -> Optional[Position]:
        return self.broker.get_position(symbol)

    def get_positions(self) -> Dict[str, Position]:
        return self.broker.get_positions()

    def get_balance(self) -> AccountBalance:
        return self.broker.get_balance()

    def buy(
        self,
        symbol: str,
        quantity: int,
        price: Optional[float] = None,
        tag: str = "BUY",
        collar_cap_price: Optional[float] = None,
    ) -> Optional[str]:
        order = Order(
            order_id="",
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT if price else OrderType.MARKET,
            quantity=quantity,
            price=price,
            tag=tag,
            collar_cap_price=collar_cap_price,
        )
        if self.risk_manager and not self.risk_manager.validate_order(order, self.get_balance()):
            return None
        return self.broker.submit_order(order)

    def liquidate(
        self,
        symbol: str,
        price: Optional[float] = None,
        tag: str = "SELL",
    ) -> Optional[str]:
        pos = self.get_position(symbol)
        if not pos or pos.quantity <= 0:
            return None

        # Allow tag to be passed positionally if price is a str
        if isinstance(price, str):
            tag = price
            price = None

        order_price = price if (price is not None and price > 0) else pos.current_price

        order = Order(
            order_id="",
            symbol=symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=pos.quantity,
            price=order_price,
            tag=tag,
        )
        return self.broker.submit_order(order)

    def schedule(self, trigger_time_str: str, callback: Callable[[datetime], None]) -> None:
        self.clock.schedule(trigger_time_str, callback)


# Alias for backward compatibility
TradingContext = StrategyContext

