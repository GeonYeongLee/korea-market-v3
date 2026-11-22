"""
Open Window Entry Rule.
Executes entries within a configurable opening morning window (09:00 - 09:10/20/30).
Supports 3 selectable entry styles:
  1. 'orb'          : Opening Range High Breakout (breaching opening threshold/high)
  2. 'pullback'     : Open Support Bounce (testing day_open support and bouncing green)
  3. 'volume_surge' : High Volume Surge (green bar with surging turnover/volume)

Enforces:
  - Execution price: Close of the trigger bar.
  - Collar cap: trigger price * (1 + collar_cap_pct), rounded via round_to_krx_tick(..., side='buy') (Floor).
  - Bar 0 Lookahead 0%: strictly causal, never reads uncompleted future bar data.
  - Single-fire per symbol per trading session.
"""
from __future__ import annotations

import math
from datetime import date, time
from typing import Optional, Dict, Any, Set

from core.krx_ticks import round_to_krx_tick
from core.telemetry import telemetry
from engine.models import MinuteBar, TickData, EntrySignal
from strategy.base import IEntryRule


class OpenWindowEntry(IEntryRule):
    """
    Open Window modular entry rule.
    Executes on completed trigger bar close with statutory KRX tick collar cap.
    """

    name: str = "open_window_entry"

    def __init__(
        self,
        start_time_min: int = 1,               # 09:01:00 (allows 09:00 bar to establish initial reference)
        end_time_min: int = 10,                # 09:10:00 (strict morning burst window)
        entry_style: str = "orb",              # 'orb', 'pullback', 'volume_surge'
        breakout_pct: float = 0.035,           # +3.5% breakout threshold for ORB
        pullback_tolerance_pct: float = 0.005, # +0.5% tolerance buffer near day_open
        min_surge_turnover: float = 300_000_000.0,  # 300M KRW min turnover for volume surge
        volume_surge_mult: float = 1.5,        # 1.5x previous bar volume for surge
        collar_cap_pct: float = 0.005,         # +0.5% anti-chase collar cap
        target_notional: float = 10_000_000.0, # 10M KRW fixed single-position capital
        start_time: Optional[time] = None,     # Explicit override for start time
        end_time: Optional[time] = None,       # Explicit override for end time
        **kwargs: Any,
    ):
        self.start_time_min = start_time_min
        self.end_time_min = end_time_min
        self.entry_style = entry_style.lower()
        self.breakout_pct = breakout_pct
        self.pullback_tolerance_pct = pullback_tolerance_pct
        self.min_surge_turnover = min_surge_turnover
        self.volume_surge_mult = volume_surge_mult
        self.collar_cap_pct = collar_cap_pct
        self.target_notional = target_notional

        # Resolve start and end times (convert minutes from 09:00 if not explicitly provided)
        if start_time is not None:
            self.start_time = start_time
        else:
            self.start_time = time(9 + start_time_min // 60, start_time_min % 60)

        if end_time is not None:
            self.end_time = end_time
        else:
            self.end_time = time(9 + end_time_min // 60, end_time_min % 60)

        self._day_open_prices: Dict[str, float] = {}
        self._prior_high_prices: Dict[str, float] = {}
        self._prev_bar_volumes: Dict[str, int] = {}
        self._entered_symbols_today: Set[str] = set()

    def on_day_open(self, current_date: date) -> None:
        """Called at 09:00:00 to reset daily entry states."""
        self._day_open_prices.clear()
        self._prior_high_prices.clear()
        self._prev_bar_volumes.clear()
        self._entered_symbols_today.clear()

    def register_day_open(self, symbol: str, price: float) -> None:
        """External hook to register day open price from scanner or engine."""
        if price > 0 and symbol not in self._day_open_prices:
            self._day_open_prices[symbol] = price

    def rollback_entered_symbol(self, symbol: str) -> None:
        """Removes symbol from entered set if broker rejected or cancelled the order."""
        self._entered_symbols_today.discard(symbol)

    def _check_style_condition(self, symbol: str, bar: MinuteBar, day_open: float) -> bool:
        """Evaluates specific entry style condition strictly on available Bar 0 data."""
        if self.entry_style == "orb":
            # Opening Range Breakout: Breaches breakout threshold above day open
            threshold = day_open * (1.0 + self.breakout_pct)
            prior_high = self._prior_high_prices.get(symbol, threshold)
            trigger_level = max(threshold, prior_high)
            return bar.high >= trigger_level

        elif self.entry_style == "pullback":
            # Open Support Bounce: tests near day_open and closes green above day_open
            tested_support = bar.low <= day_open * (1.0 + self.pullback_tolerance_pct)
            held_above_open = bar.close >= day_open
            is_green = bar.close >= bar.open
            return tested_support and held_above_open and is_green

        elif self.entry_style == "volume_surge":
            # Volume Surge: Green candle with surging turnover/volume
            is_green = bar.close > bar.open
            turnover_met = bar.turnover >= self.min_surge_turnover
            prev_vol = self._prev_bar_volumes.get(symbol, 0)
            vol_surged = (prev_vol > 0 and bar.volume >= self.volume_surge_mult * prev_vol)
            return is_green and (turnover_met or vol_surged)

        # Fallback: ORB default
        return bar.high >= day_open * (1.0 + self.breakout_pct)

    def evaluate_entry(
        self,
        symbol: str,
        bar: MinuteBar,
        tick: Optional[TickData] = None,
        context_state: Optional[Dict[str, Any]] = None,
    ) -> Optional[EntrySignal]:
        """
        Evaluates open window entry signal.
        Execution price: Close of the trigger bar (or tick price in live real-time tick feed).
        Collar cap: trigger price * (1 + collar_cap_pct) rounded with Floor.
        """
        if self.target_notional <= 0:
            return None

        # Zero volume guard: illiquid or unformed bar
        if bar.volume <= 0:
            return None

        # Static / Dynamic VI Defense: Reject entry during call auction (single-price matching)
        if context_state and (context_state.get("is_vi") or context_state.get("vi_cls_code") in ("Y", "1", "2")):
            return None

        # 1. Single-fire daily invariant
        if symbol in self._entered_symbols_today:
            return None

        # 2. Entry window check
        t = tick.dt.time() if tick else bar.time
        if t < self.start_time or t > self.end_time:
            return None

        # 3. Resolve day open
        day_open = 0.0
        if context_state and "day_open" in context_state and context_state["day_open"]:
            day_open = context_state["day_open"]
            self._day_open_prices[symbol] = day_open
        elif symbol in self._day_open_prices:
            day_open = self._day_open_prices[symbol]
        else:
            day_open = bar.open
            self._day_open_prices[symbol] = day_open

        if day_open <= 0.0 or not math.isfinite(day_open):
            return None

        # 4. Check entry style condition
        threshold = day_open * (1.0 + self.breakout_pct)
        prior_high = self._prior_high_prices.get(symbol, threshold)
        triggered = self._check_style_condition(symbol, bar, day_open)

        # Update historical trackers for subsequent bars
        if symbol not in self._prior_high_prices:
            self._prior_high_prices[symbol] = bar.high
        else:
            self._prior_high_prices[symbol] = max(self._prior_high_prices[symbol], bar.high)
        self._prev_bar_volumes[symbol] = bar.volume

        if not triggered:
            return None

        # 5. Determine execution price and KRX statutory tick collar cap
        if self.entry_style == "orb":
            raw_trigger = tick.price if tick else max(threshold, prior_high)
        else:
            raw_trigger = tick.price if tick else bar.close

        if raw_trigger <= 0.0 or not math.isfinite(raw_trigger):
            return None

        trigger_price = round_to_krx_tick(raw_trigger, side="BUY", round_mode="floor")

        # Collar cap: trigger price * (1 + collar_cap_pct) rounded Floor, clamped to KRX +30% statutory limit-up
        raw_collar = trigger_price * (1.0 + self.collar_cap_pct)
        collar_cap_price = max(
            trigger_price,
            round_to_krx_tick(raw_collar, side="BUY", round_mode="floor"),
        )
        pc = context_state.get("prev_close") if context_state else None
        limit_base = pc if pc and pc > 0 else (day_open * 0.95)
        max_limit_up = round_to_krx_tick(limit_base * 1.30, side="BUY", round_mode="floor")
        collar_cap_price = min(collar_cap_price, max_limit_up)

        eval_dt = tick.dt if tick else bar.dt
        metrics = {
            "trigger_price": trigger_price,
            "collar_cap_price": collar_cap_price,
            "day_open": day_open,
            "entry_style": self.entry_style,
            "target_notional": self.target_notional,
        }

        # 6. Anti-chase Collar Cap Check
        if bar.open > collar_cap_price:
            telemetry.record_strategy_eval(
                "ENTRY", symbol, "REJECT", f"FAIL_COLLAR_CAP_OPEN ({bar.open} > {collar_cap_price})",
                metrics, dt=eval_dt,
            )
            return None

        if tick and tick.price > collar_cap_price:
            telemetry.record_strategy_eval(
                "ENTRY", symbol, "REJECT", f"FAIL_COLLAR_CAP_TICK ({tick.price} > {collar_cap_price})",
                metrics, dt=eval_dt,
            )
            return None

        # 7. Lock single-fire and issue EntrySignal
        self._entered_symbols_today.add(symbol)
        reason_tag = f"OPEN_WINDOW_{self.entry_style.upper()}"
        telemetry.record_strategy_eval("ENTRY", symbol, "PASS", reason_tag, metrics, dt=eval_dt)

        return EntrySignal(
            symbol=symbol,
            dt=eval_dt,
            trigger_price=trigger_price,
            collar_cap_price=collar_cap_price,
            target_notional=self.target_notional,
            reason=reason_tag,
            metadata={
                "day_open": day_open,
                "entry_style": self.entry_style,
                "collar_cap_pct": self.collar_cap_pct,
            },
        )
