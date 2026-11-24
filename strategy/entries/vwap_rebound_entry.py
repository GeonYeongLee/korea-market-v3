"""
VWAP Rebound Entry Rule.
Triggers an entry when price reclaims Day Open and VWAP with surging liquidity,
strictly capped at +0.5% collar cap to prevent chasing into illiquid spreads.
"""
from __future__ import annotations

import math
from datetime import date, time
from typing import Optional, Dict, Any, Set

from core.krx_ticks import round_to_krx_tick
from core.telemetry import telemetry
from engine.models import MinuteBar, TickData, EntrySignal
from strategy.base import IEntryRule


class VwapReboundEntry(IEntryRule):
    """
    Evaluates VWAP and Day Open reclaim entry in bear market dip rebounds.
    Strictly enforces Bar 0 Causal Integrity (Lookahead 0%), statutory KRX tick conformance,
    +0.5% collar cap slippage defense, and single-fire daily execution.
    """

    name: str = "vwap_rebound_entry"

    def __init__(
        self,
        collar_cap_pct: float = 0.005,          # +0.5% anti-chase collar cap
        target_notional: float = 10_000_000.0,  # 10M KRW fixed single-position capital
        start_time: time = time(9, 0),          # Default opens from 09:00:00 (scanner controls discovery window)
        end_time: time = time(15, 0),           # Entry allowed until afternoon cutoff
        **kwargs: Any,
    ):
        self.collar_cap_pct = collar_cap_pct
        self.target_notional = target_notional
        self.start_time = start_time
        self.end_time = end_time

        self._day_open_prices: Dict[str, float] = {}
        self._entered_symbols_today: Set[str] = set()

    def on_day_open(self, current_date: date) -> None:
        """Called at 09:00:00 to reset daily entry states."""
        self._day_open_prices.clear()
        self._entered_symbols_today.clear()

    def register_day_open(self, symbol: str, price: float) -> None:
        """Hook to register day open price from scanner or engine."""
        if price > 0 and symbol not in self._day_open_prices:
            self._day_open_prices[symbol] = price

    def rollback_entered_symbol(self, symbol: str) -> None:
        """Rolls back entered symbol if order was rejected or cancelled."""
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

        # Zero volume or unformed bar guard
        if bar.volume <= 0:
            return None

        # Static / Dynamic VI Defense: Reject entry during call auction
        if context_state and (context_state.get("is_vi") or context_state.get("vi_cls_code") in ("Y", "1", "2")):
            return None

        # 1. Single-fire daily invariant
        if symbol in self._entered_symbols_today:
            return None

        eval_dt = tick.dt if tick else bar.dt
        t = eval_dt.time()

        # 2. Entry window check
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

        # 4. VWAP validation with physical candle range defense
        vwap = day_open
        if bar.volume > 0:
            raw_vwap = bar.turnover / bar.volume
            if math.isfinite(raw_vwap) and bar.low <= raw_vwap <= bar.high:
                vwap = raw_vwap

        # Bar 0 causal integrity: trigger price aligns to day open reclaim
        raw_trigger = day_open
        trigger_price = round_to_krx_tick(raw_trigger, side="BUY", round_mode="floor")

        # Statutory KRX tick alignment for collar cap (+0.5% floor)
        raw_collar = trigger_price * (1.0 + self.collar_cap_pct)
        collar_cap_price = max(
            trigger_price,
            round_to_krx_tick(raw_collar, side="BUY", round_mode="floor"),
        )

        current_high = tick.price if tick else bar.high
        curr_p = tick.price if tick else bar.close

        metrics = {
            "trigger_price": trigger_price,
            "collar_cap_price": collar_cap_price,
            "current_price": curr_p,
            "high": current_high,
            "day_open": day_open,
            "vwap": vwap,
            "target_notional": self.target_notional,
        }

        # Anti-chase Collar Cap Check 1: Opening price gapped over collar cap
        if bar.open > collar_cap_price:
            telemetry.record_strategy_eval("ENTRY", symbol, "REJECT", f"FAIL_COLLAR_CAP_OPEN ({bar.open} > {collar_cap_price})", metrics, dt=eval_dt)
            return None

        # Anti-chase Collar Cap Check 2: Realtime tick surged past collar cap
        if tick and tick.price > collar_cap_price:
            telemetry.record_strategy_eval("ENTRY", symbol, "REJECT", f"FAIL_COLLAR_CAP_TICK ({tick.price} > {collar_cap_price})", metrics, dt=eval_dt)
            return None

        # Reclaim verification: must reach Day Open and VWAP
        if current_high >= trigger_price and current_high >= vwap:
            self._entered_symbols_today.add(symbol)
            telemetry.record_strategy_eval("ENTRY", symbol, "PASS", "PASS_VWAP_REBOUND_ENTRY", metrics, dt=eval_dt)
            return EntrySignal(
                symbol=symbol,
                dt=eval_dt,
                trigger_price=trigger_price,
                collar_cap_price=collar_cap_price,
                target_notional=self.target_notional,
                reason="VWAP_REBOUND",
                metadata={
                    "day_open": day_open,
                    "vwap": vwap,
                    "collar_cap_pct": self.collar_cap_pct,
                },
            )

        telemetry.record_strategy_eval("ENTRY", symbol, "REJECT", f"FAIL_BELOW_RECLAIM ({current_high} < {trigger_price})", metrics, dt=eval_dt)
        return None
