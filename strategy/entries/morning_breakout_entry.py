"""
Morning Breakout with Collar-Cap Entry Rule.
Triggers an entry when price breaches the +4.5% breakout threshold from day open,
strictly capped at +0.5% collar cap to prevent chasing into illiquid spreads.
"""
from __future__ import annotations

from datetime import date, time
from typing import Optional, Dict, Any, Set
import numpy as np

from core.dynamic_features import DynamicFeatureProvider, get_default_dynamic_feature_provider, SymbolDynamicFeatures
from core.krx_ticks import round_to_krx_tick
from core.telemetry import telemetry
from engine.models import MinuteBar, TickData, EntrySignal
from strategy.base import IEntryRule


class MorningBreakoutEntry(IEntryRule):
    """
    Evaluates +4.5% morning breakout from day open, capped at +0.5% max slippage collar.
    Enforces Bar 0 causal integrity (Lookahead 0%) and KRX statutory tick conformance.
    """

    name: str = "morning_breakout_entry"

    def __init__(
        self,
        breakout_pct: float = 0.045,            # +4.5% 시가 대비 돌파선
        collar_cap_pct: float = 0.005,          # +0.5% 안티 체이스 호가 캡
        target_notional: float = 10_000_000.0,  # 단리 고정 1,000만 원 운용
        start_time: time = time(9, 1),
        end_time: time = time(9, 3),
        use_dynamic_breakout: bool = True,
        exhaustion_filter_ratio: float = 0.65,
        feature_provider: Optional[DynamicFeatureProvider] = None,
    ):
        self.breakout_pct = breakout_pct
        self.collar_cap_pct = collar_cap_pct
        self.target_notional = target_notional
        self.start_time = start_time
        self.end_time = end_time
        self.use_dynamic_breakout = use_dynamic_breakout
        self.exhaustion_filter_ratio = exhaustion_filter_ratio
        self.feature_provider = feature_provider

        self._day_open_prices: Dict[str, float] = {}
        self._entered_symbols_today: Set[str] = set()
        self._day_features: Dict[str, SymbolDynamicFeatures] = {}

    def on_day_open(self, current_date: date) -> None:
        self._day_open_prices.clear()
        self._entered_symbols_today.clear()
        if self.use_dynamic_breakout:
            provider = self.feature_provider or get_default_dynamic_feature_provider()
            self._day_features = provider.get_day_stock_features(current_date)
        else:
            self._day_features.clear()

    def register_day_open(self, symbol: str, price: float) -> None:
        """Explicitly registers day open price from external component (e.g. scanner)."""
        if price > 0 and symbol not in self._day_open_prices:
            self._day_open_prices[symbol] = price

    def rollback_entered_symbol(self, symbol: str) -> None:
        """Removes symbol from entered symbols if broker rejected or cancelled order."""
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

        # Single-fire daily rule
        if symbol in self._entered_symbols_today:
            return None

        t = tick.dt.time() if tick else bar.time
        if t < self.start_time or t > self.end_time:
            return None

        # Resolve day open: prefer context_state (from scanner), then cached, fallback to bar.open
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

        # Resolve dynamic breakout threshold and apply exhaustion filter
        eff_breakout = self.breakout_pct
        eff_atr = self.breakout_pct / 0.40  # default equivalent ATR (~11.25%)

        if self.use_dynamic_breakout and symbol in self._day_features:
            feat = self._day_features[symbol]
            if feat.prior_atr20_pct > 0.0:
                eff_atr = feat.prior_atr20_pct
                eff_breakout = float(np.clip(0.40 * eff_atr, 0.025, 0.055))
                # Exhaustion Filter: Disqualify if (breakout_pct_i / prior_atr20_pct_i) > 0.65
                if (eff_breakout / eff_atr) > self.exhaustion_filter_ratio:
                    telemetry.record_strategy_eval(
                        "ENTRY", symbol, "REJECT", "FAIL_EXHAUSTION_FILTER",
                        {"day_open": day_open, "eff_breakout": eff_breakout, "eff_atr": eff_atr},
                        dt=tick.dt if tick else bar.dt
                    )
                    return None

        # Statutory KRX tick alignment for breakout trigger
        raw_trigger = day_open * (1.0 + eff_breakout)
        trigger_price = round_to_krx_tick(raw_trigger, side="BUY", round_mode="ceil")

        # Statutory KRX tick alignment for collar cap (+0.5% floor)
        raw_collar = trigger_price * (1.0 + self.collar_cap_pct)
        collar_cap_price = max(
            trigger_price,
            round_to_krx_tick(raw_collar, side="BUY", round_mode="floor")
        )

        current_high = tick.price if tick else bar.high
        curr_p = tick.price if tick else bar.close
        eval_dt = tick.dt if tick else bar.dt
        metrics = {
            "trigger_price": trigger_price,
            "collar_cap_price": collar_cap_price,
            "current_price": curr_p,
            "high": current_high,
            "day_open": day_open,
            "breakout_pct": round(eff_breakout * 100, 2),
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

        # Breakout price trigger check (Bar 0 causal integrity: close is never examined)
        if current_high >= trigger_price:
            self._entered_symbols_today.add(symbol)
            telemetry.record_strategy_eval("ENTRY", symbol, "PASS", "PASS_BREAKOUT_ENTRY", metrics, dt=eval_dt)
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

        telemetry.record_strategy_eval("ENTRY", symbol, "REJECT", f"FAIL_BELOW_TRIGGER ({current_high} < {trigger_price})", metrics, dt=eval_dt)
        return None
