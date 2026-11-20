"""
Unit and integration tests for the Anchor Candle Close Betting Strategy modules:
- AnchorCloseScanner
- AnchorCloseEntry
- AnchorMorningExit (True Smart Step-Up with Overnight Continuation)
- Full 14/14 Universal Strategy Audit Contract Verification
"""
from __future__ import annotations

from datetime import datetime, time, date
import pytest

from engine.models import MinuteBar, Position, OrderType
from strategy.scanners.anchor_close_scanner import AnchorCloseScanner
from strategy.entries.anchor_close_entry import AnchorCloseEntry
from strategy.exits.anchor_morning_exit import AnchorMorningExit
from strategy.registry import build_strategy
from audit.universal_auditor import UniversalStrategyAuditor


def test_anchor_close_scanner_window_and_cache():
    """Verifies that AnchorCloseScanner filters by 15:15~15:25 window and candidate set."""
    scanner = AnchorCloseScanner()
    scanner.on_day_open(date(2025, 1, 13))

    # Mock candidate injection
    scanner._day_candidates = ["005930"]

    # 1. Bar at 10:00 (out of window) -> None
    bar_morning = MinuteBar("005930", datetime(2025, 1, 13, 10, 0), 50000.0, 50500.0, 49800.0, 50200.0, 5000, 250_000_000.0, cum_turnover=2_000_000_000.0)
    assert scanner.on_bar(date(2025, 1, 13), bar_morning) is None

    # 2. Bar at 15:20 for candidate stock -> returns ["005930"]
    bar_close = MinuteBar("005930", datetime(2025, 1, 13, 15, 20), 55000.0, 55500.0, 54800.0, 55200.0, 10000, 550_000_000.0, cum_turnover=55_000_000_000.0)
    assert scanner.on_bar(date(2025, 1, 13), bar_close) == ["005930"]

    # 3. Bar at 15:20 for non-candidate stock -> None
    bar_other = MinuteBar("999999", datetime(2025, 1, 13, 15, 20), 10000.0, 10100.0, 9900.0, 10000.0, 1000, 10_000_000.0, cum_turnover=100_000_000.0)
    assert scanner.on_bar(date(2025, 1, 13), bar_other) is None


def test_anchor_close_entry_execution_and_collar():
    """Verifies that AnchorCloseEntry executes cleanly at 15:20 with collar cap and KRX tick sizing."""
    entry = AnchorCloseEntry(collar_cap_pct=0.005, target_notional=10_000_000.0)
    entry.on_day_open(date(2025, 1, 13))

    # Entry at 15:20 on symbol 005930 at open price 50,000 KRW
    bar_entry = MinuteBar("005930", datetime(2025, 1, 13, 15, 20), 50000.0, 50500.0, 49900.0, 50200.0, 10000, 500_000_000.0)
    sig = entry.evaluate_entry("005930", bar_entry)

    assert sig is not None
    assert sig.symbol == "005930"
    assert sig.trigger_price == 50000.0
    # KRX tick at 50,000 is 100 KRW. 50,000 * 1.005 = 50,250 -> floor to 100 KRW tick is 50,200 KRW
    assert sig.collar_cap_price == 50200.0
    assert sig.reason == "ANCHOR_CLOSE_BETTING"

    # Single-fire invariant: second call on same symbol returns None
    sig_dup = entry.evaluate_entry("005930", bar_entry)
    assert sig_dup is None


def test_anchor_morning_exit_step_up_and_cutoff():
    """Verifies True Smart Step-Up on D+1 morning with 09:30 morning profit cutoff."""
    exit_policy = AnchorMorningExit(
        tp_target_pct=0.030,
        trailing_buffer_pct=0.010,
        emergency_sl_pct=-0.025,
        morning_cutoff=time(9, 30),
        hard_timeout=time(15, 20),
    )

    pos = Position(
        symbol="005930",
        quantity=100,
        avg_price=50000.0,
        current_price=50000.0,
        high_water_mark=50000.0,
        entry_dt=datetime(2025, 1, 13, 15, 20), # Bought at 15:20 on D
        phase=1,
        metadata={"ref_bar": {"open": 50000.0, "high": 50500.0, "low": 49800.0, "close": 50000.0}},
    )
    exit_policy.on_position_opened(pos)

    assert pos.stop_loss_price == 48750.0 # -2.5% emergency SL
    assert pos.take_profit_price == 51500.0 # +3.0% TP floor target

    # D+1 morning (09:01): Gap-up rally to 52,500 (+5.0%)
    bar_d1 = MinuteBar("005930", datetime(2025, 1, 14, 9, 1), 51800.0, 52500.0, 51600.0, 52200.0, 20000)
    sig_d1 = exit_policy.evaluate_exit(pos, bar_d1)

    assert sig_d1 is None # Promotion Candle Defense: Do not stop out prematurely
    assert pos.metadata["locked_at_tp"] is True
    # Trailing ratchet: 52,500 * 0.99 = 51,975 -> KRX floor tick 51,900 KRW
    assert pos.stop_loss_price >= 51500.0

    # D+1 morning (09:30): Morning cutoff reached if still holding
    bar_cutoff = MinuteBar("005930", datetime(2025, 1, 14, 9, 30), 52000.0, 52100.0, 51950.0, 52000.0, 5000)
    sig_cutoff = exit_policy.evaluate_exit(pos, bar_cutoff)
    assert sig_cutoff is not None
    assert sig_cutoff.reason == "MORNING_CUTOFF_0930"
    assert sig_cutoff.order_type == OrderType.MARKET


