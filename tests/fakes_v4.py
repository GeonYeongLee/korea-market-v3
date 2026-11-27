"""In-memory fake ports implementing v4/interfaces.py for deterministic unit/integration tests."""
from __future__ import annotations

from datetime import date, datetime, time
from typing import AsyncIterator, Callable, Dict, List, Optional

from core.krx_ticks import round_to_krx_tick
from v4.interfaces import (
    ExecutionCallback, IBrokerPort, IClock, IConditionSearchPort,
    IRealtimeFeedPort, IRegimeDataPort,
)
from v4.models import (
    AccountBalance, ConditionCandidate, ExecutionMode, IntradaySnapshot, MarketEvent,
    MarketIndexRegime, MinuteBar, OrderEvent, OrderRequest, OrderSide, OrderStatus,
    Position, SymbolFeatures, TickEvent, TradeRecord,
)
from v4.risk import DailyRiskGuard
from v4.runner import UnifiedTradingRunner
from v4.strategy import AllWeatherEntryV4, AllWeatherExitV4, AllWeatherScannerV4, CompositeStrategyV4


class FakeClock(IClock):
    def __init__(self, initial_dt: Optional[datetime] = None) -> None:
        self._now = initial_dt or datetime(2026, 11, 26, 9, 0)

    def now(self) -> datetime:
        return self._now

    def sync_time(self, dt: datetime) -> None:
        self._now = dt

    async def sleep(self, seconds: float) -> None:
        return None


class FakeRegimeData(IRegimeDataPort):
    def __init__(
        self,
        regimes: Optional[Dict[date, MarketIndexRegime]] = None,
        features: Optional[Dict[str, SymbolFeatures]] = None,
        prev_closes: Optional[Dict[str, float]] = None,
    ) -> None:
        self.regimes = regimes or {}
        self.features = features or {}
        self.prev_closes = prev_closes or {}

    async def get_index_regime(self, d: date) -> MarketIndexRegime:
        return self.regimes.get(d, MarketIndexRegime(trade_date=d, ks_gap_pct=0.2, kq_gap_pct=0.3))

    async def get_symbol_features(self, d: date) -> Dict[str, SymbolFeatures]:
        return dict(self.features)

    async def get_prev_closes(self, d: date) -> Dict[str, float]:
        return dict(self.prev_closes)


class FakeConditionSearch(IConditionSearchPort):
    def __init__(self) -> None:
        self.polls: Dict[datetime, List[ConditionCandidate]] = {}
        self.snapshots: Dict[str, IntradaySnapshot] = {}
        self.recent_bars: Dict[str, List[MinuteBar]] = {}
        self.poll_calls: List[tuple[datetime, str]] = []
        self.snapshot_calls: List[str] = []
        self.recent_bar_calls: List[str] = []

    async def poll_candidates(self, as_of: datetime, seq: str = "0") -> List[ConditionCandidate]:
        self.poll_calls.append((as_of, seq))
        return list(self.polls.get(as_of, []))

    async def get_intraday_snapshot(self, symbol: str, as_of: datetime) -> Optional[IntradaySnapshot]:
        self.snapshot_calls.append(symbol)
        return self.snapshots.get(symbol)

    async def get_recent_minute_bars(self, symbol: str, as_of: datetime, limit: int = 30) -> List[MinuteBar]:
        self.recent_bar_calls.append(symbol)
        return list(self.recent_bars.get(symbol, []))[-limit:]


class FakeFeed(IRealtimeFeedPort):
    def __init__(self, events: List[MarketEvent], max_subscriptions: Optional[int] = None) -> None:
        self.events = events
        self.max_subscriptions = max_subscriptions
        self.subscriptions: Dict[str, Optional[bool]] = {}
        self.unsubscribed: List[str] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def subscribe(self, symbol: str, is_nxt: Optional[bool] = None) -> bool:
        if symbol not in self.subscriptions and self.max_subscriptions is not None and len(self.subscriptions) >= self.max_subscriptions:
            return False
        self.subscriptions[symbol] = is_nxt
        return True

    async def unsubscribe(self, symbol: str) -> None:
        self.subscriptions.pop(symbol, None)
        self.unsubscribed.append(symbol)

    def active_symbols(self) -> List[str]:
        return list(self.subscriptions.keys())

    async def stream_events(
        self, start_date: Optional[date] = None, end_date: Optional[date] = None
    ) -> AsyncIterator[MarketEvent]:
        for ev in self.events:
            yield ev


