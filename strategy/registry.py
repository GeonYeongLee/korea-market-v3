"""
Strategy Component Registry & Factory.
Provides dynamic instantiation and configuration-based assembly of strategies.
"""
from __future__ import annotations

from typing import Dict, Type, Any, Optional
from datetime import time
from strategy.base import IScanner, IEntryRule, IExitPolicy
from strategy.composite import CompositeStrategy
from strategy.scanners.morning_leader_scanner import MorningLeaderScanner
from strategy.scanners.open_window_scanner import OpenWindowScanner
from strategy.scanners.bear_dip_rebound_scanner import BearDipReboundScanner
from strategy.scanners.all_weather_scanner import AllWeatherScanner
from strategy.entries.morning_breakout_entry import MorningBreakoutEntry
from strategy.entries.open_window_entry import OpenWindowEntry
from strategy.entries.vwap_rebound_entry import VwapReboundEntry
from strategy.entries.all_weather_entry import AllWeatherEntry
from strategy.exits.morning_leader_exit import MorningLeaderExit
from strategy.exits.dynamic_leader_exit import DynamicLeaderExit
from strategy.exits.trailing_lock_exit import TrailingLockExit
from strategy.exits.eod_trend_exit import EodTrendExit
from strategy.exits.all_weather_exit import AllWeatherExit


SCANNERS: Dict[str, Type[IScanner]] = {
    "morning_leader": MorningLeaderScanner,
    "open_window": OpenWindowScanner,
    "bear_dip_rebound": BearDipReboundScanner,
    "all_weather": AllWeatherScanner,
}

ENTRIES: Dict[str, Type[IEntryRule]] = {
    "morning_breakout": MorningBreakoutEntry,
    "open_window": OpenWindowEntry,
    "vwap_rebound": VwapReboundEntry,
    "all_weather": AllWeatherEntry,
}

EXITS: Dict[str, Type[IExitPolicy]] = {
    "morning_leader": MorningLeaderExit,
    "dynamic_leader": DynamicLeaderExit,
    "trailing_lock": TrailingLockExit,
    "eod_trend": EodTrendExit,
    "all_weather": AllWeatherExit,
}


