"""
All-Weather Integrated Scanner.
Encapsulates:
  - Bull / Normal Market: MorningLeaderScanner (09:01~09:03 high momentum breakout, gap 0~10%, MAE -1.5%)
  - Bear Market: MorningLeaderScanner (09:01~09:25 decoupled leader breakout, gap 0~6%, MAE -1.2%)
Dynamically delegates based on daily market regime (is_bear_market) determined at on_day_open.
"""
from __future__ import annotations

import logging
from datetime import date, time
from typing import Optional, List, Dict, Any

from core.dynamic_features import DynamicFeatureProvider, get_default_dynamic_feature_provider
from engine.models import MinuteBar
from strategy.base import IScanner
from strategy.scanners.morning_leader_scanner import MorningLeaderScanner

logger = logging.getLogger("korea_market.strategy.all_weather_scanner")


class AllWeatherScanner(IScanner):
    """
    Composite Scanner that dynamically delegates to regime-tuned MorningLeaderScanner
    configurations based on dual-index MA20 trend regime.
    """

    name: str = "all_weather_scanner"

    def __init__(
        self,
        bull_scanner: Optional[IScanner] = None,
        bear_scanner: Optional[IScanner] = None,
        feature_provider: Optional[DynamicFeatureProvider] = None,
        bull_kwargs: Optional[Dict[str, Any]] = None,
        bear_kwargs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        self.feature_provider = feature_provider

        # Bull scanner configuration
        b_kwargs = (bull_kwargs or {}).copy()
        if "feature_provider" not in b_kwargs and feature_provider is not None:
            b_kwargs["feature_provider"] = feature_provider
        self.bull_scanner = bull_scanner or MorningLeaderScanner(**b_kwargs)

        # Bear scanner configuration: Decoupled Leader Breakout (09:01~09:25, gap 0~6%, MAE -1.2%)
        br_kwargs = (bear_kwargs or {}).copy()
        if "feature_provider" not in br_kwargs and feature_provider is not None:
            br_kwargs["feature_provider"] = feature_provider
        br_kwargs.setdefault("min_gap_pct", 0.000)
        br_kwargs.setdefault("max_gap_pct", 0.060)
        br_kwargs.setdefault("max_mae_pct", -0.012)
        br_kwargs.setdefault("min_body_pct", 0.035)
        br_kwargs.setdefault("start_time", time(9, 1))
        br_kwargs.setdefault("end_time", time(9, 25))
        br_kwargs.setdefault("use_dynamic_turnover", True)
        self.bear_scanner = bear_scanner or MorningLeaderScanner(**br_kwargs)

        self.is_bear_market: bool = True  # Safe default matching mock audit date (2024-10-21)
        self._current_date: Optional[date] = None

    @property
    def active_scanner(self) -> IScanner:
        """Returns the currently active sub-scanner based on regime."""
        return self.bear_scanner if self.is_bear_market else self.bull_scanner

    @property
    def start_time(self) -> time:
        return getattr(self.active_scanner, "start_time", time(9, 1))

    @property
    def end_time(self) -> time:
        return getattr(self.active_scanner, "end_time", time(9, 25) if self.is_bear_market else time(9, 3))

    @property
    def min_cum_turnover(self) -> float:
        return getattr(self.active_scanner, "min_cum_turnover", 1_000_000_000.0)

    @property
    def max_mae_pct(self) -> float:
        return getattr(self.active_scanner, "max_mae_pct", -0.012 if self.is_bear_market else -0.015)

    @property
    def _day_prev_closes(self) -> Dict[str, float]:
        return getattr(self.active_scanner, "_day_prev_closes", {})

    @_day_prev_closes.setter
    def _day_prev_closes(self, val: Dict[str, float]) -> None:
        if hasattr(self.bull_scanner, "_day_prev_closes"):
            self.bull_scanner._day_prev_closes = val
        if hasattr(self.bear_scanner, "_day_prev_closes"):
            self.bear_scanner._day_prev_closes = val

    def on_day_open(self, current_date: date) -> None:
        self._current_date = current_date
        provider = self.feature_provider or get_default_dynamic_feature_provider()
        idx_feat = provider.get_index_features(current_date)
        self.is_bear_market = bool(idx_feat.is_bear_market)

        self.bull_scanner.on_day_open(current_date)
        # Share cached daily prev_close lookup to avoid reading daily parquet twice
        if (
            hasattr(self.bear_scanner, "_prev_close_lookup")
            and hasattr(self.bull_scanner, "_prev_close_lookup")
            and not self.bear_scanner._prev_close_lookup
            and self.bull_scanner._prev_close_lookup
        ):
            self.bear_scanner._prev_close_lookup = self.bull_scanner._prev_close_lookup
        self.bear_scanner.on_day_open(current_date)

    def filter_universe(self, current_date: date) -> Optional[List[str]]:
        return self.active_scanner.filter_universe(current_date)

    def on_bar(self, current_date: date, bar: MinuteBar) -> Optional[List[str]]:
        return self.active_scanner.on_bar(current_date, bar)

    def is_disqualified(self, symbol: str) -> bool:
        return getattr(self.active_scanner, "is_disqualified", lambda s: False)(symbol)

    def get_day_open(self, symbol: str) -> Optional[float]:
        return getattr(self.active_scanner, "get_day_open", lambda s: None)(symbol)

    def get_prev_close(self, symbol: str) -> Optional[float]:
        return getattr(self.active_scanner, "get_prev_close", lambda s: None)(symbol)

    def get_symbol_metadata(self, symbol: str) -> Dict[str, Any]:
        return getattr(self.active_scanner, "get_symbol_metadata", lambda s: {})(symbol)

    def is_vi(self, symbol: str) -> bool:
        return getattr(self.active_scanner, "is_vi", lambda s: False)(symbol)

    def get_vi_cls_code(self, symbol: str) -> Optional[str]:
        return getattr(self.active_scanner, "get_vi_cls_code", lambda s: None)(symbol)

    def feed_condition_search_symbol(self, symbol: str) -> None:
        if hasattr(self.active_scanner, "feed_condition_search_symbol"):
            self.active_scanner.feed_condition_search_symbol(symbol)

    def set_condition_universe(self, symbols: List[str]) -> None:
        if hasattr(self.active_scanner, "set_condition_universe"):
            self.active_scanner.set_condition_universe(symbols)
