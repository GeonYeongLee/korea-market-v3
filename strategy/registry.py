"""
Strategy Component Registry & Factory.
=======================================
Provides dynamic instantiation and configuration-based assembly of strategies.
Decouples Scanner, Entry, and Exit modules into a modular Lego-block architecture.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Type

from strategy.base import IEntryRule, IExitPolicy, IScanner
from strategy.composite import CompositeStrategy
from strategy.entries.anchor_breakout_entry import AnchorBreakoutEntry
from strategy.entries.anchor_close_entry import AnchorCloseEntry
from strategy.entries.morning_breakout_entry import MorningBreakoutEntry
from strategy.exits.anchor_dynamic_exit import AnchorDynamicExit
from strategy.exits.anchor_morning_exit import AnchorConditionalMorningExit, AnchorMorningExit
from strategy.exits.dynamic_leader_exit import DynamicLeaderExit
from strategy.exits.morning_leader_exit import MorningLeaderExit
from strategy.scanners.anchor_body_micro_scanner import AnchorBodyMicroScanner, ChampionBScanner
from strategy.scanners.anchor_close_scanner import AnchorCloseScanner
from strategy.scanners.anchor_scanner import AnchorScanner
from strategy.scanners.crisis_rebound_scanner import CrisisReboundScanner
from strategy.scanners.morning_leader_scanner import MorningLeaderScanner

logger = logging.getLogger("korea_market.strategy.registry")

SCANNERS: Dict[str, Type[IScanner]] = {
    "morning_leader": MorningLeaderScanner,
    "anchor": AnchorScanner,
    "anchor_close": AnchorCloseScanner,
    "anchor_body_micro": AnchorBodyMicroScanner,
    "champion_b": ChampionBScanner,
    "crisis_rebound": CrisisReboundScanner,
}

ENTRIES: Dict[str, Type[IEntryRule]] = {
    "morning_breakout": MorningBreakoutEntry,
    "anchor_breakout": AnchorBreakoutEntry,
    "anchor_close": AnchorCloseEntry,
}

EXITS: Dict[str, Type[IExitPolicy]] = {
    "morning_leader": MorningLeaderExit,
    "dynamic_leader": DynamicLeaderExit,
    "anchor_dynamic": AnchorDynamicExit,
    "anchor_morning": AnchorMorningExit,
    "anchor_conditional_morning": AnchorConditionalMorningExit,
    "anchor_conditional_0800": AnchorConditionalMorningExit,
}


def build_strategy(
    scanner_name: str = "morning_leader",
    entry_name: str = "morning_breakout",
    exit_name: str = "morning_leader",
    scanner_kwargs: Optional[Dict[str, Any]] = None,
    entry_kwargs: Optional[Dict[str, Any]] = None,
    exit_kwargs: Optional[Dict[str, Any]] = None,
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
    single_slot_cap_pct: Optional[float] = None,
) -> CompositeStrategy:
    """
    Builds and returns an assembled CompositeStrategy instance from registry keys.

    Args:
        scanner_name: Key in SCANNERS registry dict.
        entry_name: Key in ENTRIES registry dict.
        exit_name: Key in EXITS registry dict.
        scanner_kwargs: Optional kwargs to pass to scanner constructor.
        entry_kwargs: Optional kwargs to pass to entry rule constructor.
        exit_kwargs: Optional kwargs to pass to exit policy constructor.
        max_daily_positions: Maximum daily concurrent positions.
        total_capital: Portfolio total capital.
        dynamic_slots: Whether to dynamically adjust slot count.
        base_slots: Base slot allocation under normal regime.
        expanded_slots: Expanded slot allocation under high-conviction regime.
        kospi_gap_threshold: Threshold to switch between base and expanded slots.
        max_market_gap: Upper market gap ceiling filter (None to disable).
        severe_gap_down_filter: Severe gap-down halt threshold.
        use_dual_index: Whether to use 4-quadrant dual-index regime.
        gap_filter_index: Index to evaluate for gap filtering ('max', 'kospi', 'kosdaq').
        ambiguity_mode: Resolution mode for dual SL/TP breach ('pessimistic' or 'optimistic').
        dynamic: Master toggle for dynamic parameter scaling.
        dynamic_turnover: Toggle for dynamic turnover scaling.
        dynamic_breakout: Toggle for dynamic breakout threshold scaling.
        dynamic_exit_targets: Toggle for dynamic exit target scaling.
        dynamic_max_gap: Toggle for dynamic ATR-based gap ceiling.
        bear_market_defense: Toggle for 20-day moving average bear market defense.
        single_slot_cap_pct: Optional single slot capital cap percentage.

    Returns:
        Assembled CompositeStrategy instance.
    """
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

    # Anchor Close Betting defaults
    if scanner_name in ("champion_b", "anchor_body_micro"):
        if bear_market_defense is None:
            bear_flag = False
        if dynamic_max_gap is None:
            gap_flag = False
        if max_market_gap == 0.75:
            max_market_gap = None

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
    if scanner_name in ("champion_b", "anchor_body_micro") and exit_name == "anchor_morning":
        if "exit_at_open" not in x_kwargs:
            x_kwargs["exit_at_open"] = True

    scanner = SCANNERS[scanner_name](**s_kwargs)
    if "scanner" not in e_kwargs and hasattr(scanner, "get_anchor_info"):
        e_kwargs["scanner"] = scanner
    entry = ENTRIES[entry_name](**e_kwargs)
    exit_policy = EXITS[exit_name](**x_kwargs)

    strat = CompositeStrategy(
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
    if single_slot_cap_pct is not None:
        strat.single_slot_cap_pct = single_slot_cap_pct
    return strat


def build_champion_b_strategy(
    total_capital: float = 10_000_000.0,
    cap_pct: float = 0.50,
    max_daily_positions: int = 2,
    ambiguity_mode: str = "pessimistic",
    exit_at_open: bool = True,
    enable_nxt_exit: bool = False,
    enable_crisis_rebound: bool = True,
    scanner_kwargs: Optional[Dict[str, Any]] = None,
    entry_kwargs: Optional[Dict[str, Any]] = None,
    exit_kwargs: Optional[Dict[str, Any]] = None,
) -> CompositeStrategy:
    """
    Builds and returns the production-ready Champion B (Body5.5-Micro) Composite Strategy.

    Architecture (Lego-block Decoupled):
      - Scanner: AnchorBodyMicroScanner (ChampionBScanner)
          * Filters confirmed solid-body Anchor Candles right before 15:20 close.
          * Real body return >= +5.5% ((close - open) / open * 100 >= 5.5).
          * Tiered Micro-Shadow:
              - TOP 1~10: upper shadow <= 10.0%, daily change +8.0% ~ +22.0%
              - TOP 11~20: upper shadow <= 8.0%, daily change +10.0% ~ +22.0%
          * Alpha 4-Factor Ranking Score (Turnover 25% + Upper Shadow 35% + 60d High 20% + Body 20%).
          * Discovers top 2 candidates per day at 15:15~15:25.
      - Entry: AnchorCloseEntry
          * 15:20 close betting execution with +0.5% anti-chase collar cap and KRX tick sizing.
      - Exit: AnchorMorningExit
          * 09:00 KRX regular market opening auction liquidation (0% slippage)
          * Optional 08:00 NXT early profit taking (+3.0%) if enabled.

    Empirical Performance (2020~2026, 7-year verified):
      - 622 trades (1.8 trades/week, 0 losing years across all 7 years)
      - 50% Cap: Net Profit +18,097,977 KRW (+181.0%), MDD -7.66%, Sharpe 1.36, Win Rate 49.7%, PF 1.62
      - 60% Cap: Net Profit +18,932,438 KRW (+189.3%), MDD -9.18%, Sharpe 1.29, Win Rate 49.7%, PF 1.62

    Args:
        total_capital: Total portfolio trading capital.
        cap_pct: Capital allocation percentage per slot (default: 0.50).
        max_daily_positions: Maximum daily concurrent positions (default: 2).
        ambiguity_mode: Dual breach resolution ('pessimistic' or 'optimistic').
        exit_at_open: Liquidate overnight position at 09:00 open auction.
        enable_nxt_exit: Enable conditional 08:00 NXT early profit taking.
        scanner_kwargs: Optional kwargs to override scanner settings.
        entry_kwargs: Optional kwargs to override entry rule settings.
        exit_kwargs: Optional kwargs to override exit policy settings.

    Returns:
        Assembled CompositeStrategy configured for Champion B.
    """
    s_kwargs = (scanner_kwargs or {}).copy()
    if "max_universe_size" not in s_kwargs:
        s_kwargs["max_universe_size"] = max_daily_positions
    if "enable_crisis_rebound" not in s_kwargs:
        s_kwargs["enable_crisis_rebound"] = enable_crisis_rebound
    scanner = AnchorBodyMicroScanner(**s_kwargs)

    effective_cap = total_capital * (cap_pct * max_daily_positions)
    slot_notional = effective_cap / max(1, max_daily_positions)

    e_kwargs = (entry_kwargs or {}).copy()
    if "collar_cap_pct" not in e_kwargs:
        e_kwargs["collar_cap_pct"] = 0.005
    if "target_notional" not in e_kwargs:
        e_kwargs["target_notional"] = slot_notional
    entry = AnchorCloseEntry(**e_kwargs)

    x_kwargs = (exit_kwargs or {}).copy()
    if "exit_at_open" not in x_kwargs:
        x_kwargs["exit_at_open"] = exit_at_open
    if "enable_nxt_exit" not in x_kwargs:
        x_kwargs["enable_nxt_exit"] = enable_nxt_exit
    if "ambiguity_mode" not in x_kwargs:
        x_kwargs["ambiguity_mode"] = ambiguity_mode
    exit_policy = AnchorMorningExit(**x_kwargs)

    strat = CompositeStrategy(
        scanner=scanner,
        entry=entry,
        exit_policy=exit_policy,
        max_daily_positions=max_daily_positions,
        total_capital=total_capital,
        dynamic_slots=False,
        bear_market_defense=False,
        dynamic_max_gap=False,
        max_market_gap=None,
        name=f"ChampionB_Body5.5_Micro_Cap{int(cap_pct * 100)}",
    )
    if cap_pct > (1.0 / max_daily_positions):
        strat.single_slot_cap_pct = cap_pct
        strat.current_slot_notional = total_capital * cap_pct
    return strat
