"""
Clock abstraction for trading engines.
SimulatedClock advances virtual time during backtests.
RealtimeClock references actual system clock during live trading with background scheduling.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time as pytime
from abc import ABC, abstractmethod
from datetime import datetime, date, time
from typing import Callable, List, Union, Optional, Dict, Any

logger = logging.getLogger("korea_market.clock")


def parse_time(t: Union[time, str]) -> time:
    """Parses time object or 'HH:MM' / 'HH:MM:SS' string into datetime.time."""
    if isinstance(t, time):
        return t
    parts = list(map(int, t.strip().split(":")))
    if len(parts) == 2:
        return time(parts[0], parts[1], 0)
    elif len(parts) == 3:
        return time(parts[0], parts[1], parts[2])
    raise ValueError(f"Invalid time format: {t}")


class IClock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        """Returns the current simulated or real time."""
        pass

    @abstractmethod
    def schedule(
        self,
        target_time: Union[time, str],
        callback: Callable[[datetime], None],
        name: str = "",
    ) -> None:
        """Schedules a callback when clock reaches target_time."""
        pass

    def check_and_dispatch(self) -> None:
        """Dispatches any overdue scheduled callbacks."""
        pass


class SimulatedClock(IClock):
    """Virtual clock driven by incoming bar or tick events."""

    def __init__(self, initial_dt: Optional[datetime] = None):
        self._current_dt: datetime = initial_dt or datetime(2024, 1, 1, 9, 0, 0)
        self._schedules: List[Dict[str, Any]] = []

    def now(self) -> datetime:
        return self._current_dt

    def schedule(
        self,
        target_time: Union[time, str],
        callback: Callable[[datetime], None],
        name: str = "",
    ) -> None:
        parsed_t = parse_time(target_time)
        self._schedules.append({
            "target_time": parsed_t,
            "callback": callback,
            "name": name or f"schedule_{parsed_t.strftime('%H%M%S')}",
            "last_triggered_date": None,
        })
        self._schedules.sort(key=lambda item: item["target_time"])

    def set_time(self, dt: datetime) -> None:
        self._current_dt = dt
        self.check_and_dispatch()

    def check_and_dispatch(self) -> None:
        cur_date = self._current_dt.date()
        cur_time = self._current_dt.time()
        for s in self._schedules:
            target_t = s["target_time"]
            if cur_time >= target_t and s["last_triggered_date"] != cur_date:
                s["last_triggered_date"] = cur_date
                try:
                    s["callback"](self._current_dt)
                except Exception as e:
                    logger.error(f"Error in scheduled event '{s['name']}': {e}", exc_info=True)


class RealtimeClock(IClock):
    """
    OS wall-clock for live and mock trading.
    Supports background monitoring thread and scheduled callback dispatching.
    """

    def __init__(self, auto_start: bool = False, interval_seconds: float = 0.5):
        self._schedules: List[Dict[str, Any]] = []
        self._running: bool = False
        self._interval_seconds = interval_seconds
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        if auto_start:
            self.start_background(interval_seconds)

    def now(self) -> datetime:
        return datetime.now()

    def schedule(
        self,
        target_time: Union[time, str],
        callback: Callable[[datetime], None],
        name: str = "",
    ) -> None:
        parsed_t = parse_time(target_time)
        with self._lock:
            self._schedules.append({
                "target_time": parsed_t,
                "callback": callback,
                "name": name or f"schedule_{parsed_t.strftime('%H%M%S')}",
                "last_triggered_date": None,
            })
            self._schedules.sort(key=lambda item: item["target_time"])

    def check_and_dispatch(self, now_dt: Optional[datetime] = None) -> None:
        if now_dt is None:
            now_dt = datetime.now()
        cur_date = now_dt.date()
        cur_time = now_dt.time()

        with self._lock:
            for s in self._schedules:
                target_t = s["target_time"]
                if cur_time >= target_t and s["last_triggered_date"] != cur_date:
                    s["last_triggered_date"] = cur_date
                    try:
                        s["callback"](now_dt)
                    except Exception as e:
                        logger.error(f"Error in realtime scheduled event '{s['name']}': {e}", exc_info=True)

    def start_background(self, interval_seconds: Optional[float] = None) -> None:
        """Starts a background daemon thread to monitor and dispatch scheduled events."""
        with self._lock:
            if self._running:
                return
            self._running = True
            interval = interval_seconds if interval_seconds is not None else self._interval_seconds

            def _loop():
                while self._running:
                    self.check_and_dispatch()
                    pytime.sleep(interval)

            self._thread = threading.Thread(target=_loop, daemon=True, name="RealtimeClockThread")
            self._thread.start()

    async def start(self, interval_seconds: Optional[float] = None) -> None:
        """Starts an asyncio background monitoring loop."""
        self._running = True
        interval = interval_seconds if interval_seconds is not None else self._interval_seconds
        while self._running:
            self.check_and_dispatch()
            await asyncio.sleep(interval)

    def stop(self) -> None:
        with self._lock:
            self._running = False
            if self._thread and self._thread.is_alive() and threading.current_thread() != self._thread:
                self._thread.join(timeout=1.0)
                self._thread = None
