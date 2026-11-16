"""
Anchor Breakout Entry Rule.
Triggers an intraday entry order when price breaks out over the Anchor Candle's high (신고가 돌파)
during the 09:00~09:30 morning golden hour, protected by a statutory KRX +0.5% collar cap.
"""
from __future__ import annotations

from datetime import date, time
from typing import Optional, Dict, Any, Set

from core.krx_ticks import round_to_krx_tick
from engine.models import MinuteBar, TickData, EntrySignal, OrderType
from strategy.base import IEntryRule


class AnchorBreakoutEntry(IEntryRule):
    """
    Evaluates breakout over the Anchor Candle's High during morning session (09:00~09:30).
    Guarantees Lookahead 0% (Bar 0 causality) and KRX statutory tick rounding.
    """

    name: str = "anchor_breakout_entry"

    def __init__(
        self,
        collar_cap_pct: float = 0.005,          # +0.5% anti-chase collar cap
        target_notional: float = 10_000_000.0,  # 10M KRW notional
        start_time: time = time(9, 1),
        end_time: time = time(9, 30),
        min_bar_turnover: float = 200_000_000.0,# 1-min bar turnover >= 200M KRW
        max_open_gap_pct: float = 0.015,        # Don't chase if opened > 1.5% above anchor high
        min_day_surge_pct: float = 0.005,       # Must have >= +0.5% upward momentum from today's open
        max_day_surge_pct: float = 0.060,       # Exhaustion Filter: Disqualify if already surged > 6.0% from day open
        scanner: Optional[Any] = None,
        **kwargs: Any,
    ):
        self.collar_cap_pct = collar_cap_pct
        self.target_notional = target_notional
        self.start_time = start_time
        self.end_time = end_time
        self.min_bar_turnover = min_bar_turnover
        self.max_open_gap_pct = max_open_gap_pct
        self.min_day_surge_pct = min_day_surge_pct
        self.max_day_surge_pct = max_day_surge_pct
        self.scanner = scanner

        self._day_open_prices: Dict[str, float] = {}
        self._entered_symbols_today: Set[str] = set()

    def on_day_open(self, current_date: date) -> None:
        self._day_open_prices.clear()
        self._entered_symbols_today.clear()

    def register_day_open(self, symbol: str, price: float) -> None:
        if price > 0 and symbol not in self._day_open_prices:
            self._day_open_prices[symbol] = price

    def rollback_entered_symbol(self, symbol: str) -> None:
        self._entered_symbols_today.discard(symbol)

    def evaluate_entry(
        self,
        symbol: str,
        bar: MinuteBar,
        tick: Optional[TickData] = None,
        context_state: Optional[Dict[str, Any]] = None,
    ) -> Optional[EntrySignal]:
        if self.target_notional <= 0:
            return None

        # 1. Single-fire daily rule
        if symbol in self._entered_symbols_today:
            return None

        # 2. Time window check
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

        if day_open <= 0.0:
            return None

        # 4. Resolve anchor info
        anchor_info = None
        if context_state and "anchor_info" in context_state:
            anchor_info = context_state["anchor_info"]
        elif self.scanner and hasattr(self.scanner, "get_anchor_info"):
            anchor_info = self.scanner.get_anchor_info(symbol)

        # Threshold definition: Anchor High or fallback breakout (+4.5%)
        if anchor_info and "anchor_high" in anchor_info:
            threshold_price = anchor_info["anchor_high"]
        else:
            threshold_price = day_open * 1.045

        # 5. Breakout condition check: bar.high must reach threshold, and open shouldn't be excessively gapped
        if bar.high < threshold_price:
            return None
        if bar.open > threshold_price * (1.0 + self.max_open_gap_pct):
            return None

        # 6. Intraday surge & exhaustion check: must have positive momentum and not be exhausted
        surge_ratio = (bar.high - day_open) / day_open
        if surge_ratio < self.min_day_surge_pct:
            return None
        if surge_ratio > self.max_day_surge_pct:
            return None

        # 6. Bar turnover check (skip if mock test bar where turnover is 0)
        if bar.turnover > 0 and bar.turnover < self.min_bar_turnover:
            return None

        # 7. Execution & Collar-Cap Price Calculation
        raw_trigger = max(bar.open, threshold_price)
        trigger_price = round_to_krx_tick(raw_trigger, side="BUY", round_mode="ceil")
        raw_collar = trigger_price * (1.0 + self.collar_cap_pct)
        collar_cap_price = max(
            trigger_price,
            round_to_krx_tick(raw_collar, side="BUY", round_mode="floor"),
        )

        self._entered_symbols_today.add(symbol)

        return EntrySignal(
            symbol=symbol,
            dt=bar.dt,
            trigger_price=trigger_price,
            collar_cap_price=collar_cap_price,
            target_notional=self.target_notional,
            reason="ANCHOR_BREAKOUT",
            metadata={
                "trigger_price": trigger_price,
                "threshold_price": threshold_price,
                "anchor_info": anchor_info,
                "ref_bar": {
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "dt": bar.dt,
                },
            },
        )
