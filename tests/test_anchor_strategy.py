"""
Unit and integration tests for the Anchor Candle Breakout Strategy modules:
- AnchorScanner
- AnchorBreakoutEntry
- AnchorDynamicExit (True Smart Step-Up)
"""
from __future__ import annotations

from datetime import datetime, time, date
import pytest

from engine.models import MinuteBar, Position, OrderType
from strategy.scanners.anchor_scanner import AnchorScanner
from strategy.entries.anchor_breakout_entry import AnchorBreakoutEntry
from strategy.exits.anchor_dynamic_exit import AnchorDynamicExit


def test_anchor_dynamic_exit_floor_lock_and_trailing():
    """
    Verifies that once +3.0% is touched:
    1. Stop-loss is elevated to at least +3.0% (guaranteeing >= +3.0% floor lock).
    2. Does NOT trigger a false lookahead stop-out on the promotion bar itself.
    3. Monotonically trails upwards as price reaches +5.0% and +7.0%.
    4. Triggers an exit at the locked trailing level when price pulls back.
    """
    exit_policy = AnchorDynamicExit(
        tp_target_pct=0.030,
        trailing_buffer_pct=0.010,
        emergency_sl_pct=-0.025,
    )

    pos = Position(
        symbol="005930",
        quantity=100,
        avg_price=50000.0,
        current_price=50000.0,
        high_water_mark=50000.0,
        entry_dt=datetime(2025, 1, 13, 9, 1),
        phase=1,
        metadata={"ref_bar": {"open": 49800.0, "high": 50200.0, "low": 49700.0, "close": 50100.0}},
    )
    exit_policy.on_position_opened(pos)

    # Initial checks
    assert pos.stop_loss_price == 48750.0  # -2.5%
    assert pos.take_profit_price == 51500.0  # +3.0%
    assert not pos.metadata["locked_at_tp"]

    # Bar 1 (09:02): Price surges to +4.0% (52,000) with low=50,100 (safely above Phase 2 SL 49,950)
    bar_promo = MinuteBar("005930", datetime(2025, 1, 13, 9, 2), 50200.0, 52000.0, 50100.0, 51800.0, 10000)
    sig_promo = exit_policy.evaluate_exit(pos, bar_promo)

    # Must NOT stop out on the promotion candle itself (Promotion Candle Defense)
    assert sig_promo is None
    assert pos.metadata["locked_at_tp"] is True
    # Trailing SL: 52,000 * 0.99 = 51,480 -> max(51,500 TP price, 51,480) = 51,500
    assert pos.stop_loss_price >= 51500.0

    # Bar 2 (09:03): Further rally to +6.0% (53,000)
    bar_surge = MinuteBar("005930", datetime(2025, 1, 13, 9, 3), 51800.0, 53000.0, 51700.0, 52800.0, 15000)
    sig_surge = exit_policy.evaluate_exit(pos, bar_surge)
    assert sig_surge is None
    # Trailing SL: 53,000 * 0.99 = 52,470 -> KRW tick floor = 52,400
    assert pos.stop_loss_price >= 52400.0

    # Bar 3 (09:04): Pullback below trailing SL
    bar_pullback = MinuteBar("005930", datetime(2025, 1, 13, 9, 4), 52800.0, 52800.0, 52100.0, 52200.0, 8000)
    sig_exit = exit_policy.evaluate_exit(pos, bar_pullback)
    assert sig_exit is not None
    assert sig_exit.reason == "SMART_STEP_UP_TRAILING_SL"
    assert sig_exit.target_price >= 52400.0
    # Net gain is guaranteed to be +4.8% (well above +3.0%)
    gain_pct = (sig_exit.target_price - pos.avg_price) / pos.avg_price
    assert gain_pct >= 0.045


def test_anchor_dynamic_exit_pessimistic_ambiguous_bar():
    """Verifies that an extreme ambiguous bar before TP is resolved defensively as STOP_LOSS_AMBIGUOUS."""
    exit_policy = AnchorDynamicExit(ambiguity_mode="pessimistic")
    pos = Position(
        symbol="005930",
        quantity=100,
        avg_price=50000.0,
        current_price=50000.0,
        high_water_mark=50000.0,
        entry_dt=datetime(2025, 1, 13, 9, 1),
        phase=1,
    )
    exit_policy.on_position_opened(pos)

    ambiguous_bar = MinuteBar("005930", datetime(2025, 1, 13, 9, 2), 50000.0, 53000.0, 48000.0, 52000.0, 10000)
    sig = exit_policy.evaluate_exit(pos, ambiguous_bar)
    assert sig is not None
    assert sig.reason == "STOP_LOSS_AMBIGUOUS"
    assert sig.target_price == pos.stop_loss_price


def test_anchor_dynamic_exit_timeout_1520():
    """Verifies 15:20 hard timeout market liquidation invariant."""
    exit_policy = AnchorDynamicExit()
    pos = Position(
        symbol="005930",
        quantity=100,
        avg_price=50000.0,
        current_price=50500.0,
        high_water_mark=50500.0,
        entry_dt=datetime(2025, 1, 13, 9, 5),
        phase=2,
    )
    exit_policy.on_position_opened(pos)

    bar_1520 = MinuteBar("005930", datetime(2025, 1, 13, 15, 20), 50500.0, 50600.0, 50400.0, 50500.0, 1000)
    sig = exit_policy.evaluate_exit(pos, bar_1520)
    assert sig is not None
    assert sig.reason == "TIMEOUT_1520"
    assert sig.order_type == OrderType.MARKET
