"""Composite Strategy Orchestrator: Assembles Scanner, EntryRule, ExitPolicy into a unified strategy."""
from __future__ import annotations

import logging
import threading
from datetime import date, time
from typing import Optional, List, Dict, Set, Any

from core.market_regime import MarketRegimeProvider, get_default_market_regime_provider
from core.dynamic_features import DynamicFeatureProvider, get_default_dynamic_feature_provider
from engine.models import MinuteBar, TickData, OrderEvent, OrderStatus, OrderSide, Position
from strategy.base import BaseStrategy, IScanner, IEntryRule, IExitPolicy
from strategy import live_sync

logger = logging.getLogger("korea_market.strategy.composite")


class CompositeStrategy(BaseStrategy):
    """Modular trading strategy composed of scanner, entry, and exit policy."""

    def __init__(
        self,
        scanner: IScanner, entry: IEntryRule, exit_policy: IExitPolicy,
        max_daily_positions: int = 1, total_capital: float = 10_000_000.0,
        dynamic_slots: bool = False, base_slots: int = 2, expanded_slots: int = 5,
        kospi_gap_threshold: float = 0.0, max_market_gap: Optional[float] = None,
        severe_gap_down_filter: Optional[float] = None, use_dual_index: bool = False,
        gap_filter_index: str = "max", dynamic_max_gap: bool = True,
        bear_market_defense: bool = True, regime_provider: Optional[MarketRegimeProvider] = None,
        dynamic_feature_provider: Optional[DynamicFeatureProvider] = None,
        regime_mode: Optional[str] = None, name: str = "composite_strategy",
    ):
        self.scanner, self.entry, self.exit_policy = scanner, entry, exit_policy
        self.base_max_daily_positions = self.max_daily_positions = max_daily_positions
        self.total_capital, self.dynamic_slots = total_capital, dynamic_slots
        self.base_slots, self.expanded_slots = base_slots, expanded_slots
        self.kospi_gap_threshold, self.max_market_gap = kospi_gap_threshold, max_market_gap
        self.severe_gap_down_filter, self.use_dual_index = severe_gap_down_filter, use_dual_index
        self.gap_filter_index = gap_filter_index.lower()
        self.dynamic_max_gap, self.bear_market_defense = dynamic_max_gap, bear_market_defense
        self.regime_provider, self.dynamic_feature_provider = regime_provider, dynamic_feature_provider
        self.regime_mode, self.name = regime_mode, name

        self.current_slot_notional: float = (total_capital / max(1, max_daily_positions)) if max_daily_positions > 0 else 0.0
        self._active_candidates: Set[str] = set()
        self._active_positions: Set[str] = set()
        self._pending_buy_symbols: Set[str] = set()
        self._pending_sell_symbols: Set[str] = set()
        self._mid_minute_promoted: Set[str] = set()
        self._forming_bars: Dict[str, MinuteBar] = {}
        self._daily_trade_count: int = 0
        self._pending_ref_bars: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    @property
    def is_daily_trading_completed(self) -> bool:
        """Returns True if the maximum daily trade capacity has been filled."""
        return self._daily_trade_count >= self.max_daily_positions

    @property
    def cutoff_time(self) -> time:
        """Computes safe cutoff time after which no new candidate or entry can trigger."""
        scanner_end = getattr(self.scanner, "end_time", time(9, 3))
        entry_end = getattr(self.entry, "end_time", time(9, 3))
        latest = max(scanner_end, entry_end)
        m = latest.minute + 5
        return time(latest.hour + m // 60, m % 60)

    def on_start(self, context: Any) -> None:
        logger.info(f"🚀 [{self.name} Started] Scanner: {self.scanner.name} | Entry: {self.entry.name} | Exit: {self.exit_policy.name}")

    def on_stop(self, context: Any) -> None:
        logger.info(f"🛑 [{self.name} Stopped]")

    def _set_regime(self, slots: int, desc: str) -> None:
        self.max_daily_positions = max(0, slots)
        effective = getattr(self, "effective_capital", self.total_capital)
        self.current_slot_notional = (effective / max(1, self.max_daily_positions)) if self.max_daily_positions > 0 else 0.0
        suffix = f" ({self.current_slot_notional:,.0f} KRW/slot)" if self.max_daily_positions > 0 else ""
        logger.info(f"🧭 [{self.name}] Daily Regime Configured: {desc} -> {self.max_daily_positions} slot{'s' if self.max_daily_positions != 1 else ''}{suffix}")

    def sync_capital_with_balance(self, available_cash: float) -> None:
        """Dynamically caps trading capital to actual available cash (buffer 0.5%) if under total_capital."""
        if available_cash <= 0:
            return
        self.effective_capital = available_cash * 0.995 if available_cash < self.total_capital else self.total_capital
        if self.max_daily_positions > 0:
            self.current_slot_notional = self.effective_capital / self.max_daily_positions
            if hasattr(self.entry, "target_notional"):
                self.entry.target_notional = self.current_slot_notional
            logger.info(f"💰 [{self.name}] Capital Synced: Cash {available_cash:,.0f} -> Effective {self.effective_capital:,.0f} ({self.current_slot_notional:,.0f} KRW/slot)")

    def _configure_regime(self, current_date: date) -> None:
        provider = self.regime_provider or get_default_market_regime_provider()
        dyn_provider = self.dynamic_feature_provider or get_default_dynamic_feature_provider()
        ks_gap = provider.get_kospi_gap(current_date) if hasattr(provider, "get_kospi_gap") else None
        kq_gap = provider.get_kosdaq_gap(current_date) if hasattr(provider, "get_kosdaq_gap") else None
        idx_feat = dyn_provider.get_index_features(current_date)

        # 0. Custom Regime Mode Gating
        if self.regime_mode == "bear_only":
            slots = self.base_max_daily_positions if idx_feat.is_bear_market else 0
            desc = "Bear Market Active" if idx_feat.is_bear_market else "Non-Bear Market Inactive (Bear Strategy Dormant)"
            return self._set_regime(slots, desc)

        if self.regime_mode in ("all_weather", "unified"):
            slots = self.base_max_daily_positions if not self.dynamic_slots else max(1, self.base_slots)
            return self._set_regime(slots, f"Unified Regime-Free Active (KS MA20 {idx_feat.ks_trend:+.2%}, KQ MA20 {idx_feat.kq_trend:+.2%})")

        # 1. Bear Market Defense: If both KOSPI and KOSDAQ are below MA20 on t-1 -> 0 slots
        if self.bear_market_defense and idx_feat.is_bear_market:
            return self._set_regime(0, f"Dual-Index Bear Market Defense (KS MA20 {idx_feat.ks_trend:+.2%}, KQ MA20 {idx_feat.kq_trend:+.2%})")

        # 2. Gap-Up Trap Filter
        eff_gap = kq_gap if self.gap_filter_index == "kosdaq" else (
            provider.get_max_gap(current_date) if self.gap_filter_index == "max" and hasattr(provider, "get_max_gap") else ks_gap
        )
        max_gap = idx_feat.dynamic_max_gap if self.dynamic_max_gap else self.max_market_gap
        if max_gap is not None and eff_gap is not None and eff_gap > max_gap:
            gt = "Dynamic" if self.dynamic_max_gap else self.gap_filter_index.upper()
            return self._set_regime(0, f"{gt} Gap-Up Trap Filter ({eff_gap:+.2f}% > {max_gap:+.2f}%)")

        if not self.dynamic_slots:
            return self._set_regime(self.base_max_daily_positions, "Static Slots")

        # 3. Dynamic Slots Allocation
        min_gap = provider.get_min_gap(current_date) if hasattr(provider, "get_min_gap") else (
            min(ks_gap, kq_gap) if ks_gap is not None and kq_gap is not None else ks_gap
        )
        if self.severe_gap_down_filter is not None and min_gap is not None and min_gap < self.severe_gap_down_filter:
            return self._set_regime(0, f"Severe Gap Down ({min_gap:+.2f}% < {self.severe_gap_down_filter:.1f}%)")

        if self.use_dual_index and ks_gap is not None and kq_gap is not None:
            if ks_gap < 0.0 and kq_gap >= 0.0:
                slots, q = max(1, self.expanded_slots), "Q2"
            elif ks_gap < 0.0 and kq_gap < 0.0:
                slots, q = max(1, min(3, self.expanded_slots)), "Q1"
            elif ks_gap >= 0.0 and kq_gap < 0.0:
                slots, q = 1, "Q3"
            else:
                slots, q = max(1, self.base_slots), "Q4"
            return self._set_regime(slots, f"Dual-Index {q} (KS {ks_gap:+.2f}%, KQ {kq_gap:+.2f}%)")

        is_expanded = ks_gap is not None and ks_gap < self.kospi_gap_threshold
        slots = max(1, self.expanded_slots) if is_expanded else max(1, self.base_slots)
        op = "<" if is_expanded else ">="
        gap_val = ks_gap if ks_gap is not None else 0.0
        self._set_regime(slots, f"KOSPI Gap {gap_val:+.2f}% {op} {self.kospi_gap_threshold:.1f}%")

    def on_day_open(self, context: Any, current_date: date) -> None:
        with self._lock:
            self._active_candidates.clear()
            self._active_positions.clear()
            self._pending_buy_symbols.clear()
            self._pending_sell_symbols.clear()
            self._mid_minute_promoted.clear()
            self._forming_bars.clear()
            self._daily_trade_count = 0
            self._pending_ref_bars.clear()

            bal = context.get_balance() if hasattr(context, "get_balance") else None
            if bal and bal.cash > 0:
                self.effective_capital = bal.cash * 0.995 if bal.cash < self.total_capital else self.total_capital
            else:
                self.effective_capital = getattr(self, "effective_capital", self.total_capital)

            self._configure_regime(current_date)
            if hasattr(self.entry, "target_notional"):
                self.entry.target_notional = self.current_slot_notional
            self.scanner.on_day_open(current_date)
            self.entry.on_day_open(current_date)
            self.exit_policy.on_day_open(current_date)

    def on_day_close(self, context: Any, current_date: date) -> None:
        with self._lock:
            active = set(self._active_positions)
            if hasattr(context, "get_positions"):
                active.update(s for s, p in context.get_positions().items() if p and p.quantity > 0)
            for sym in active:
                pos = context.get_position(sym)
                if pos and pos.quantity > 0:
                    logger.warning(f"🚨 [EOD FAILSAFE] Liquidating unclosed {sym} at day close {current_date}")
                    context.liquidate(sym, price=pos.current_price, tag="EOD_FAILSAFE_LIQUIDATION")
            self._active_positions.clear()
            self._active_candidates.clear()
            self._pending_buy_symbols.clear()
            self._pending_sell_symbols.clear()

    def register_symbol_day_open(
        self, symbol: str, day_open: float, day_low: float = 0.0, cum_turnover: float = 0.0,
        prev_close: float = 0.0, is_promoted: bool = True,
    ) -> None:
        with self._lock:
            live_sync.register_symbol_day_open(self, symbol, day_open, day_low=day_low, prev_close=prev_close)

    def register_symbol_minute_bars(self, symbol: str, bars: List[MinuteBar], **kwargs: Any) -> List[str]:
        with self._lock:
            return live_sync.register_symbol_minute_bars(self, symbol, bars, **kwargs)

    def on_bar(self, context: Any, bar: MinuteBar) -> None:
        with self._lock:
            position: Optional[Position] = context.get_position(bar.symbol)
            if position and position.quantity > 0:
                bar_min = bar.dt.replace(second=0, microsecond=0)
                entry_min = position.entry_dt.replace(second=0, microsecond=0) if position.entry_dt else None
                if entry_min is not None and bar_min <= entry_min:
                    if bar_min == entry_min:
                        live_sync.sync_completed_entry_bar(self, position, bar)
                    return
                if bar.symbol not in self._pending_sell_symbols:
                    sig = self.exit_policy.evaluate_exit(position, bar)
                    if sig:
                        if bar.symbol in self._pending_buy_symbols and hasattr(getattr(context, "broker", None), "cancel_pending_orders"):
                            context.broker.cancel_pending_orders(bar.symbol)
                        is_sl = "SL" in sig.reason or "STOP" in sig.reason
                        exec_p = min(bar.open, sig.target_price) if (is_sl and sig.target_price and sig.target_price > 0) else bar.close
                        logger.info(f"🔻 [EXIT SIGNAL] {bar.symbol} Reason: {sig.reason} | Target: {f'{sig.target_price:,.0f}' if sig.target_price else 'N/A'} KRW | Exec: {exec_p:,.0f} KRW")
                        if context.liquidate(position.symbol, price=exec_p, tag=sig.reason):
                            rem = context.get_position(position.symbol)
                            if rem and rem.quantity > 0:
                                self._pending_sell_symbols.add(position.symbol)
                            else:
                                self._active_positions.discard(position.symbol)
                return

            if self._daily_trade_count >= self.max_daily_positions or self.current_slot_notional <= 0 or bar.symbol in self._pending_buy_symbols:
                return
            candidates = self.scanner.on_bar(bar.date, bar)
            if candidates:
                self._active_candidates.update(candidates)
            if hasattr(self.scanner, "is_disqualified") and self.scanner.is_disqualified(bar.symbol):
                self._active_candidates.discard(bar.symbol)
                return
            if bar.symbol in self._active_candidates:
                entry_sig = self.entry.evaluate_entry(bar.symbol, bar, context_state=live_sync.build_entry_ctx(self, bar))
                if entry_sig:
                    live_sync.submit_entry_order(self, context, bar.symbol, bar, entry_sig)

    def on_tick(self, context: Any, tick: TickData) -> None:
        with self._lock:
            position: Optional[Position] = context.get_position(tick.symbol)
            if position and position.quantity > 0:
                if tick.symbol not in self._pending_sell_symbols:
                    sig = self.exit_policy.evaluate_exit(position, None, tick=tick)
                    if sig:
                        if tick.symbol in self._pending_buy_symbols and hasattr(getattr(context, "broker", None), "cancel_pending_orders"):
                            context.broker.cancel_pending_orders(tick.symbol)
                        if context.liquidate(tick.symbol, price=tick.price, tag=sig.reason):
                            rem = context.get_position(tick.symbol)
                            if rem and rem.quantity > 0:
                                self._pending_sell_symbols.add(tick.symbol)
                            else:
                                self._active_positions.discard(tick.symbol)
                return
            if self._daily_trade_count >= self.max_daily_positions or self.current_slot_notional <= 0 or tick.symbol in self._pending_buy_symbols:
                return
            forming_bar = live_sync.update_forming_bar(self._forming_bars, tick, self._mid_minute_promoted)
            live_sync.try_qualify_tick(self, tick, forming_bar)
            if tick.symbol in self._active_candidates:
                eval_bar = None if tick.symbol in self._mid_minute_promoted else forming_bar
                entry_sig = self.entry.evaluate_entry(tick.symbol, bar=eval_bar, tick=tick, context_state=live_sync.build_entry_ctx(self, forming_bar))
                if entry_sig:
                    live_sync.submit_entry_order(self, context, tick.symbol, forming_bar, entry_sig)

    def on_order_event(self, context: Any, event: OrderEvent) -> None:
        with self._lock:
            if event.side == OrderSide.BUY:
                if event.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
                    self._pending_buy_symbols.discard(event.symbol)
                    pos_now = context.get_position(event.symbol)
                    has_filled = bool(pos_now and pos_now.quantity > 0)
                    if event.filled_quantity == 0 and not has_filled:
                        self._daily_trade_count = max(0, self._daily_trade_count - 1)
                        self._active_positions.discard(event.symbol)
                        if hasattr(self.entry, "rollback_entered_symbol"):
                            self.entry.rollback_entered_symbol(event.symbol)
                    self._pending_ref_bars.pop(event.symbol, None)
                elif event.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                    if event.status == OrderStatus.FILLED:
                        self._pending_buy_symbols.discard(event.symbol)
                    self._active_positions.add(event.symbol)
                    position = context.get_position(event.symbol)
                    if position:
                        live_sync.handle_buy_fill(self, position, event)
            elif event.side == OrderSide.SELL and event.status in (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED):
                self._pending_sell_symbols.discard(event.symbol)
                if event.status == OrderStatus.FILLED:
                    self._active_positions.discard(event.symbol)
