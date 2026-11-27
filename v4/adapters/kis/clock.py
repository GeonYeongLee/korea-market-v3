"""
Live KST Wall-Clock Adapter for Korea Market v4.
Implements `IClock` using Asia/Seoul (UTC+9) naive datetimes and `asyncio.sleep`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from v4.interfaces import IClock

_KST = timezone(timedelta(hours=9))


class KisLiveClock(IClock):
    """Wall-clock KST implementation of `IClock` for REAL-live trading."""

    def now(self) -> datetime:
        """Returns current naive KST datetime."""
        return datetime.now(_KST).replace(tzinfo=None)

    def sync_time(self, dt: datetime) -> None:
        """No-op in live mode (wall clock is authoritative)."""
        return None

    async def sleep(self, seconds: float) -> None:
        """Awaits wall-clock duration asynchronously."""
        if seconds > 0.0:
            await asyncio.sleep(seconds)
