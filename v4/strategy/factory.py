"""
Strategy Factory for Korea Market v4.
Assembles `CompositeStrategyV4` from CLI/config arguments and execution mode.
"""
from __future__ import annotations

import argparse

from v4.models import ExecutionMode
from v4.strategy.composite import CompositeStrategyV4
from v4.strategy.entry import AllWeatherEntryV4
from v4.strategy.exit import AllWeatherExitV4
from v4.strategy.scanner import AllWeatherScannerV4


def build_v4_strategy(args: argparse.Namespace, execution_mode: ExecutionMode) -> CompositeStrategyV4:
    """Assembles `CompositeStrategyV4` from CLI arguments and ambiguity mode."""
    scanner_name = getattr(args, "scanner", "all_weather")
    entry_name = getattr(args, "entry", "all_weather")
    exit_name = getattr(args, "exit", "all_weather")
    if (scanner_name, entry_name, exit_name) != ("all_weather", "all_weather", "all_weather"):
        raise ValueError(
            f"v4 unified runner supports ('all_weather', 'all_weather', 'all_weather'); "
            f"got ({scanner_name!r}, {entry_name!r}, {exit_name!r})"
        )

    strict_parity = bool(getattr(args, "strict_v3_parity", False))
    shift_open = not strict_parity
    enable_bar0_bar_sl = not strict_parity
    dynamic = bool(getattr(args, "dynamic", True))

    slots = getattr(args, "slots", None)
    dynamic_slots = slots is None
    base_slots = int(slots) if slots is not None else 2

    raw_min_gap = getattr(args, "min_gap", None)
    raw_max_gap = getattr(args, "max_gap", None)
    raw_max_mae = getattr(args, "max_mae", None)
    min_gap = 0.000 if raw_min_gap is None else float(raw_min_gap)
    max_gap = 0.060 if raw_max_gap in (None, 0.10) else float(raw_max_gap)
    max_mae = -0.012 if raw_max_mae in (None, -0.015) else float(raw_max_mae)

    scanner = AllWeatherScannerV4(
        min_gap_pct=min_gap,
        max_gap_pct=max_gap,
        max_mae_pct=max_mae,
        use_dynamic_turnover=dynamic,
        shift_to_market_open=shift_open,
        execution_mode=execution_mode,
        optimistic_prior_mae=not strict_parity,
    )
    entry = AllWeatherEntryV4(
        use_dynamic_breakout=dynamic,
        shift_to_market_open=shift_open,
    )
    exit_policy = AllWeatherExitV4(
        execution_mode=execution_mode,
        use_dynamic_targets=dynamic,
        enable_bar0_bar_sl=enable_bar0_bar_sl,
    )

    regime_mode = getattr(args, "regime_mode", "all_weather")
    if regime_mode in ("all", None):
        regime_mode = "all_weather"

    return CompositeStrategyV4(
        scanner=scanner,
        entry=entry,
        exit_policy=exit_policy,
        total_capital=float(getattr(args, "capital", 10_000_000.0)),
        max_daily_positions=base_slots,
        base_slots=base_slots,
        dynamic_slots=dynamic_slots,
        regime_mode=regime_mode,
        execution_mode=execution_mode,
        name="Composite_all_weather_v4",
    )
