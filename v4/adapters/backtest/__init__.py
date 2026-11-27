"""
Backtest Port Adapters for Korea Market v4.
Exports virtual clock, parquet loader, regime/features, condition emulator,
realtime feed emulator, and deterministic backtest broker.
"""
from __future__ import annotations

from v4.adapters.backtest.broker import BacktestBrokerAdapter
from v4.adapters.backtest.clock import BacktestClock
from v4.adapters.backtest.condition_emulator import BacktestConditionSearchAdapter
from v4.adapters.backtest.feed import BacktestRealtimeFeed
from v4.adapters.backtest.parquet_loader import (
    CurrentDayStore,
    DayBarIndex,
    detect_session_times,
    list_trading_dates,
    load_day_bar_index,
    parse_date_arg,
    resolve_data_dir,
)
from v4.adapters.backtest.regime_data import ParquetRegimeDataAdapter, load_nxt_symbols

__all__ = [
    "BacktestBrokerAdapter",
    "BacktestClock",
    "BacktestConditionSearchAdapter",
    "BacktestRealtimeFeed",
    "CurrentDayStore",
    "DayBarIndex",
    "ParquetRegimeDataAdapter",
    "detect_session_times",
    "list_trading_dates",
    "load_day_bar_index",
    "load_nxt_symbols",
    "parse_date_arg",
    "resolve_data_dir",
]