def build_strategy(
    scanner_name: str = "morning_leader",
    entry_name: str = "morning_breakout",
    exit_name: str = "morning_leader",
    scanner_kwargs: Dict[str, Any] = None,
    entry_kwargs: Dict[str, Any] = None,
    exit_kwargs: Dict[str, Any] = None,
    max_daily_positions: int = 1,
    total_capital: float = 10_000_000.0,
    dynamic_slots: bool = True,
    base_slots: int = 2,
    expanded_slots: int = 5,
    kospi_gap_threshold: float = 0.0,
    max_market_gap: Optional[float] = 0.75,
    severe_gap_down_filter: Optional[float] = None,
    use_dual_index: bool = True,
    gap_filter_index: str = "max",
    ambiguity_mode: str = "pessimistic",
    dynamic: bool = False,
    dynamic_turnover: Optional[bool] = None,
    dynamic_breakout: Optional[bool] = None,
    dynamic_exit_targets: Optional[bool] = None,
    dynamic_max_gap: Optional[bool] = None,
    bear_market_defense: Optional[bool] = None,
    regime_mode: Optional[str] = "all",
) -> CompositeStrategy:
    """Builds and returns an assembled CompositeStrategy instance from registry keys."""
    if scanner_name not in SCANNERS:
        raise KeyError(f"Unknown scanner: {scanner_name}. Available: {list(SCANNERS.keys())}")
    if entry_name not in ENTRIES:
        raise KeyError(f"Unknown entry: {entry_name}. Available: {list(ENTRIES.keys())}")
    if exit_name not in EXITS:
        raise KeyError(f"Unknown exit: {exit_name}. Available: {list(EXITS.keys())}")

    turnover_flag = dynamic if dynamic_turnover is None else dynamic_turnover
    breakout_flag = dynamic if dynamic_breakout is None else dynamic_breakout
    exit_flag = dynamic if dynamic_exit_targets is None else dynamic_exit_targets
    gap_flag = dynamic if dynamic_max_gap is None else dynamic_max_gap
    bear_flag = dynamic if bear_market_defense is None else bear_market_defense
    severe_flag = -2.0 if (dynamic and severe_gap_down_filter is None) else severe_gap_down_filter

    # Dedicated Bear Market Dip Rebound Configuration
    if scanner_name == "bear_dip_rebound":
        if bear_market_defense is None:
            bear_flag = False
        severe_flag = None
        gap_flag = False
        dynamic_slots = False
        max_daily_positions = 1
        base_slots = 1
        expanded_slots = 1

    # All-Weather / Unified Regime-Free Strategy Configuration
    is_unified_mode = regime_mode == "unified" or scanner_name == "all_weather" or entry_name == "all_weather" or exit_name == "all_weather"
    if scanner_name == "all_weather" or entry_name == "all_weather" or exit_name == "all_weather":
        if regime_mode in ("all", None):
            regime_mode = "all_weather"
    if is_unified_mode:
        bear_flag = False
        gap_flag = False
        severe_flag = None

    s_kwargs = (scanner_kwargs or {}).copy()
    if scanner_name == "bear_dip_rebound":
        # Neutralize CLI defaults designed for morning breakout (0.00 to 0.10)
        if s_kwargs.get("min_gap_pct") == 0.00:
            s_kwargs["min_gap_pct"] = -0.030
        if s_kwargs.get("max_gap_pct") == 0.10:
            s_kwargs["max_gap_pct"] = 0.010
    if is_unified_mode:
        # Apply validated Unified Regime-Free presets over CLI baseline defaults
        s_kwargs.setdefault("min_gap_pct", 0.000)
        if s_kwargs.get("max_gap_pct") in (None, 0.10):
            s_kwargs["max_gap_pct"] = 0.060
        if s_kwargs.get("max_mae_pct") in (None, -0.015):
            s_kwargs["max_mae_pct"] = -0.012
        s_kwargs.setdefault("min_body_pct", 0.035)
        s_kwargs.setdefault("start_time", time(9, 1))
        s_kwargs.setdefault("end_time", time(9, 15))
    if scanner_name == "all_weather":
        unified_s = s_kwargs.copy()
        if "use_dynamic_turnover" not in unified_s:
            unified_s["use_dynamic_turnover"] = turnover_flag
        s_kwargs["bull_kwargs"] = unified_s
    if "use_dynamic_turnover" not in s_kwargs and scanner_name == "morning_leader":
        s_kwargs["use_dynamic_turnover"] = turnover_flag

    e_kwargs = (entry_kwargs or {}).copy()
    if is_unified_mode:
        e_kwargs.setdefault("breakout_pct", 0.045)
        e_kwargs.setdefault("start_time", time(9, 1))
        e_kwargs.setdefault("end_time", time(9, 15))
    if entry_name == "all_weather":
        unified_e = e_kwargs.copy()
        if "use_dynamic_breakout" not in unified_e:
            unified_e["use_dynamic_breakout"] = breakout_flag
        e_kwargs["bull_kwargs"] = unified_e
    if "use_dynamic_breakout" not in e_kwargs and entry_name == "morning_breakout":
        e_kwargs["use_dynamic_breakout"] = breakout_flag

    x_kwargs = (exit_kwargs or {}).copy()
    if "ambiguity_mode" not in x_kwargs:
        x_kwargs["ambiguity_mode"] = ambiguity_mode
    if exit_name == "all_weather":
        unified_x = x_kwargs.copy()
        if "use_dynamic_targets" not in unified_x:
            unified_x["use_dynamic_targets"] = exit_flag
        x_kwargs["bull_kwargs"] = unified_x
    if "use_dynamic_targets" not in x_kwargs and exit_name == "morning_leader":
        x_kwargs["use_dynamic_targets"] = exit_flag
    if "use_dynamic_targets" not in x_kwargs and exit_name == "morning_leader":
        x_kwargs["use_dynamic_targets"] = exit_flag

    scanner = SCANNERS[scanner_name](**s_kwargs)
    entry = ENTRIES[entry_name](**e_kwargs)
    exit_policy = EXITS[exit_name](**x_kwargs)

    return CompositeStrategy(
        scanner=scanner,
        entry=entry,
        exit_policy=exit_policy,
        max_daily_positions=max_daily_positions,
        total_capital=total_capital,
        dynamic_slots=dynamic_slots,
        base_slots=base_slots,
        expanded_slots=expanded_slots,
        kospi_gap_threshold=kospi_gap_threshold,
        max_market_gap=max_market_gap,
        severe_gap_down_filter=severe_flag,
        use_dual_index=use_dual_index,
        gap_filter_index=gap_filter_index,
        dynamic_max_gap=gap_flag,
        bear_market_defense=bear_flag,
        regime_mode=regime_mode,
        name=f"Composite_{scanner_name}_{entry_name}_{exit_name}",
    )
