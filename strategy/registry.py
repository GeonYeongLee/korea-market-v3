"""
Strategy Component Registry & Factory.
Provides dynamic instantiation and configuration-based assembly of strategies.
"""
from __future__ import annotations

from typing import Dict, Type, Any, Optional
from strategy.base import IScanner, IEntryRule, IExitPolicy
from strategy.composite import CompositeStrategy
from strategy.scanners.morning_leader_scanner import MorningLeaderScanner
from strategy.scanners.anchor_scanner import AnchorScanner
from strategy.entries.morning_breakout_entry import MorningBreakoutEntry
from strategy.entries.anchor_breakout_entry import AnchorBreakoutEntry
from strategy.exits.morning_leader_exit import MorningLeaderExit
from strategy.exits.dynamic_leader_exit import DynamicLeaderExit
from strategy.exits.anchor_dynamic_exit import AnchorDynamicExit


SCANNERS: Dict[str, Type[IScanner]] = {
    "morning_leader": MorningLeaderScanner,
    "anchor": AnchorScanner,
}

ENTRIES: Dict[str, Type[IEntryRule]] = {
    "morning_breakout": MorningBreakoutEntry,
    "anchor_breakout": AnchorBreakoutEntry,
}

EXITS: Dict[str, Type[IExitPolicy]] = {
    "morning_leader": MorningLeaderExit,
    "dynamic_leader": DynamicLeaderExit,
    "anchor_dynamic": AnchorDynamicExit,
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

    s_kwargs = (scanner_kwargs or {}).copy()
    if "use_dynamic_turnover" not in s_kwargs and scanner_name == "morning_leader":
        s_kwargs["use_dynamic_turnover"] = turnover_flag

    e_kwargs = (entry_kwargs or {}).copy()
    if "use_dynamic_breakout" not in e_kwargs and entry_name == "morning_breakout":
        e_kwargs["use_dynamic_breakout"] = breakout_flag

    x_kwargs = (exit_kwargs or {}).copy()
    if "ambiguity_mode" not in x_kwargs:
        x_kwargs["ambiguity_mode"] = ambiguity_mode
    if "use_dynamic_targets" not in x_kwargs and exit_name == "morning_leader":
        x_kwargs["use_dynamic_targets"] = exit_flag

    scanner = SCANNERS[scanner_name](**s_kwargs)
    if "scanner" not in e_kwargs and hasattr(scanner, "get_anchor_info"):
        e_kwargs["scanner"] = scanner
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
        severe_gap_down_filter=severe_gap_down_filter,
        use_dual_index=use_dual_index,
        gap_filter_index=gap_filter_index,
        dynamic_max_gap=gap_flag,
        bear_market_defense=bear_flag,
        name=f"Composite_{scanner_name}_{entry_name}_{exit_name}",
    )
