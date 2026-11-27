"""
Deterministic Simulation Clock for Korea Market v4 Backtesting.
Implements `IClock` without wall-clock sleeps.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from v4.interfaces import IClock


class BacktestClock(IClock):
    """Virtual simulation clock advanced deterministically by `BacktestRealtimeFeed`."""

    def __init__(self, initial_dt: Optional[datetime] = None) -> None:
        self._now: datetime = initial_dt or datetime(2026, 1, 2, 9, 0, 0)

    def now(self) -> datetime:
        return self._now

    def sync_time(self, dt: datetime) -> None:
        self._now = dt

    async def sleep(self, seconds: float) -> None:
        return None