class FakeBroker(IBrokerPort):
    def __init__(
        self,
        initial_capital: float = 10_000_000.0,
        slippage_rate: float = 0.00035,
        comm_rate: float = 0.00014,
        tax_rate: float = 0.0018,
    ) -> None:
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.slippage_rate = slippage_rate
        self.comm_rate = comm_rate
        self.tax_rate = tax_rate
        self.execution_mode: ExecutionMode = "pessimistic"
        self.positions: Dict[str, Position] = {}
        self.pending: Dict[str, OrderEvent] = {}
        self.latest_prices: Dict[str, float] = {}
        self.completed_trades: List[TradeRecord] = []
        self.submitted_requests: List[OrderRequest] = []
        self.buy_outcomes: Dict[str, OrderStatus] = {}
        self._callbacks: List[ExecutionCallback] = []
        self._current_dt: Optional[datetime] = None
        self._seq = 1
        self.emit_on_submit: bool = True
        self.on_submit_hook: Optional[Callable[[OrderRequest], None]] = None
        self.on_cancel_hook: Optional[Callable[[Optional[str]], None]] = None

    def get_balance(self) -> AccountBalance:
        eq = self.cash + sum(p.current_price * p.quantity for p in self.positions.values())
        return AccountBalance(total_equity=eq, cash=self.cash, positions=dict(self.positions), initial_capital=self.initial_capital)

    def get_positions(self) -> Dict[str, Position]:
        return dict(self.positions)

    def get_position(self, symbol: str) -> Optional[Position]:
        return self.positions.get(symbol)

    def get_pending_orders(self) -> List[OrderEvent]:
        return list(self.pending.values())

    def get_completed_trades(self) -> List[TradeRecord]:
        return list(self.completed_trades)

    def on_execution_event(self, cb: ExecutionCallback) -> None:
        self._callbacks.append(cb)

    def set_execution_mode(self, mode: ExecutionMode) -> None:
        self.execution_mode = mode

    def emit(self, ev: OrderEvent) -> None:
        for cb in self._callbacks:
            cb(ev)

    def on_bar(self, bar: MinuteBar) -> None:
        self.latest_prices[bar.symbol] = bar.close
        self._current_dt = bar.dt
        pos = self.positions.get(bar.symbol)
        if pos:
            pos.current_price = bar.close
            pos.high_water_mark = max(pos.high_water_mark, bar.high)
            pos.low_water_mark = min(pos.low_water_mark, bar.low) if pos.low_water_mark > 0 else bar.low

    def on_tick(self, tick: TickEvent) -> None:
        self.latest_prices[tick.symbol] = tick.price
        self._current_dt = tick.dt
        pos = self.positions.get(tick.symbol)
        if pos:
            pos.current_price = tick.price
            pos.high_water_mark = max(pos.high_water_mark, tick.price)
            pos.low_water_mark = min(pos.low_water_mark, tick.price) if pos.low_water_mark > 0 else tick.price

    async def submit_order(self, req: OrderRequest) -> OrderEvent:
        self.submitted_requests.append(req)
        if self.on_submit_hook is not None:
            self.on_submit_hook(req)
        oid = f"ord_{self._seq}"
        self._seq += 1
        dt = req.created_at or self._current_dt or datetime.now()

        if req.side == OrderSide.BUY and self.buy_outcomes.get(req.symbol) == OrderStatus.REJECTED:
            ev = OrderEvent(order_id=oid, symbol=req.symbol, side=req.side, status=OrderStatus.REJECTED, dt=dt, message="TEST_REJECT")
            if self.emit_on_submit:
                self.emit(ev)
            return ev
        if req.side == OrderSide.BUY and self.buy_outcomes.get(req.symbol) == OrderStatus.PENDING:
            ev = OrderEvent(order_id=oid, symbol=req.symbol, side=req.side, status=OrderStatus.PENDING, requested_price=req.price or 0.0, requested_quantity=req.quantity, unfilled_quantity=req.quantity, submitted_at=dt, dt=dt, tag=req.tag)
            self.pending[oid] = ev
            if self.emit_on_submit:
                self.emit(ev)
            return ev

        base = req.price if (req.price and req.price > 0) else self.latest_prices.get(req.symbol, 0.0)
        fill_p = (
            round_to_krx_tick(base * (1.0 + self.slippage_rate), "BUY", "ceil")
            if req.side == OrderSide.BUY
            else round_to_krx_tick(base * (1.0 - self.slippage_rate), "SELL", "floor")
        )
        amt = fill_p * req.quantity
        comm = amt * self.comm_rate
        tax = amt * self.tax_rate if req.side == OrderSide.SELL else 0.0
        net_pnl = 0.0
        if req.side == OrderSide.BUY:
            self.cash -= amt + comm
            self.positions[req.symbol] = Position(symbol=req.symbol, quantity=req.quantity, avg_price=fill_p, current_price=fill_p, entry_dt=dt)
        else:
            self.cash += amt - comm - tax
            pos = self.positions.pop(req.symbol, None)
            if pos:
                gross = (fill_p - pos.avg_price) * pos.quantity
                net_pnl = gross - comm - tax - (pos.avg_price * pos.quantity * self.comm_rate)
                self.completed_trades.append(TradeRecord(len(self.completed_trades) + 1, req.symbol, pos.entry_dt or dt, dt, pos.avg_price, fill_p, pos.quantity, "BUY", gross, net_pnl, (fill_p - pos.avg_price) / pos.avg_price, 0.0, req.tag))
        ev = OrderEvent(order_id=oid, symbol=req.symbol, side=req.side, status=OrderStatus.FILLED, filled_price=fill_p, filled_quantity=req.quantity, cum_filled_quantity=req.quantity, commission=comm, tax=tax, net_pnl=net_pnl, dt=dt, tag=req.tag)
        if self.emit_on_submit:
            self.emit(ev)
        return ev

    async def cancel_order(self, order_id: str, symbol: str = "") -> bool:
        pend = self.pending.pop(order_id, None)
        if pend is None:
            return False
        ev = OrderEvent(order_id=order_id, symbol=pend.symbol, side=pend.side, status=OrderStatus.CANCELLED, cum_filled_quantity=pend.cum_filled_quantity, dt=self._current_dt)
        self.emit(ev)
        return True

    async def cancel_pending(self, symbol: Optional[str] = None) -> List[str]:
        if self.on_cancel_hook is not None:
            self.on_cancel_hook(symbol)
        ids = [oid for oid, o in self.pending.items() if symbol is None or o.symbol == symbol]
        for oid in ids:
            await self.cancel_order(oid, symbol or "")
        return ids

    async def sync_open_orders(self) -> List[OrderEvent]:
        return []


def make_v4_strategy(mode: str = "pessimistic", max_pos: int = 2, shift: bool = True) -> CompositeStrategyV4:
    return CompositeStrategyV4(
        scanner=AllWeatherScannerV4(shift_to_market_open=shift, execution_mode=mode),
        entry=AllWeatherEntryV4(shift_to_market_open=shift),
        exit_policy=AllWeatherExitV4(execution_mode=mode),
        max_daily_positions=max_pos,
        base_slots=max_pos,
        execution_mode=mode,
    )


def make_v4_runner(
    events: List[MarketEvent],
    cond: Optional[FakeConditionSearch] = None,
    broker: Optional[FakeBroker] = None,
    regime: Optional[FakeRegimeData] = None,
    strategy: Optional[CompositeStrategyV4] = None,
    mode: str = "pessimistic",
    **kwargs,
) -> UnifiedTradingRunner:
    return UnifiedTradingRunner(
        strategy or make_v4_strategy(mode=mode),
        FakeClock(),
        regime or FakeRegimeData(),
        cond or FakeConditionSearch(),
        FakeFeed(events),
        broker or FakeBroker(),
        DailyRiskGuard(),
        execution_mode=mode,
        **kwargs,
    )
