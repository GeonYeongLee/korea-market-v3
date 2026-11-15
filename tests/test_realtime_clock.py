"""
Unit tests for RealtimeClock and SimulatedClock scheduling.
"""
from datetime import datetime, time, timedelta
import time as pytime
import pytest

from engine.clock import RealtimeClock, SimulatedClock, parse_time


def test_parse_time():
    assert parse_time("09:20") == time(9, 20, 0)
    assert parse_time("15:20:00") == time(15, 20, 0)
    assert parse_time(time(9, 0)) == time(9, 0, 0)
    with pytest.raises(ValueError):
        parse_time("invalid_time")


def test_realtime_clock_schedule_and_dispatch():
    clock = RealtimeClock()
    fired = []

    # Schedule a time that is already passed today (e.g. 00:00:00)
    clock.schedule("00:00:00", lambda dt: fired.append(dt), name="midnight_event")
    clock.check_and_dispatch()

    assert len(fired) == 1
    assert isinstance(fired[0], datetime)

    # Calling check_and_dispatch again on same date should NOT re-fire
    clock.check_and_dispatch()
    assert len(fired) == 1


def test_realtime_clock_future_schedule_not_fired():
    clock = RealtimeClock()
    fired = []

    # Schedule a time in the future (23:59:59)
    clock.schedule("23:59:59", lambda dt: fired.append(dt))
    clock.check_and_dispatch()

    assert len(fired) == 0


def test_realtime_clock_background_monitoring():
    clock = RealtimeClock()
    fired = []

    clock.schedule("00:00:00", lambda dt: fired.append(dt))
    clock.start_background(interval_seconds=0.05)
    pytime.sleep(0.15)
    clock.stop()

    assert len(fired) == 1
    assert clock._running is False


def test_simulated_clock_schedule_and_day_reset():
    clock = SimulatedClock(datetime(2026, 1, 2, 9, 0, 0))
    fired = []

    clock.schedule("09:20", lambda dt: fired.append(dt))

    # Advance time before target
    clock.set_time(datetime(2026, 1, 2, 9, 10, 0))
    assert len(fired) == 0

    # Advance time past target
    clock.set_time(datetime(2026, 1, 2, 9, 20, 0))
    assert len(fired) == 1

    # Same day advance -> no duplicate
    clock.set_time(datetime(2026, 1, 2, 9, 25, 0))
    assert len(fired) == 1

    # Next day rollover -> fires again at 09:20
    clock.set_time(datetime(2026, 1, 3, 9, 20, 0))
    assert len(fired) == 2
