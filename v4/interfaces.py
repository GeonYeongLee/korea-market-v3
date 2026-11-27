"""
Abstract Ports & Strategy Component Contracts for Korea Market v4 (DIP).
All strategy and unified runner code communicates solely through these interfaces.
Adapters (`v4/adapters/backtest/` and `v4/adapters/kis/`) implement the I/O ports.
Strategy components (`v4/strategy/`) remain strictly synchronous & audit-compatible.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, time
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Union

from v4.models import (
    AccountBalance, ConditionCandidate, EntrySignal, ExecutionMode, ExitSignal,
    IntradaySnapshot, MarketEvent, MarketIndexRegime, MinuteBar, OrderEvent,
    OrderRequest, Position, SymbolFeatures, TickEvent, TradeRecord,
)

ExecutionCallback = Callable[[OrderEvent], Union[None, Awaitable[None]]]


class IClock(ABC):
    """Unified simulation/wall-clock abstraction."""

    @abstractmethod
    def now(self) -> datetime: """Returns current simulated or live KST datetime."""
    def today(self) -> date: return self.now().date()
    def time(self) -> time: return self.now().time()

    @abstractmethod
    def sync_time(self, dt: datetime) -> None: """Advances virtual clock in backtest; no-op in live."""

    @abstractmethod
    async def sleep(self, seconds: float) -> None: """Awaits wall time in live; no-op in backtest."""


class IRegimeDataPort(ABC):
    """Pre-market index regime, session open/close schedule, and t-1 symbol features."""

    @abstractmethod
    async def get_index_regime(self, d: date) -> MarketIndexRegime:
        """Returns index regime, 09:00/10:00 open & close times, and gap thresholds."""

    @abstractmethod
    async def get_symbol_features(self, d: date) -> Dict[str, SymbolFeatures]:
        """Returns causal t-1 ATR20/ADTO20/prev_close/NXT flags per symbol."""

    @abstractmethod
    async def get_prev_closes(self, d: date) -> Dict[str, float]:
        """Returns prior trading day settled close prices keyed by 6-digit symbol."""


class IConditionSearchPort(ABC):
    """
    HTS Condition Search (`HHKST03900400`) & causal point-in-time snapshot port.
    Supports `seq` switching (`seq="0"` -> `n`, `seq="1"` -> `n OR n-1`) and
    strict regular-session `day_open`/`day_low` isolation from `08:00~08:50` premarket.
    """

    @abstractmethod
    async def poll_candidates(self, as_of: datetime, seq: str = "0") -> List[ConditionCandidate]:
        """Polls pure-price HTS candidates (`HHKST03900400` vs causal 1m emulator)."""

    @abstractmethod
    async def get_intraday_snapshot(self, symbol: str, as_of: datetime) -> Optional[IntradaySnapshot]:
        """
        Fetches causal snapshot as of `as_of` (`FHKST01010100` `UN` -> `J` fallback).
        For NXT-eligible stocks in live: `cum_turnover` & `cum_volume` come from `"UN"`,
        while `day_open`/`day_low`/`day_high` MUST come from `"J"` (KRX) or regular
        (>= market_open_time) bars — NEVER from raw `"UN"` premarket-contaminated fields.
        """

    @abstractmethod
    async def get_recent_minute_bars(self, symbol: str, as_of: datetime, limit: int = 30) -> List[MinuteBar]:
        """Fetches regular-session (>= market_open_time, vol > 0) 1m bars up to `as_of`."""


class IRealtimeFeedPort(ABC):
    """
    Unified market event pump driving both live WS/polling and backtest simulation.
    Live routes WS TRs: NXT-eligible -> `H0UNCNT0` ("UN"), non-NXT -> `H0STCNT0` ("J").
    """

    @abstractmethod
    async def start(self) -> None: """Initializes stream connections / dataset prep."""

    @abstractmethod
    async def stop(self) -> None: """Cleanly tears down stream connections and tasks."""

    @abstractmethod
    async def subscribe(self, symbol: str, is_nxt: Optional[bool] = None) -> bool:
        """
        Idempotently subscribes symbol across shared 40-slot WS cap (`H0UNCNT0` + `H0STCNT0`).
        - If `is_nxt is None`, resolves via NXT master / features / snapshot before WS send
          (never blindly subscribing non-NXT stocks to `H0UNCNT0`).
        - Eviction protects BOTH open held positions AND symbols with pending orders;
          returns `False` if all 40 slots are protected.
        """

    @abstractmethod
    async def unsubscribe(self, symbol: str) -> None:
        """Immediately unsubscribes disqualified, expired, or closed symbols."""

    @abstractmethod
    def active_symbols(self) -> List[str]:
        """Returns currently subscribed symbols in subscription order."""

    @abstractmethod
    def stream_events(
        self, start_date: Optional[date] = None, end_date: Optional[date] = None
    ) -> AsyncIterator[MarketEvent]:
        """
        Yields `MarketEvent` (`MinutePulseEvent`, `TickEvent`, `BarCloseEvent`) only;
        NEVER emits `OrderEvent` (canonical execution stream comes solely from `IBrokerPort`).
        Continues emitting `MinutePulseEvent` until `market_close_time` (`15:30`) even on
        zero-tick non-NXT stocks so `15:21` timeout liquidates in closing auction. In live,
        wall-clock rollover flushes quiet bars at `M+1:00.1` with `BarCloseEvent.dt = M`.
        """


class IBrokerPort(ABC):
    """
    Single canonical owner of order execution state, positions, and ambiguity mode.
    Emits all `OrderEvent` transitions via `on_execution_event` queue consumed by `runner.py`.
    `get_positions()` / `get_position()` expose shared mutable `Position` state owned by broker.
    """

    @abstractmethod
    def get_balance(self) -> AccountBalance: """Returns cash and mark-to-market equity."""

    @abstractmethod
    def get_positions(self) -> Dict[str, Position]: """Returns shallow dict copy -> mutable `Position`."""

    @abstractmethod
    def get_position(self, symbol: str) -> Optional[Position]: """Returns mutable `Position`, or None."""

    @abstractmethod
    async def submit_order(self, req: OrderRequest) -> OrderEvent:
        """Submits order (`PENDING` or immediate fill) and emits lifecycle `OrderEvent`s."""

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str = "") -> bool:
        """Cancels an open order; reconciles exchange fills if cancel is rejected."""

    @abstractmethod
    async def cancel_pending(self, symbol: Optional[str] = None) -> List[str]:
        """Cancels open pending orders (optionally filtered by symbol)."""

    @abstractmethod
    async def sync_open_orders(self) -> List[OrderEvent]:
        """Reconciles pending orders and holdings against exchange REST state."""

    @abstractmethod
    def on_execution_event(self, cb: ExecutionCallback) -> None:
        """Registers single canonical execution callback (`OrderEvent.net_pnl` set on SELL FILL)."""

    @abstractmethod
    def set_execution_mode(self, mode: ExecutionMode) -> None:
        """Controls intra-bar ambiguity (`pessimistic` | `optimistic`) in backtest; no-op in live."""

    @abstractmethod
    def on_bar(self, bar: MinuteBar) -> None: """Updates MTM price, HWM/LWM, and clock ref."""

    @abstractmethod
    def on_tick(self, tick: TickEvent) -> None: """Updates MTM price and HWM/LWM from ticks."""

    def get_pending_orders(self) -> List[OrderEvent]: return []
    def get_completed_trades(self) -> List[TradeRecord]: return []


class IRiskGuard(ABC):
    """Account-level circuit breaker guarding daily loss limits and consecutive losses."""

    @abstractmethod
    def on_day_open(self, current_date: date) -> None:
        """Resets daily realized PnL, consecutive losses, and circuit-breaker flag."""

    @abstractmethod
    def on_trade_completed(self, net_pnl: float) -> None:
        """Updates daily PnL & loss streak upon `SELL` `FILLED` `OrderEvent.net_pnl`."""

    @abstractmethod
    def validate_order(self, req: OrderRequest, balance: AccountBalance) -> bool:
        """Rejects new `BUY` orders when circuit broken; always permits `SELL` exits."""

    @property
    def is_circuit_broken(self) -> bool: return False


class IScannerComponent(ABC):
    """Pure synchronous candidate scanner block (<= 300 lines, Lookahead 0%, zero disk I/O)."""

    name: str = "base_scanner"
    execution_mode: ExecutionMode = "pessimistic"

    def set_execution_mode(self, mode: ExecutionMode) -> None: self.execution_mode = mode

    @abstractmethod
    def on_day_open(
        self, current_date: date, prev_closes: Optional[Dict[str, float]] = None,
        features: Optional[Dict[str, SymbolFeatures]] = None,
        market_open_time: time = time(9, 0), market_close_time: time = time(15, 30),
        execution_mode: Optional[ExecutionMode] = None,
    ) -> None: """Resets daily state and registers causal metadata & session schedule."""

    @abstractmethod
    def on_bar(self, current_date: date, bar: MinuteBar) -> Optional[List[str]]:
        """Evaluates 1m bar stream and returns qualified symbols if updated."""

    @abstractmethod
    def evaluate_candidate(
        self, cand: ConditionCandidate, snapshot: Optional[IntradaySnapshot] = None,
        recent_bars: Optional[List[MinuteBar]] = None,
    ) -> bool: """Validates gap, KRX MAE guard (`execution_mode`-aware), and same-bar surge+turnover."""

    def filter_universe(self, current_date: date) -> Optional[List[str]]: return None
    def is_disqualified(self, symbol: str) -> bool: return False
    def get_day_open(self, symbol: str) -> Optional[float]: return None
    def get_prev_close(self, symbol: str) -> Optional[float]: return None
    def register_day_open(self, symbol: str, day_open: float, krx_day_low: float = 0.0, prev_close: float = 0.0) -> None: return None
    def effective_min_turnover(self, symbol: str) -> float: return float(getattr(self, "min_cum_turnover", 0.0))


class IEntryComponent(ABC):
    """Pure synchronous entry signal block (<= 300 lines, Bar 0 causal & KRX tick compliant)."""

    name: str = "base_entry"

    @abstractmethod
    def on_day_open(
        self, current_date: date, features: Optional[Dict[str, SymbolFeatures]] = None,
        market_open_time: time = time(9, 0), market_close_time: time = time(15, 30),
    ) -> None: """Resets daily single-fire state, ATR20 features, and session schedule."""

    @abstractmethod
    def evaluate_entry(
        self, symbol: str, bar: Optional[MinuteBar] = None, tick: Optional[TickEvent] = None,
        context_state: Optional[Dict[str, Any]] = None,
    ) -> Optional[EntrySignal]: """Evaluates breakout without `bar.close` dependency; aligns to KRX ticks."""

    def register_day_open(self, symbol: str, price: float) -> None: return None
    def rollback_entered_symbol(self, symbol: str) -> None: return None


class IExitComponent(ABC):
    """Pure synchronous exit policy block (<= 300 lines, monotonic ratchet SL & Dual-Bounds)."""

    name: str = "base_exit"
    execution_mode: ExecutionMode = "pessimistic"
    hard_timeout: time = time(15, 21)

    @property
    def ambiguity_mode(self) -> ExecutionMode: return self.execution_mode

    @ambiguity_mode.setter
    def ambiguity_mode(self, value: ExecutionMode) -> None: self.execution_mode = value

    def set_execution_mode(self, mode: ExecutionMode) -> None: self.execution_mode = mode

    @abstractmethod
    def on_day_open(
        self, current_date: date, market_open_time: time = time(9, 0),
        market_close_time: time = time(15, 30), execution_mode: Optional[ExecutionMode] = None,
    ) -> None: """Resets per-day exit state and session schedule."""

    @abstractmethod
    def on_position_opened(self, position: Position) -> None:
        """Initializes Phase 1 emergency SL and dynamic TP targets upon fill."""

    def elevate_to_phase2(self, position: Position, ref_bar: Optional[MinuteBar] = None) -> None:
        """Syncs completed Bar 0 ref_bar metadata and elevates Phase 1 -> Phase 2 monotonically."""
        return None

    @abstractmethod
    def evaluate_exit(
        self, position: Position, bar: Optional[MinuteBar] = None, tick: Optional[TickEvent] = None,
    ) -> Optional[ExitSignal]:
        """
        Enforces `if position.phase == 1: evaluate GRACE_EMERGENCY_SL ONLY` (never TP,
        Super Leader, or body-center SL inside Bar 0; skipped when `execution_mode == "optimistic"`).
        Phase 2+ evaluates ref/trailing SL, TP, and `hard_timeout` (`15:21` or `< market_close_time`).
        """


class IAuditSink(ABC):
    """Structured audit trail sink for live/backtest reconciliation (zero-overhead when NullAuditSink)."""

    @abstractmethod
    def emit(self, event_type: str, dt: datetime, symbol: str = "", **data: Any) -> None:
        """Emits a structured audit event."""

    def close(self) -> None:
        """Flushes and closes any underlying sink resources."""
        return None
