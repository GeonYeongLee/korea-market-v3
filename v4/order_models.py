"""
Order, Signal, Position & Account Data Models for Korea Market v4.
Slotted dataclasses and str-Enums shared across broker adapters, strategy components, and audit harness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    BEST_LIMIT = "BEST_LIMIT"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass(slots=True)
class EntrySignal:
    """Emitted by IEntryComponent when breakout + collar-cap conditions hold."""
    symbol: str
    dt: datetime
    trigger_price: float
    collar_cap_price: float
    target_notional: float = 10_000_000.0
    reason: str = "MORNING_BREAKOUT"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExitSignal:
    """Emitted by IExitComponent when a position must be liquidated."""
    symbol: str
    dt: datetime
    order_type: OrderType = OrderType.MARKET
    target_price: Optional[float] = None
    reason: str = "STOP_LOSS"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderRequest:
    """
    Order submission request sent from unified runner to IBrokerPort.
    `price` semantics:
    - For `LIMIT`: statutory KRX-rounded limit price (`BUY`: floor, `SELL`: ceil).
    - For `MARKET`: live KIS sends `ORD_DVSN="01"`, `ORD_UNPR="0"` (ignoring `price`);
      in backtest, `price > 0` (e.g. stop/timeout target or bar ref) is used as the
      deterministic base reference before adverse slippage + tick rounding.
    """
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    price: Optional[float] = None
    tag: str = ""
    created_at: Optional[datetime] = None
    client_order_id: str = ""
    exchange: Optional[str] = None


@dataclass(slots=True)
class OrderEvent:
    """
    Canonical broker order lifecycle notification delivered exclusively by IBrokerPort.
    `net_pnl` MUST be populated on `SELL` `FILLED` events in both backtest and live
    (`gross_pnl - sell_commission - sell_tax - pro_rata_buy_commission`) for `IRiskGuard`.
    """
    order_id: str
    symbol: str
    side: OrderSide
    status: OrderStatus
    filled_price: float = 0.0
    filled_quantity: int = 0
    dt: Optional[datetime] = None
    unfilled_quantity: int = 0
    cum_filled_quantity: int = 0
    requested_price: float = 0.0
    requested_quantity: int = 0
    commission: float = 0.0
    tax: float = 0.0
    net_pnl: float = 0.0
    submitted_at: Optional[datetime] = None
    client_order_id: str = ""
    message: str = ""
    tag: str = ""


@dataclass(slots=True)
class Position:
    """
    Active position state owned by IBrokerPort and mutated synchronously by IExitComponent.
    `phase == 1`: Bar 0 in-flight (ONLY `GRACE_EMERGENCY_SL` permitted).
    `phase == 2`: Bar 1+ completed ref bar (`LOCKED_SL`, `SMART_STEP_UP`, `HARD_TIMEOUT_1521`).
    """
    symbol: str
    quantity: int = 0
    avg_price: float = 0.0
    current_price: float = 0.0
    high_water_mark: float = 0.0
    low_water_mark: float = 0.0
    entry_dt: Optional[datetime] = None
    entry_bar_idx: int = 0
    phase: int = 1
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    trailing_step: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        base = self.avg_price if self.avg_price > 0.0 else self.current_price
        if self.high_water_mark <= 0.0 and base > 0.0:
            self.high_water_mark = base
        if self.low_water_mark <= 0.0 and base > 0.0:
            self.low_water_mark = base

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.avg_price) * self.quantity

    @property
    def return_pct(self) -> float:
        return ((self.current_price - self.avg_price) / self.avg_price) if self.avg_price > 0.0 else 0.0


@dataclass(slots=True)
class AccountBalance:
    """Account cash & mark-to-market equity snapshot."""
    total_equity: float
    cash: float
    positions: Dict[str, Position] = field(default_factory=dict)
    initial_capital: float = 0.0


@dataclass(slots=True)
class TradeRecord:
    """Completed round-trip trade record for reconciliation and metrics."""
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
