"""
Unified Data Models Barrel for Korea Market v4.
Re-exports all slotted market, event, order, signal, and position models.
"""
from __future__ import annotations

from typing import Union

from v4.market_models import (
    BarCloseEvent,
    ConditionCandidate,
    ExecutionMode,
    IntradaySnapshot,
    MarketDivision,
    MarketIndexRegime,
    MinuteBar,
    MinutePulseEvent,
    SymbolFeatures,
    TickEvent,
    WsTrId,
    shift_from_0900,
)
from v4.order_models import (
    AccountBalance,
    EntrySignal,
    ExitSignal,
    OrderEvent,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    TradeRecord,
)

# Canonical stream_events() yield type (OrderEvent is emitted solely via IBrokerPort).
MarketEvent = Union[MinutePulseEvent, TickEvent, BarCloseEvent]

__all__ = [
    "AccountBalance",
    "BarCloseEvent",
    "ConditionCandidate",
    "EntrySignal",
    "ExecutionMode",
    "ExitSignal",
    "IntradaySnapshot",
    "MarketDivision",
    "MarketEvent",
    "MarketIndexRegime",
    "MinuteBar",
    "MinutePulseEvent",
    "OrderEvent",
    "OrderRequest",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "Position",
    "SymbolFeatures",
    "TickEvent",
    "TradeRecord",
    "WsTrId",
    "shift_from_0900",
]
