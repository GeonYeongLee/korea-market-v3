"""
Unit and integration tests for Live-vs-Backtest Remediation Fixes:
1. AnchorMorningExit evaluation on pure TickData (bar=None) -> No AttributeError
2. AnchorBodyMicroScanner.feed_condition_search_symbol dynamic candidate injection
3. CompositeStrategy rejection handling (empty string order_id)
4. RealtimeAnchorScanner candidate extraction and alpha score calculation
"""
from __future__ import annotations

from datetime import datetime, time, date
import pytest

from engine.models import MinuteBar, Position, TickData, Order, OrderSide, OrderType
from strategy.scanners.anchor_body_micro_scanner import AnchorBodyMicroScanner
from strategy.entries.anchor_close_entry import AnchorCloseEntry
from strategy.exits.anchor_morning_exit import AnchorMorningExit
from strategy.composite import CompositeStrategy


def test_anchor_morning_exit_on_tick_without_bar():
    """Verifies that AnchorMorningExit handles on_tick (bar=None) without AttributeError."""
    exit_policy = AnchorMorningExit(exit_at_open=True, open_exit_time=time(9, 0))

    pos = Position(
        symbol="005930",
        quantity=100,
        avg_price=50000.0,
        current_price=50000.0,
        high_water_mark=50000.0,
        entry_dt=datetime(2025, 1, 13, 15, 20),
        phase=1,
    )

    # 1. NXT pre-market tick at 08:30 (bar=None)
    tick_nxt = TickData(
        symbol="005930",
        dt=datetime(2025, 1, 14, 8, 30),
        price=50500.0,
        volume=100,
        cum_turnover=50_000_000.0,
    )
    # Should not raise AttributeError!
    sig_nxt = exit_policy.evaluate_exit(pos, None, tick=tick_nxt)
    assert sig_nxt is None  # No exit triggered under threshold

    # 2. 09:00 regular open tick at 09:00:01 (bar=None)
    tick_open = TickData(
        symbol="005930",
        dt=datetime(2025, 1, 14, 9, 0, 1),
        price=51500.0,
        volume=5000,
        cum_turnover=2_500_000_000.0,
    )
    sig_open = exit_policy.evaluate_exit(pos, None, tick=tick_open)
    assert sig_open is not None
    assert sig_open.reason == "KRX_0900_OPEN_EXIT"
    assert sig_open.target_price == 51500.0


def test_anchor_scanner_dynamic_candidate_injection():
    """Verifies that AnchorBodyMicroScanner accepts live symbols fed via feed_condition_search_symbol."""
    scanner = AnchorBodyMicroScanner()
    scanner.on_day_open(date(2026, 11, 19))
    assert "005930" not in scanner._day_candidates

    scanner.feed_condition_search_symbol("005930")
    assert "005930" in scanner._day_candidates

    # Bar within scan window for injected candidate -> discovered
    bar = MinuteBar(
        "005930",
        datetime(2026, 11, 19, 15, 20),
        55000.0, 55500.0, 54800.0, 55200.0, 10000, 550_000_000.0, cum_turnover=55_000_000_000.0
    )
    cands = scanner.on_bar(date(2026, 11, 19), bar)
    assert cands == ["005930"]


def test_composite_strategy_empty_order_id_rejection():
    """Verifies that an empty string order_id from broker is correctly treated as failed order."""
    strat = CompositeStrategy(
        scanner=AnchorBodyMicroScanner(),
        entry=AnchorCloseEntry(),
        exit_policy=AnchorMorningExit(),
        max_daily_positions=2,
        dynamic_slots=False,
        total_capital=10_000_000.0,
    )

    class MockContext:
        def __init__(self):
            self.buy_called = False
        def get_position(self, sym): return None
        def get_balance(self): return None
        def buy(self, symbol, quantity, price, tag):
            self.buy_called = True
            return ""  # Live broker rejection returns empty string

    ctx = MockContext()
    bar = MinuteBar(
        "005930",
        datetime(2026, 11, 19, 15, 20),
        50000.0, 50500.0, 49800.0, 50200.0, 10000, 500_000_000.0, cum_turnover=50_000_000_000.0
    )
    strat.scanner._day_candidates = ["005930"]
    strat.on_bar(ctx, bar)

    assert ctx.buy_called is True
    # Crucial assertion: rejection must NOT increment daily trade count!
    assert strat._daily_trade_count == 0
