"""
All-Weather Integrated Exit Policy.
Encapsulates:
  - Bull / Normal Market: MorningLeaderExit (Quad-layer exit: emergency SL, body center SL, step-up TP, 15:21 timeout)
  - Bear Market: TrailingLockExit (-1.5% hard SL, +2.5% TP1 -> +1.0% lock & 1.5% trailing, 15:19 timeout)
Dynamically delegates based on daily market regime (is_bear_market) determined at on_day_open.
"""
from __future__ import annotations

import logging
from datetime import date, time
from typing import Optional, Dict, Any

from core.dynamic_features import DynamicFeatureProvider, get_default_dynamic_feature_provider
from engine.models import MinuteBar, TickData, Position, ExitSignal
from strategy.base import IExitPolicy
from strategy.exits.morning_leader_exit import MorningLeaderExit
from strategy.exits.trailing_lock_exit import TrailingLockExit

logger = logging.getLogger("korea_market.strategy.all_weather_exit")


class AllWeatherExit(IExitPolicy):
    """
    Composite Exit Policy that dynamically delegates to MorningLeaderExit
    or TrailingLockExit based on dual-index MA20 trend regime.
    """

    name: str = "all_weather_exit"

    def __init__(
        self,
        bull_exit: Optional[IExitPolicy] = None,
        bear_exit: Optional[IExitPolicy] = None,
        feature_provider: Optional[DynamicFeatureProvider] = None,
        ambiguity_mode: str = "pessimistic",
        bull_kwargs: Optional[Dict[str, Any]] = None,
        bear_kwargs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        self.feature_provider = feature_provider
        self._ambiguity_mode = ambiguity_mode

        # Bull exit configuration
        b_kwargs = (bull_kwargs or {}).copy()
        b_kwargs.setdefault("ambiguity_mode", ambiguity_mode)
        self.bull_exit = bull_exit or MorningLeaderExit(**b_kwargs)

        # Bear exit configuration: TrailingLockExit (-1.5% SL, +2.5% TP1 -> +1.0% lock, 1.5% trail, 15:19 EOD)
        br_kwargs = (bear_kwargs or {}).copy()
        br_kwargs.setdefault("hard_sl_pct", -0.015)
        br_kwargs.setdefault("tp1_pct", 0.025)
        br_kwargs.setdefault("lock_profit_pct", 0.010)
        br_kwargs.setdefault("trailing_offset_pct", 0.015)
        br_kwargs.setdefault("hard_timeout", time(15, 19))
        br_kwargs.setdefault("ambiguity_mode", ambiguity_mode)
        self.bear_exit = bear_exit or TrailingLockExit(**br_kwargs)

        self.is_bear_market: bool = True  # Safe default matching mock audit date (2024-10-21)
        self._current_date: Optional[date] = None

    @property
    def active_exit(self) -> IExitPolicy:
        """Returns the currently active sub-exit policy based on regime."""
        return self.bear_exit if self.is_bear_market else self.bull_exit

    @property
    def emergency_sl_pct(self) -> float:
        return getattr(self.active_exit, "emergency_sl_pct", getattr(self.active_exit, "hard_sl_pct", -0.015))

    @property
    def tp_target_pct(self) -> float:
        return getattr(self.active_exit, "tp_target_pct", getattr(self.active_exit, "tp1_pct", 0.025))

    @property
    def hard_timeout(self) -> time:
        return getattr(self.active_exit, "hard_timeout", time(15, 21))

    @property
    def ambiguity_mode(self) -> str:
        return getattr(self.active_exit, "ambiguity_mode", self._ambiguity_mode)

    @ambiguity_mode.setter
    def ambiguity_mode(self, value: str) -> None:
        self._ambiguity_mode = value
        if hasattr(self.bull_exit, "ambiguity_mode"):
            self.bull_exit.ambiguity_mode = value
        if hasattr(self.bear_exit, "ambiguity_mode"):
            self.bear_exit.ambiguity_mode = value

    def on_day_open(self, current_date: date) -> None:
        self._current_date = current_date
        provider = self.feature_provider or get_default_dynamic_feature_provider()
        idx_feat = provider.get_index_features(current_date)
        self.is_bear_market = bool(idx_feat.is_bear_market)

        self.bull_exit.on_day_open(current_date)
        self.bear_exit.on_day_open(current_date)

    def on_position_opened(self, position: Position) -> None:
        if position.entry_dt is not None:
            provider = self.feature_provider or get_default_dynamic_feature_provider()
            d_val = position.entry_dt.date() if hasattr(position.entry_dt, "date") else position.entry_dt
            self.is_bear_market = provider.get_index_features(d_val).is_bear_market
        self.active_exit.on_position_opened(position)

    def evaluate_exit(
        self,
        position: Position,
        bar: Optional[MinuteBar] = None,
        tick: Optional[TickData] = None,
    ) -> Optional[ExitSignal]:
        return self.active_exit.evaluate_exit(position, bar=bar, tick=tick)
