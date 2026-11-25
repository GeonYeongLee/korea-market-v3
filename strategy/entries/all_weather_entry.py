"""
All-Weather Unified Entry Rule.
Uses the empirically validated Regime-Free MorningBreakoutEntry across ALL market regimes:
  - Window: 09:01 ~ 09:15 (0% lookahead, dynamic ATR breakout threshold + +0.5% collar cap)
"""
from __future__ import annotations

import logging
from datetime import date, time
from typing import Optional, Dict, Any

from core.dynamic_features import DynamicFeatureProvider, get_default_dynamic_feature_provider
from engine.models import MinuteBar, TickData, EntrySignal
from strategy.base import IEntryRule
from strategy.entries.morning_breakout_entry import MorningBreakoutEntry

logger = logging.getLogger("korea_market.strategy.all_weather_entry")


class AllWeatherEntry(IEntryRule):
    """
    Unified Entry Rule delegating to a single regime-free MorningBreakoutEntry configuration.
    """

    name: str = "all_weather_entry"

    def __init__(
        self,
        bull_entry: Optional[IEntryRule] = None,
        bear_entry: Optional[IEntryRule] = None,
        feature_provider: Optional[DynamicFeatureProvider] = None,
        target_notional: float = 10_000_000.0,
        bull_kwargs: Optional[Dict[str, Any]] = None,
        bear_kwargs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        self.feature_provider = feature_provider
        self._target_notional = target_notional

        u_kwargs = (bull_kwargs or {}).copy()
        u_kwargs.setdefault("target_notional", target_notional)
        if "feature_provider" not in u_kwargs and feature_provider is not None:
            u_kwargs["feature_provider"] = feature_provider
        u_kwargs.setdefault("breakout_pct", 0.045)
        u_kwargs.setdefault("start_time", time(9, 1))
        u_kwargs.setdefault("end_time", time(9, 15))
        u_kwargs.setdefault("use_dynamic_breakout", True)
        self.bull_entry = bull_entry or MorningBreakoutEntry(**u_kwargs)

        if bear_entry is not None:
            self.bear_entry = bear_entry
        elif bear_kwargs:
            br_kwargs = bear_kwargs.copy()
            br_kwargs.setdefault("target_notional", target_notional)
            if "feature_provider" not in br_kwargs and feature_provider is not None:
                br_kwargs["feature_provider"] = feature_provider
            br_kwargs.setdefault("breakout_pct", 0.045)
            br_kwargs.setdefault("start_time", time(9, 1))
            br_kwargs.setdefault("end_time", time(9, 15))
            br_kwargs.setdefault("use_dynamic_breakout", True)
            self.bear_entry = MorningBreakoutEntry(**br_kwargs)
        else:
            self.bear_entry = self.bull_entry

        self.is_bear_market: bool = False
        self._current_date: Optional[date] = None

    @property
    def active_entry(self) -> IEntryRule:
        """Returns the currently active sub-entry rule based on regime."""
        return self.bear_entry if self.is_bear_market else self.bull_entry

    @property
    def target_notional(self) -> float:
        return getattr(self.active_entry, "target_notional", self._target_notional)

    @target_notional.setter
    def target_notional(self, value: float) -> None:
        self._target_notional = value
        if hasattr(self.bull_entry, "target_notional"):
            self.bull_entry.target_notional = value
        if self.bear_entry is not self.bull_entry and hasattr(self.bear_entry, "target_notional"):
            self.bear_entry.target_notional = value

    @property
    def start_time(self) -> time:
        return getattr(self.active_entry, "start_time", time(9, 1))

    @property
    def end_time(self) -> time:
        return getattr(self.active_entry, "end_time", time(9, 15))

    @property
    def collar_cap_pct(self) -> float:
        return getattr(self.active_entry, "collar_cap_pct", 0.005)

    @property
    def breakout_threshold_pct(self) -> float:
        return getattr(self.active_entry, "breakout_pct", 0.045) * 100.0

    def on_day_open(self, current_date: date) -> None:
        self._current_date = current_date
        provider = self.feature_provider or get_default_dynamic_feature_provider()
        idx_feat = provider.get_index_features(current_date)
        self.is_bear_market = bool(idx_feat.is_bear_market)

        self.bull_entry.on_day_open(current_date)
        if self.bear_entry is not self.bull_entry:
            self.bear_entry.on_day_open(current_date)

    def evaluate_entry(
        self,
        symbol: str,
        bar: Optional[MinuteBar] = None,
        tick: Optional[TickData] = None,
        context_state: Optional[Dict[str, Any]] = None,
    ) -> Optional[EntrySignal]:
        return self.active_entry.evaluate_entry(symbol, bar, tick=tick, context_state=context_state)

    def register_day_open(self, symbol: str, price: float) -> None:
        if hasattr(self.bull_entry, "register_day_open"):
            self.bull_entry.register_day_open(symbol, price)
        if self.bear_entry is not self.bull_entry and hasattr(self.bear_entry, "register_day_open"):
            self.bear_entry.register_day_open(symbol, price)

    def rollback_entered_symbol(self, symbol: str) -> None:
        if hasattr(self.bull_entry, "rollback_entered_symbol"):
            self.bull_entry.rollback_entered_symbol(symbol)
        if self.bear_entry is not self.bull_entry and hasattr(self.bear_entry, "rollback_entered_symbol"):
            self.bear_entry.rollback_entered_symbol(symbol)
