"""
All-Weather Integrated Entry Rule.
Encapsulates:
  - Bull / Normal Market: MorningBreakoutEntry (+4.5% morning breakout from day open, capped at +0.5%)
  - Bear Market: VwapReboundEntry (reclaim Day Open & VWAP with +0.5% collar cap)
Dynamically delegates based on daily market regime (is_bear_market) determined at on_day_open.
"""
from __future__ import annotations

import logging
from datetime import date, time
from typing import Optional, Dict, Any

from core.dynamic_features import DynamicFeatureProvider, get_default_dynamic_feature_provider
from engine.models import MinuteBar, TickData, EntrySignal
from strategy.base import IEntryRule
from strategy.entries.morning_breakout_entry import MorningBreakoutEntry
from strategy.entries.vwap_rebound_entry import VwapReboundEntry

logger = logging.getLogger("korea_market.strategy.all_weather_entry")


class AllWeatherEntry(IEntryRule):
    """
    Composite Entry Rule that dynamically delegates to MorningBreakoutEntry
    or VwapReboundEntry based on index trend regime.
    """

    name: str = "all_weather_entry"

    def __init__(
        self,
        bull_entry: Optional[MorningBreakoutEntry] = None,
        bear_entry: Optional[VwapReboundEntry] = None,
        feature_provider: Optional[DynamicFeatureProvider] = None,
        target_notional: float = 10_000_000.0,
        bull_kwargs: Optional[Dict[str, Any]] = None,
        bear_kwargs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        self.feature_provider = feature_provider
        self._target_notional = target_notional

        # Bull entry configuration
        b_kwargs = (bull_kwargs or {}).copy()
        b_kwargs.setdefault("target_notional", target_notional)
        if "feature_provider" not in b_kwargs and feature_provider is not None:
            b_kwargs["feature_provider"] = feature_provider
        self.bull_entry = bull_entry or MorningBreakoutEntry(**b_kwargs)

        # Bear entry configuration
        br_kwargs = (bear_kwargs or {}).copy()
        br_kwargs.setdefault("target_notional", target_notional)
        br_kwargs.setdefault("start_time", time(9, 0))
        br_kwargs.setdefault("end_time", time(9, 10))
        self.bear_entry = bear_entry or VwapReboundEntry(**br_kwargs)

        self.is_bear_market: bool = True  # Safe default matching mock audit date (2024-10-21)
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
        if hasattr(self.bear_entry, "target_notional"):
            self.bear_entry.target_notional = value

    @property
    def start_time(self) -> time:
        return getattr(self.active_entry, "start_time", time(9, 0))

    @property
    def end_time(self) -> time:
        return getattr(self.active_entry, "end_time", time(9, 10))

    @property
    def collar_cap_pct(self) -> float:
        return getattr(self.active_entry, "collar_cap_pct", 0.005)

    @property
    def breakout_threshold_pct(self) -> float:
        if self.is_bear_market:
            return 1.2
        return getattr(self.bull_entry, "breakout_threshold_pct", 4.5)

    def on_day_open(self, current_date: date) -> None:
        self._current_date = current_date
        provider = self.feature_provider or get_default_dynamic_feature_provider()
        idx_feat = provider.get_index_features(current_date)
        self.is_bear_market = bool(idx_feat.is_bear_market)

        self.bull_entry.on_day_open(current_date)
        self.bear_entry.on_day_open(current_date)

    def evaluate_entry(
        self,
        symbol: str,
        bar: MinuteBar,
        tick: Optional[TickData] = None,
        context_state: Optional[Dict[str, Any]] = None,
    ) -> Optional[EntrySignal]:
        return self.active_entry.evaluate_entry(symbol, bar, tick=tick, context_state=context_state)

    def register_day_open(self, symbol: str, price: float) -> None:
        if hasattr(self.bull_entry, "register_day_open"):
            self.bull_entry.register_day_open(symbol, price)
        if hasattr(self.bear_entry, "register_day_open"):
            self.bear_entry.register_day_open(symbol, price)

    def rollback_entered_symbol(self, symbol: str) -> None:
        if hasattr(self.bull_entry, "rollback_entered_symbol"):
            self.bull_entry.rollback_entered_symbol(symbol)
        if hasattr(self.bear_entry, "rollback_entered_symbol"):
            self.bear_entry.rollback_entered_symbol(symbol)
