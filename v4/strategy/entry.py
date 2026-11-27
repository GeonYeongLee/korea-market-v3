"""
All-Weather Morning Breakout Entry Rule for Korea Market v4.
Pure synchronous IEntryComponent with Bar 0 causal integrity (no bar.close dependency)
and statutory KRX tick alignment.
"""
from __future__ import annotations

from datetime import date, time
from typing import Any, Dict, Optional, Set

from core.krx_ticks import round_to_krx_tick
from v4.interfaces import IEntryComponent
from v4.market_models import shift_from_0900
from v4.models import EntrySignal, MinuteBar, SymbolFeatures, TickEvent


class AllWeatherEntryV4(IEntryComponent):
    """
    Evaluates +4.5% (or ATR20-dynamic) morning breakout from day open,
    capped at +0.5% collar cap with exhaustion filter and single-fire daily lock.
    """

    name: str = "all_weather_entry"

    def __init__(
        self,
        breakout_pct: float = 0.045,
        collar_cap_pct: float = 0.005,
        target_notional: float = 10_000_000.0,
        start_time: time = time(9, 1),
        end_time: time = time(9, 15),
        use_dynamic_breakout: bool = True,
        exhaustion_filter_ratio: float = 0.65,
        shift_to_market_open: bool = True,
    ) -> None:
        self.breakout_pct = breakout_pct
        self.collar_cap_pct = collar_cap_pct
        self.target_notional = target_notional
        self.base_start_time = start_time
        self.base_end_time = end_time
        self.start_time = start_time
        self.end_time = end_time
        self.use_dynamic_breakout = use_dynamic_breakout
        self.exhaustion_filter_ratio = exhaustion_filter_ratio
        self.shift_to_market_open = shift_to_market_open

        self._day_open_prices: Dict[str, float] = {}
        self._entered_symbols_today: Set[str] = set()
        self._day_features: Dict[str, SymbolFeatures] = {}

    def on_day_open(
        self,
        current_date: date,
        features: Optional[Dict[str, SymbolFeatures]] = None,
        market_open_time: time = time(9, 0),
        market_close_time: time = time(15, 30),
    ) -> None:
        self._day_open_prices.clear()
        self._entered_symbols_today.clear()
        self._day_features = dict(features) if features else {}
        if self.shift_to_market_open:
            self.start_time = shift_from_0900(self.base_start_time, market_open_time)
            self.end_time = shift_from_0900(self.base_end_time, market_open_time)
        else:
            self.start_time = self.base_start_time
            self.end_time = self.base_end_time

    def register_day_open(self, symbol: str, price: float) -> None:
        if price > 0.0 and symbol not in self._day_open_prices:
            self._day_open_prices[symbol] = float(price)

    def rollback_entered_symbol(self, symbol: str) -> None:
        self._entered_symbols_today.discard(symbol)

    def evaluate_entry(
        self,
        symbol: str,
        bar: Optional[MinuteBar] = None,
        tick: Optional[TickEvent] = None,
        context_state: Optional[Dict[str, Any]] = None,
    ) -> Optional[EntrySignal]:
        if self.target_notional <= 0.0 or (bar is None and tick is None):
            return None
        if symbol in self._entered_symbols_today:
            return None

        eval_dt = tick.dt if tick is not None else bar.dt
        if eval_dt.time() < self.start_time or eval_dt.time() > self.end_time:
            return None

        day_open = self._resolve_day_open(symbol, bar, tick, context_state)
        if day_open <= 0.0:
            return None

        eff_breakout = self.breakout_pct
        eff_atr = self.breakout_pct / 0.40
        if self.use_dynamic_breakout and symbol in self._day_features:
            feat = self._day_features[symbol]
            if feat.prior_atr20_pct > 0.0:
                eff_atr = feat.prior_atr20_pct
                eff_breakout = min(0.055, max(0.025, 0.40 * eff_atr))
                if (eff_breakout / eff_atr) > self.exhaustion_filter_ratio:
                    return None

        raw_trigger = day_open * (1.0 + eff_breakout)
        trigger_price = round_to_krx_tick(raw_trigger, side="BUY", round_mode="ceil")
        raw_collar = trigger_price * (1.0 + self.collar_cap_pct)
        collar_cap_price = max(
            trigger_price,
            round_to_krx_tick(raw_collar, side="BUY", round_mode="floor"),
        )

        # Anti-chase Collar Cap checks (Bar 0 causal integrity: bar.close is never read)
        if bar is not None and bar.open > collar_cap_price:
            return None
        if tick is not None and tick.price > collar_cap_price:
            return None

        current_high = tick.price if tick is not None else bar.high
        if current_high < trigger_price:
            return None

        self._entered_symbols_today.add(symbol)
        return EntrySignal(
            symbol=symbol,
            dt=eval_dt,
            trigger_price=trigger_price,
            collar_cap_price=collar_cap_price,
            target_notional=self.target_notional,
            reason="MORNING_BREAKOUT",
            metadata={
                "day_open": day_open,
                "breakout_pct": eff_breakout,
                "prior_atr20_pct": eff_atr,
                "collar_cap_pct": self.collar_cap_pct,
            },
        )

    def _resolve_day_open(
        self,
        symbol: str,
        bar: Optional[MinuteBar],
        tick: Optional[TickEvent],
        context_state: Optional[Dict[str, Any]],
    ) -> float:
        if context_state and context_state.get("day_open"):
            val = float(context_state["day_open"])
            if val > 0.0:
                self._day_open_prices[symbol] = val
                return val
        if symbol in self._day_open_prices:
            return self._day_open_prices[symbol]
        if tick is not None and tick.day_open > 0.0:
            self._day_open_prices[symbol] = float(tick.day_open)
            return float(tick.day_open)
        if bar is not None and bar.open > 0.0:
            self._day_open_prices[symbol] = float(bar.open)
            return float(bar.open)
        return 0.0
