"""
Core Data Models for Korea Market v3.
Features high-performance slotted dataclasses and strict typing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date, time
from enum import Enum
from typing import Optional, Dict, Any, List


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass(slots=True)
class MinuteBar:
    """Standardized 1-minute OHLCV bar with essential features."""
    symbol: str
    dt: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    turnover: float = 0.0
    vwap: float = 0.0
    cum_turnover: float = 0.0

    @property
    def time(self) -> time:
        return self.dt.time()

    @property
    def date(self) -> date:
        return self.dt.date()


@dataclass(slots=True)
class TickData:
    """Real-time trade or quote tick data."""
    symbol: str
    dt: datetime
    price: float
    volume: int
    turnover: float = 0.0
    cum_turnover: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    day_open: float = 0.0
    day_low: float = 0.0


@dataclass(slots=True)
class EntrySignal:
    """Emitted by an EntryRule when entry conditions are met."""
    symbol: str
    dt: datetime
    trigger_price: float
    collar_cap_price: float
    target_notional: float = 10_000_000.0  # 단리 고정 1,000만 원
    reason: str = "BREAKOUT"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExitSignal:
    """Emitted by an ExitPolicy when position liquidation is required."""
    symbol: str
    dt: datetime
    order_type: OrderType = OrderType.MARKET
    target_price: Optional[float] = None
    reason: str = "STOP_LOSS"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Order:
    """Unified internal representation of an order."""
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    price: Optional[float] = None
    created_at: Optional[datetime] = None
    status: OrderStatus = OrderStatus.PENDING
    tag: str = ""
    unfilled_quantity: Optional[int] = None
    collar_cap_price: Optional[float] = None


@dataclass(slots=True)
class OrderEvent:
    """Event emitted upon order lifecycle change (FILLED, CANCELLED, etc.)."""
    order_id: str
    symbol: str
    side: OrderSide
    status: OrderStatus
    filled_price: float
    filled_quantity: int
    dt: Optional[datetime] = None
    commission: float = 0.0
    tax: float = 0.0
    net_pnl: float = 0.0
    message: str = ""


@dataclass(slots=True)
class Position:
    """Represents an active or closed position."""
    symbol: str
    quantity: int = 0
    avg_price: float = 0.0
    current_price: float = 0.0
    high_water_mark: float = 0.0
    low_water_mark: float = 0.0
    entry_dt: Optional[datetime] = None
    entry_bar_idx: int = 0
    phase: int = 1  # 1: Bar 0 in-flight, 2: Completed bar
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    trailing_step: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        symbol: str,
        quantity: Optional[int] = None,
        avg_price: Optional[float] = None,
        current_price: float = 0.0,
        high_water_mark: float = 0.0,
        low_water_mark: float = 0.0,
        entry_dt: Optional[datetime] = None,
        entry_bar_idx: int = 0,
        phase: int = 1,
        stop_loss_price: float = 0.0,
        take_profit_price: float = 0.0,
        trailing_step: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
        *,
        shares: Optional[int] = None,
        avg_entry_price: Optional[float] = None,
        entry_time: Optional[datetime] = None,
    ):
        self.symbol = symbol
        self.quantity = quantity if quantity is not None else (shares if shares is not None else 0)
        self.avg_price = avg_price if avg_price is not None else (avg_entry_price if avg_entry_price is not None else 0.0)
        self.current_price = current_price
        self.high_water_mark = high_water_mark if high_water_mark > 0.0 else (self.avg_price if self.avg_price > 0.0 else current_price)
        self.low_water_mark = low_water_mark if low_water_mark > 0.0 else (self.avg_price if self.avg_price > 0.0 else current_price)
        self.entry_dt = entry_dt if entry_dt is not None else entry_time
        self.entry_bar_idx = entry_bar_idx
        self.phase = phase
        self.stop_loss_price = stop_loss_price
        self.take_profit_price = take_profit_price
        self.trailing_step = trailing_step
        self.metadata = metadata if metadata is not None else {}

    @property
    def shares(self) -> int:
        return self.quantity

    @shares.setter
    def shares(self, val: int) -> None:
        self.quantity = val

    @property
    def avg_entry_price(self) -> float:
        return self.avg_price

    @avg_entry_price.setter
    def avg_entry_price(self, val: float) -> None:
        self.avg_price = val

    @property
    def entry_time(self) -> Optional[datetime]:
        return self.entry_dt

    @entry_time.setter
    def entry_time(self, val: Optional[datetime]) -> None:
        self.entry_dt = val

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.avg_price) * self.quantity

    @property
    def return_pct(self) -> float:
        if self.avg_price <= 0:
            return 0.0
        return (self.current_price - self.avg_price) / self.avg_price


@dataclass(slots=True)
class AccountBalance:
    """Snapshot of account cash and positions."""
    total_equity: float
    cash: float
    positions: Dict[str, Position] = field(default_factory=dict)
    initial_capital: float = 0.0


@dataclass(slots=True)
class TradeRecord:
    """Completed trade record for reconciliation and performance calculation."""
    trade_id: int
    symbol: str
    entry_dt: datetime
    exit_dt: datetime
    entry_price: float
    exit_price: float
    quantity: int
    side: str
    gross_pnl: float
    net_pnl: float
    return_pct: float
    holding_seconds: float
    exit_reason: str
    mfe_pct: float = 0.0
    mae_pct: float = 0.0

