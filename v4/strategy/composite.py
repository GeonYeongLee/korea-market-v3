"""
Composite Strategy Orchestrator for Korea Market v4.
Assembles All-Weather Scanner, Entry, and Exit components with pure DIP,
exact v3 sizing parity, order lifecycle tracking, and audit/ Stage-4 facade compatibility.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any, Dict, Optional, Set

from v4.interfaces import IEntryComponent, IExitComponent, IScannerComponent
from v4.models import (
    EntrySignal,
    ExecutionMode,
    MarketIndexRegime,
    MinuteBar,
    OrderEvent,
    OrderSide,
    OrderStatus,
    Position,
    SymbolFeatures,
)
from v4.strategy.exit import make_ref_bar_dict

logger = logging.getLogger("korea_market.v4.strategy.composite")


class CompositeStrategyV4:
    """Unified All-Weather composite strategy orchestrator."""

    def __init__(
        self,
        scanner: IScannerComponent,
        entry: IEntryComponent,
        exit_policy: IExitComponent,
        total_capital: float = 10_000_000.0,
        max_daily_positions: int = 2,
        base_slots: int = 2,
        dynamic_slots: bool = True,
        regime_mode: str = "all_weather",
        execution_mode: ExecutionMode = "pessimistic",
        name: str = "Composite_all_weather_v4",
    ) -> None:
        self.scanner = scanner
        self.entry = entry
        self.exit_policy = exit_policy
        self.total_capital = float(total_capital)
        self.base_max_daily_positions = self.max_daily_positions = int(max_daily_positions)
        self.base_slots = int(base_slots)
        self.dynamic_slots = dynamic_slots
        self.regime_mode = regime_mode
        self.execution_mode: ExecutionMode = execution_mode
        self.name = name

        self.effective_capital: float = self.total_capital
        self.current_slot_notional: float = (
            self.effective_capital / self.max_daily_positions if self.max_daily_positions > 0 else 0.0
        )
        self._active_candidates: Set[str] = set()
        self._candidate_bar_min: Dict[str, datetime] = {}
        self._active_positions: Set[str] = set()
        self._pending_buy_symbols: Set[str] = set()
        self._pending_sell_symbols: Set[str] = set()
        self._forming_bars: Dict[str, MinuteBar] = {}
        self._mid_minute_promoted: Set[str] = set()
        self._daily_trade_count: int = 0
        self._pending_ref_bars: Dict[str, Dict[str, Any]] = {}
        self._rejection_counts: Dict[str, int] = {}

    @property
    def is_daily_trading_completed(self) -> bool:
        return self._daily_trade_count >= self.max_daily_positions

    @property
    def cutoff_time(self) -> time:
        scanner_end = getattr(self.scanner, "end_time", time(9, 15))
        entry_end = getattr(self.entry, "end_time", time(9, 15))
        latest = max(scanner_end, entry_end)
        total_min = latest.hour * 60 + latest.minute + 5
        return time(min(23, total_min // 60), total_min % 60)

    def is_locked_out(self, symbol: str) -> bool:
        return self._rejection_counts.get(symbol, 0) >= 3

    def sync_capital_with_balance(self, available_cash: float) -> None:
        if available_cash <= 0.0:
            return
        self.effective_capital = available_cash * 0.995 if available_cash < self.total_capital else self.total_capital
        if self.max_daily_positions > 0:
            self.current_slot_notional = self.effective_capital / self.max_daily_positions
            if hasattr(self.entry, "target_notional"):
                self.entry.target_notional = self.current_slot_notional

    def compute_order_quantity(self, collar_cap_price: float, available_cash: Optional[float] = None) -> int:
        if self.current_slot_notional <= 0.0 or collar_cap_price <= 0.0:
            return 0
        target_qty = int(self.current_slot_notional // (collar_cap_price * 1.002))
        if available_cash is not None:
            return min(target_qty, max(0, int((available_cash * 0.995) // collar_cap_price)))
        return target_qty

    def build_entry_context(self, symbol: str) -> Dict[str, Any]:
        return {
            "day_open": self.scanner.get_day_open(symbol),
            "prev_close": self.scanner.get_prev_close(symbol),
        }

    @staticmethod
    def make_ref_bar_dict(bar: MinuteBar, signal: Optional[EntrySignal] = None) -> Dict[str, Any]:
        prior_atr = signal.metadata.get("prior_atr20_pct") if signal else None
        return make_ref_bar_dict(bar, prior_atr20_pct=prior_atr)

    def on_day_open(
        self,
        context_or_date: Any,
        current_date: Optional[date] = None,
        *,
        regime: Optional[MarketIndexRegime] = None,
        features: Optional[Dict[str, SymbolFeatures]] = None,
        prev_closes: Optional[Dict[str, float]] = None,
        cash: Optional[float] = None,
        execution_mode: Optional[ExecutionMode] = None,
    ) -> None:
        if isinstance(context_or_date, date) and current_date is None:
            ctx, d = None, context_or_date
        else:
            ctx, d = context_or_date, current_date
            if cash is None and ctx is not None and hasattr(ctx, "get_balance"):
                bal = ctx.get_balance()
                cash = bal.cash if bal else None

        self._active_candidates.clear()
        self._candidate_bar_min.clear()
        self._active_positions.clear()
        self._pending_buy_symbols.clear()
        self._pending_sell_symbols.clear()
        self._forming_bars.clear()
        self._mid_minute_promoted.clear()
        self._daily_trade_count = 0
        self._pending_ref_bars.clear()
        self._rejection_counts.clear()

        if execution_mode is not None:
            self.execution_mode = execution_mode

        if cash is not None and cash > 0.0:
            self.effective_capital = cash * 0.995 if cash < self.total_capital else self.total_capital
        else:
            self.effective_capital = self.total_capital

        slots = self.base_max_daily_positions if not self.dynamic_slots else max(1, self.base_slots)
        self.max_daily_positions = max(0, slots)
        self.current_slot_notional = (
            self.effective_capital / self.max_daily_positions if self.max_daily_positions > 0 else 0.0
        )
        if hasattr(self.entry, "target_notional"):
            self.entry.target_notional = self.current_slot_notional

        m_open = regime.market_open_time if regime is not None else time(9, 0)
        m_close = regime.market_close_time if regime is not None else time(15, 30)
        self.scanner.on_day_open(
            d,
            prev_closes=prev_closes,
            features=features,
            market_open_time=m_open,
            market_close_time=m_close,
            execution_mode=self.execution_mode,
        )
        self.entry.on_day_open(d, features=features, market_open_time=m_open, market_close_time=m_close)
        self.exit_policy.on_day_open(
            d, market_open_time=m_open, market_close_time=m_close, execution_mode=self.execution_mode
        )

    def handle_order_event(self, event: OrderEvent, current_position: Optional[Position]) -> None:
        if event.side == OrderSide.BUY:
            if event.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
                self._pending_buy_symbols.discard(event.symbol)
                cum_filled = getattr(event, "cum_filled_quantity", 0)
                has_filled = bool(current_position and current_position.quantity > 0) or event.filled_quantity > 0 or cum_filled > 0
                if not has_filled:
                    self._daily_trade_count = max(0, self._daily_trade_count - 1)
                    self._active_positions.discard(event.symbol)
                    if event.status == OrderStatus.REJECTED:
                        rej_cnt = self._rejection_counts.get(event.symbol, 0) + 1
                        self._rejection_counts[event.symbol] = rej_cnt
                        if rej_cnt >= 3:
                            logger.error(f"[REJECTION LOCKOUT] {event.symbol} rejected {rej_cnt}x today ({event.message})")
                        else:
                            self.entry.rollback_entered_symbol(event.symbol)
                    else:
                        self.entry.rollback_entered_symbol(event.symbol)
                self._pending_ref_bars.pop(event.symbol, None)
            elif event.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                if event.status == OrderStatus.FILLED:
                    self._pending_buy_symbols.discard(event.symbol)
                self._active_positions.add(event.symbol)
                if current_position is not None:
                    self._apply_buy_fill_to_position(current_position, event)
        elif event.side == OrderSide.SELL and event.status in (
            OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED
        ):
            self._pending_sell_symbols.discard(event.symbol)
            if event.status == OrderStatus.FILLED and (current_position is None or current_position.quantity <= 0):
                self._active_positions.discard(event.symbol)

    def _apply_buy_fill_to_position(self, position: Position, event: OrderEvent) -> None:
        ref_bar = self._pending_ref_bars.get(event.symbol)
        if ref_bar:
            position.metadata.setdefault("ref_bar", ref_bar)
            if ref_bar.get("prior_atr20_pct") is not None:
                position.metadata.setdefault("prior_atr20_pct", ref_bar["prior_atr20_pct"])
        if event.status == OrderStatus.FILLED:
            self._pending_ref_bars.pop(event.symbol, None)
        if hasattr(self.exit_policy, "apply_fill_targets"):
            self.exit_policy.apply_fill_targets(position)
        elif position.stop_loss_price <= 0.0:
            self.exit_policy.on_position_opened(position)

    # --- audit/ Stage-4 Facade Compatibility ---
    def on_start(self, context: Any) -> None:
        logger.info(f"[{self.name} Started]")

    def on_stop(self, context: Any) -> None:
        logger.info(f"[{self.name} Stopped]")

    def on_order_event(self, context: Any, event: OrderEvent) -> None:
        pos = context.get_position(event.symbol) if hasattr(context, "get_position") else None
        self.handle_order_event(event, pos)

    def on_bar(self, context: Any, bar: MinuteBar) -> None:
        pos = context.get_position(bar.symbol)
        if pos and pos.quantity > 0:
            bar_min = bar.dt.replace(second=0, microsecond=0)
            entry_min = pos.entry_dt.replace(second=0, microsecond=0) if pos.entry_dt else None
            if entry_min is not None and bar_min <= entry_min:
                if bar_min == entry_min and hasattr(self.exit_policy, "elevate_to_phase2"):
                    self.exit_policy.elevate_to_phase2(pos, ref_bar=bar)
                return
            sig = self.exit_policy.evaluate_exit(pos, bar)
            if sig:
                is_sl = "SL" in sig.reason or "STOP" in sig.reason
                exec_p = min(bar.open, sig.target_price) if (is_sl and sig.target_price and sig.target_price > 0) else bar.close
                context.liquidate(pos.symbol, price=exec_p, tag=sig.reason)
            return

        if self.is_daily_trading_completed or self.current_slot_notional <= 0.0 or bar.symbol in self._pending_buy_symbols:
            return
        cands = self.scanner.on_bar(bar.date, bar)
        if cands:
            self._active_candidates.update(cands)
        if self.scanner.is_disqualified(bar.symbol) and (not cands or bar.symbol not in cands):
            self._active_candidates.discard(bar.symbol)
            return
        if bar.symbol in self._active_candidates:
            entry_sig = self.entry.evaluate_entry(bar.symbol, bar, context_state=self.build_entry_context(bar.symbol))
            if entry_sig:
                bal = context.get_balance() if hasattr(context, "get_balance") else None
                avail_cash = bal.cash if bal is not None else None
                qty = self.compute_order_quantity(entry_sig.collar_cap_price, avail_cash)
                if qty <= 0:
                    self.entry.rollback_entered_symbol(bar.symbol)
                    return
                self._pending_ref_bars[bar.symbol] = self.make_ref_bar_dict(bar, entry_sig)
                oid = context.buy(symbol=bar.symbol, quantity=qty, price=entry_sig.collar_cap_price, tag=entry_sig.reason)
                if oid:
                    self._daily_trade_count += 1
                    self._active_positions.add(bar.symbol)
                    self._active_candidates.discard(bar.symbol)
                else:
                    self._pending_ref_bars.pop(bar.symbol, None)
                    self.entry.rollback_entered_symbol(bar.symbol)