def test_anchor_close_universal_strategy_audit_14_contracts():
    """Verifies that Composite_anchor_close_anchor_close_anchor_morning passes all 14 universal audit contracts."""
    strategy = build_strategy(
        scanner_name="anchor_close",
        entry_name="anchor_close",
        exit_name="anchor_morning",
        ambiguity_mode="pessimistic",
    )
    auditor = UniversalStrategyAuditor()
    report = auditor.audit(strategy)

    assert report.total_checks == 14
    assert report.passed_checks == 14
    assert report.passed is True


def test_anchor_conditional_morning_exit_nxt_and_open():
    """
    Verifies AnchorConditionalMorningExit:
      1. 08:00~08:50 NXT early take profit if surge >= +3.0%
      2. 08:00~08:50 NXT no-loss-cut rule (holds through dip to avoid undershooting shakeout)
      3. 09:00 KRX Open Liquidation
    """
    exit_policy = AnchorMorningExit(
        exit_at_open=True,
        enable_nxt_exit=True,
        nxt_tp_pct=0.030,
        nxt_start_time=time(8, 0),
        nxt_end_time=time(8, 50),
        open_exit_time=time(9, 0),
        hard_timeout=time(15, 20),
    )

    pos = Position(
        symbol="005930",
        quantity=100,
        avg_price=50000.0,
        current_price=50000.0,
        high_water_mark=50000.0,
        entry_dt=datetime(2025, 1, 13, 15, 20),
        phase=1,
        metadata={"ref_bar": {"open": 50000.0, "high": 50500.0, "low": 49800.0, "close": 50000.0}},
    )
    exit_policy.on_position_opened(pos)

    # 1. NXT Pre-market (08:15) with +4.0% surge -> triggers early TP
    bar_nxt_surge = MinuteBar("005930", datetime(2025, 1, 14, 8, 15), 51500.0, 52000.0, 51400.0, 51800.0, 500)
    sig_nxt = exit_policy.evaluate_exit(pos, bar_nxt_surge)
    assert sig_nxt is not None
    assert sig_nxt.reason == "NXT_0800_EARLY_TAKE_PROFIT"
    assert sig_nxt.target_price == 51500.0 # +3.0% locked price

    # 2. NXT Pre-market (08:20) with -2.0% dip -> NO exit (no loss cut at 08:00)
    pos_dip = Position(
        symbol="005930",
        quantity=100,
        avg_price=50000.0,
        current_price=50000.0,
        high_water_mark=50000.0,
        entry_dt=datetime(2025, 1, 13, 15, 20),
        phase=1,
        metadata={"ref_bar": {"open": 50000.0, "high": 50500.0, "low": 49800.0, "close": 50000.0}},
    )
    exit_policy.on_position_opened(pos_dip)
    bar_nxt_dip = MinuteBar("005930", datetime(2025, 1, 14, 8, 20), 49000.0, 49200.0, 48800.0, 49000.0, 1000)
    sig_dip = exit_policy.evaluate_exit(pos_dip, bar_nxt_dip)
    assert sig_dip is None # Must hold to 09:00 auction

    # 3. KRX Regular Open (09:00) -> triggers open liquidation
    bar_open = MinuteBar("005930", datetime(2025, 1, 14, 9, 0), 49500.0, 49800.0, 49300.0, 49700.0, 15000)
    sig_open = exit_policy.evaluate_exit(pos_dip, bar_open)
    assert sig_open is not None
    assert sig_open.reason == "KRX_0900_OPEN_EXIT"
    assert sig_open.target_price == 49500.0 # Filled at open price


def test_anchor_conditional_5_slots_universal_strategy_audit():
    """Verifies that 5-slot CompositeStrategy with conditional 08:00 exit passes all 14 universal audit contracts."""
    strategy = build_strategy(
        scanner_name="anchor_close",
        entry_name="anchor_close",
        exit_name="anchor_conditional_0800",
        max_daily_positions=5,
        total_capital=10_000_000.0,
        ambiguity_mode="pessimistic",
    )
    assert strategy.max_daily_positions == 5
    assert strategy.current_slot_notional == 2_000_000.0 # 10M / 5 slots = 2M per slot

    auditor = UniversalStrategyAuditor()
    report = auditor.audit(strategy)

    assert report.total_checks == 14
    assert report.passed_checks == 14
    assert report.passed is True
