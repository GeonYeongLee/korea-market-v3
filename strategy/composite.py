"""Composite Strategy Orchestrator: Assembles Scanner, EntryRule, ExitPolicy into a unified strategy."""
from __future__ import annotations

import logging
from datetime import date, time
from typing import Optional, List, Dict, Set, Any

from core.market_regime import MarketRegimeProvider, get_default_market_regime_provider
from core.dynamic_features import DynamicFeatureProvider, get_default_dynamic_feature_provider
from core.krx_ticks import round_to_krx_tick
from engine.models import MinuteBar, TickData, OrderEvent, OrderStatus, OrderSide, Position
from strategy.base import BaseStrategy, IScanner, IEntryRule, IExitPolicy

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
        self._daily_trade_count: int = 0
        self._pending_ref_bars: Dict[str, Dict[str, Any]] = {}

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
            desc = "Bear Market Active (1 Slot)" if idx_feat.is_bear_market else "Non-Bear Market Inactive (Bear Strategy Dormant)"
            return self._set_regime(slots, desc)

        if self.regime_mode == "all_weather" and idx_feat.is_bear_market:
            return self._set_regime(1, "All-Weather: Bear Market Active (1 Slot)")

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
        self._active_candidates.clear()
        self._active_positions.clear()
        self._daily_trade_count = 0
        self._pending_ref_bars.clear()

        # Synchronize capital with actual cash if available from context
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
        # EOD Hard Failsafe: force close any remaining positions to guarantee 0% overnight risk
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

    def on_bar(self, context: Any, bar: MinuteBar) -> None:
        # Step 1: Manage Active Positions via Exit Policy
        position: Optional[Position] = context.get_position(bar.symbol)
        if position and position.quantity > 0:
            exit_signal = self.exit_policy.evaluate_exit(position, bar)
            if exit_signal:
                has_target = exit_signal.target_price is not None and exit_signal.target_price > 0
                is_sl = "SL" in exit_signal.reason or "STOP" in exit_signal.reason
                exec_price = min(bar.open, exit_signal.target_price) if (is_sl and has_target) else bar.close
                tgt_str = f"{exit_signal.target_price:,.0f}" if has_target else "N/A"
                logger.info(
                    f"🔻 [EXIT SIGNAL] {bar.symbol} Reason: {exit_signal.reason} | "
                    f"Target: {tgt_str} KRW | Exec: {exec_price:,.0f} KRW"
                )
                context.liquidate(position.symbol, price=exec_price, tag=exit_signal.reason)
                self._active_positions.discard(position.symbol)
            return

        # Step 2: Check Daily Limit
        if self._daily_trade_count >= self.max_daily_positions or self.current_slot_notional <= 0:
            return

        # Step 3: Universe Candidate Discovery via Scanner
        candidates = self.scanner.on_bar(bar.date, bar)
        if candidates:
            self._active_candidates.update(candidates)

        if hasattr(self.scanner, "is_disqualified") and self.scanner.is_disqualified(bar.symbol):
            self._active_candidates.discard(bar.symbol)
            return

        # Step 4: Entry Evaluation for Candidate Stocks
        if bar.symbol in self._active_candidates:
            day_open = getattr(self.scanner, "get_day_open", lambda s: None)(bar.symbol)
            prev_close = (
                getattr(self.scanner, "get_prev_close", lambda s: None)(bar.symbol)
                or getattr(self.scanner, "_day_prev_closes", {}).get(bar.symbol)
            )
            sc_meta = getattr(self.scanner, "get_symbol_metadata", lambda s: {})(bar.symbol) or {}
            is_vi = bool(getattr(self.scanner, "is_vi", lambda s: False)(bar.symbol) or sc_meta.get("is_vi") or getattr(bar, "is_vi", False))
            vi_cls = getattr(self.scanner, "get_vi_cls_code", lambda s: None)(bar.symbol) or sc_meta.get("vi_cls_code") or getattr(bar, "vi_cls_code", None)
            ctx_state = {
                "day_open": day_open,
                "prev_close": prev_close,
                "is_vi": is_vi,
                "vi_cls_code": vi_cls,
            }
            entry_signal = self.entry.evaluate_entry(bar.symbol, bar, context_state=ctx_state)
            if entry_signal and self.current_slot_notional > 0 and entry_signal.collar_cap_price > 0:
                # Include slippage and commission buffer (0.5%) to guarantee execution without cash breach
                est_exec_price = entry_signal.collar_cap_price * 1.002
                target_qty = int(self.current_slot_notional // est_exec_price)
                bal = context.get_balance() if hasattr(context, "get_balance") else None
                if bal and bal.cash > 0:
                    max_affordable_qty = int((bal.cash * 0.995) // entry_signal.collar_cap_price)
                    qty = min(target_qty, max_affordable_qty)
                else:
                    qty = target_qty
                if qty > 0:
                    logger.info(
                        f"🎯 [ENTRY SIGNAL] {bar.symbol} Trigger: {entry_signal.trigger_price:,.0f} | "
                        f"Collar Cap: {entry_signal.collar_cap_price:,.0f} | Qty: {qty} shares"
                    )
                    prior_atr = entry_signal.metadata.get("prior_atr20_pct")
                    ref_bar = {
                        "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close,
                        "dt": bar.dt, "turnover": bar.turnover, "volume": bar.volume,
                        "prior_atr20_pct": prior_atr,
                    }
                    self._pending_ref_bars[bar.symbol] = ref_bar
                    order_id = context.buy(
                        symbol=bar.symbol, quantity=qty, price=entry_signal.collar_cap_price, tag=entry_signal.reason
                    )
                    if order_id:
                        self._daily_trade_count += 1
                        self._active_positions.add(bar.symbol)
                        self._active_candidates.discard(bar.symbol)
                        pos = context.get_position(bar.symbol)
                        if pos:
                            pos.metadata.setdefault("ref_bar", ref_bar)
                            if prior_atr is not None:
                                pos.metadata.setdefault("prior_atr20_pct", prior_atr)
                    else:
                        self._pending_ref_bars.pop(bar.symbol, None)

    def on_tick(self, context: Any, tick: TickData) -> None:
        position: Optional[Position] = context.get_position(tick.symbol)
        if position and position.quantity > 0:
            exit_signal = self.exit_policy.evaluate_exit(position, None, tick=tick)
            if exit_signal:
                context.liquidate(tick.symbol, price=tick.price, tag=exit_signal.reason)
                self._active_positions.discard(tick.symbol)

    def on_order_event(self, context: Any, event: OrderEvent) -> None:
        if event.side == OrderSide.BUY:
            if event.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
                if event.filled_quantity == 0:
                    self._daily_trade_count = max(0, self._daily_trade_count - 1)
                    self._active_positions.discard(event.symbol)
                    if hasattr(self.entry, "rollback_entered_symbol"):
                        self.entry.rollback_entered_symbol(event.symbol)
                self._pending_ref_bars.pop(event.symbol, None)
            elif event.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                self._active_positions.add(event.symbol)
                position = context.get_position(event.symbol)
                if position:
                    ref_bar = self._pending_ref_bars.get(event.symbol)
                    if ref_bar:
                        position.metadata.setdefault("ref_bar", ref_bar)
                        if ref_bar.get("prior_atr20_pct") is not None:
                            position.metadata.setdefault("prior_atr20_pct", ref_bar["prior_atr20_pct"])
                    if event.status == OrderStatus.FILLED:
                        self._pending_ref_bars.pop(event.symbol, None)

                    if position.stop_loss_price <= 0.0:
                        self.exit_policy.on_position_opened(position)
                    else:
                        eff_sl = position.metadata.get("emergency_sl_pct", getattr(self.exit_policy, "emergency_sl_pct", -0.020))
                        eff_tp = position.metadata.get("tp_target_pct", getattr(self.exit_policy, "tp_target_pct", 0.030))
                        position.stop_loss_price = round_to_krx_tick(position.avg_price * (1.0 + eff_sl), side="SELL", round_mode="ceil")
                        position.take_profit_price = round_to_krx_tick(position.avg_price * (1.0 + eff_tp), side="SELL", round_mode="ceil")
        elif event.side == OrderSide.SELL and event.status == OrderStatus.FILLED:
            self._active_positions.discard(event.symbol)
