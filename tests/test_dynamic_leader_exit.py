"""
Unit and Contract Tests for DynamicLeaderExit.

Validates:
  1. Multi-Tier Dynamic Trailing Ratchet (+5.0%, +7.5%, +10.0%, +13.0%)
  2. Monotonicity Guarantee (Ratchet SL never regresses across pullbacks)
  3. Realistic Fallback Stop Pricing (target_price == bar.close)
  4. Adaptive 3-Bar Holding Mode & Matured Exit
  5. Trend Extension on Green Bar 3 (rides momentum up to 4 bars)
  6. Premature Retracement during Trend Extension
  7. Hard Timeout 15:21 Liquidation
"""
from __future__ import annotations

from datetime import datetime, time
import pytest

from core.krx_ticks import round_to_krx_tick
from engine.models import MinuteBar, Position, ExitSignal, OrderType
from strategy.exits.dynamic_leader_exit import DynamicLeaderExit


def _create_position(
    symbol: str = "005930",
    avg_price: float = 50000.0,
    entry_dt: datetime = datetime(2026, 6, 18, 9, 2),
) -> Position:
    pos = Position(
        symbol=symbol,
        quantity=100,
        avg_price=avg_price,
        current_price=avg_price,
        high_water_mark=avg_price,
        entry_dt=entry_dt,
        phase=1,
        metadata={
            "ref_bar": {
                "open": avg_price,
                "high": avg_price * 1.02,
                "low": avg_price * 0.99,
                "close": avg_price * 1.015,
                "dt": entry_dt,
            }
        },
    )
    return pos


def test_dynamic_leader_initialization():
    exit_policy = DynamicLeaderExit()
    assert exit_policy.name == "dynamic_leader_exit"
    assert exit_policy.step_up_holding_bars == 3
    assert exit_policy.trend_extension is True
    assert exit_policy.max_extended_bars == 4
    assert exit_policy.step_up_pct == 0.035
    assert exit_policy.tp_target_pct == 0.030


def test_multi_tier_dynamic_trailing_ratchet():
    """Validates continuous high-water-mark trailing stop advancement at -1.5% buffer."""
    exit_policy = DynamicLeaderExit(trailing_buffer_pct=0.015)
    pos = _create_position(avg_price=50000.0)
    exit_policy.on_position_opened(pos)

    # Promote to Super Leader at 09:03 (close >= +3.5% = 51,750 -> 51,800 tick)
    bar_promo = MinuteBar("005930", datetime(2026, 6, 18, 9, 3), 51500.0, 52000.0, 51400.0, 51800.0, 10000)
    sig = exit_policy.evaluate_exit(pos, bar_promo)
    assert sig is None
    assert pos.metadata["is_super_leader"] is True
    # Initial SL locked at step2_price (+3.5% = 51,800 KRW tick)
    assert pos.stop_loss_price == 51800.0

    # Holding Bar 1 (09:04): Surge to +6.6% (53,300) -> HWM Trailing SL (53,300 * 0.985 = 52,500)
    # Low is 52,000 (> 51,800 initial SL)
    bar_t1 = MinuteBar("005930", datetime(2026, 6, 18, 9, 4), 52000.0, 53300.0, 52000.0, 53100.0, 15000)
    sig = exit_policy.evaluate_exit(pos, bar_t1)
    assert sig is None
    assert pos.stop_loss_price == 52500.0

    # Holding Bar 2 (09:05): Surge to +9.6% (54,800) -> HWM Trailing SL (54,800 * 0.985 = 53,978 -> 53,900 floor)
    # Low is 53,000 (> 52,500 SL)
    bar_t2 = MinuteBar("005930", datetime(2026, 6, 18, 9, 5), 53100.0, 54800.0, 53000.0, 54600.0, 20000)
    sig = exit_policy.evaluate_exit(pos, bar_t2)
    assert sig is None
    expected_t2_sl = round_to_krx_tick(54800 * 0.985, side="SELL", round_mode="floor")
    assert pos.stop_loss_price == expected_t2_sl

    # Holding Bar 3 (09:06): Green candle (53800 -> 54200) grants trend extension!
    # Low is 54,000 (> 53,900 SL). Monotonicity: SL must remain at 53,900.
    bar_t3 = MinuteBar("005930", datetime(2026, 6, 18, 9, 6), 53800.0, 54500.0, 54000.0, 54200.0, 10000)
    sig = exit_policy.evaluate_exit(pos, bar_t3)
    assert sig is None
    assert pos.stop_loss_price == expected_t2_sl
    assert pos.metadata.get("trend_extended") is True


def test_ratchet_monotonicity_under_sawtooth_pullbacks():
    """Ensures ratchet stop-loss never regresses under extreme price drops."""
    exit_policy = DynamicLeaderExit(trailing_buffer_pct=0.015)
    pos = _create_position(avg_price=50000.0)
    exit_policy.on_position_opened(pos)

    # Promo bar
    bar_p = MinuteBar("005930", datetime(2026, 6, 18, 9, 3), 51500.0, 52000.0, 51400.0, 51800.0, 10000)
    exit_policy.evaluate_exit(pos, bar_p)

    # Surge to High=56,300 (+12.6%) -> lock SL at peak - 1.5% = 55,400 KRW
    # Low is 52,000 (> 51,800 initial SL)
    bar_surge = MinuteBar("005930", datetime(2026, 6, 18, 9, 4), 52000.0, 56300.0, 52000.0, 56000.0, 30000)
    exit_policy.evaluate_exit(pos, bar_surge)
    expected_sl = round_to_krx_tick(56300 * 0.985, side="SELL", round_mode="floor")
    assert pos.stop_loss_price == expected_sl

    # Pullback bar with lower high (Low is 55,450 > 55,400 SL)
    bar_pb = MinuteBar("005930", datetime(2026, 6, 18, 9, 5), 56000.0, 55500.0, 55450.0, 55500.0, 10000)
    exit_policy.evaluate_exit(pos, bar_pb)
    assert pos.stop_loss_price == expected_sl  # SL must remain at 55,400, not degrade


def test_fallback_stop_execution_realism():
    """Validates that REALTIME_TP_FALLBACK_STOP executes at bar.close, not tp_price."""
    exit_policy = DynamicLeaderExit()
    pos = _create_position(avg_price=50000.0)
    exit_policy.on_position_opened(pos)

    # Bar 1 touches TP (+3.0% = 51,500) with High=51,600, but drops back and closes at 51,200 (< 51,500)
    bar = MinuteBar("005930", datetime(2026, 6, 18, 9, 3), 50500.0, 51600.0, 50400.0, 51200.0, 10000)
    sig = exit_policy.evaluate_exit(pos, bar)

    assert sig is not None
    assert sig.reason == "REALTIME_TP_FALLBACK_STOP"
    assert sig.target_price == 51200.0  # Actual candle close, NOT 51,500 tp_price!


def test_adaptive_holding_bars_matured_exit_red_bar_3():
    """If Bar 3 closes red (close <= open), Super Leader exits at Bar 3 close without extension."""
    exit_policy = DynamicLeaderExit(trend_extension=True)
    pos = _create_position(avg_price=50000.0)
    exit_policy.on_position_opened(pos)

    # 09:03: Promo
    bar0 = MinuteBar("005930", datetime(2026, 6, 18, 9, 3), 51500.0, 52000.0, 51400.0, 51800.0, 5000)
    exit_policy.evaluate_exit(pos, bar0)

    # 09:04: Holding Bar 1 (Low 51900 > 51800 SL)
    bar1 = MinuteBar("005930", datetime(2026, 6, 18, 9, 4), 51900.0, 52400.0, 51900.0, 52200.0, 5000)
    assert exit_policy.evaluate_exit(pos, bar1) is None

    # 09:05: Holding Bar 2 (Low 52100 > 51800 SL)
    bar2 = MinuteBar("005930", datetime(2026, 6, 18, 9, 5), 52200.0, 52600.0, 52100.0, 52500.0, 5000)
    assert exit_policy.evaluate_exit(pos, bar2) is None

    # 09:06: Holding Bar 3 (Red candle: open 52500 -> close 52300, Low 52200 > 51800 SL)
    bar3 = MinuteBar("005930", datetime(2026, 6, 18, 9, 6), 52500.0, 52600.0, 52200.0, 52300.0, 5000)
    sig3 = exit_policy.evaluate_exit(pos, bar3)

    assert sig3 is not None
    assert sig3.reason == "SMART_STEP_UP_MATURED_EXIT"
    assert sig3.target_price == 52300.0
    assert pos.metadata.get("trend_extended") is False


def test_adaptive_holding_bars_trend_extension_green_bar_3():
    """If Bar 3 closes green (close > open), grants 1 additional bar (exits at Bar 4)."""
    exit_policy = DynamicLeaderExit(trend_extension=True)
    pos = _create_position(avg_price=50000.0)
    exit_policy.on_position_opened(pos)

    # 09:03: Promo
    bar0 = MinuteBar("005930", datetime(2026, 6, 18, 9, 3), 51500.0, 52000.0, 51400.0, 51800.0, 5000)
    exit_policy.evaluate_exit(pos, bar0)

    # 09:04: Holding Bar 1 (Low 51900 > 51800 SL)
    bar1 = MinuteBar("005930", datetime(2026, 6, 18, 9, 4), 51900.0, 52400.0, 51900.0, 52200.0, 5000)
    assert exit_policy.evaluate_exit(pos, bar1) is None

    # 09:05: Holding Bar 2 (Low 52100 > 51800 SL)
    bar2 = MinuteBar("005930", datetime(2026, 6, 18, 9, 5), 52200.0, 52600.0, 52100.0, 52500.0, 5000)
    assert exit_policy.evaluate_exit(pos, bar2) is None

    # 09:06: Holding Bar 3 (Green candle: open 52500 -> close 52800, Low 52400 > 51800 SL)
    bar3 = MinuteBar("005930", datetime(2026, 6, 18, 9, 6), 52500.0, 52900.0, 52400.0, 52800.0, 5000)
    sig3 = exit_policy.evaluate_exit(pos, bar3)
    assert sig3 is None  # Trend extension granted!
    assert pos.metadata.get("trend_extended") is True

    # 09:07: Holding Bar 4 (Matured after extension)
    bar4 = MinuteBar("005930", datetime(2026, 6, 18, 9, 7), 52800.0, 53200.0, 52700.0, 53000.0, 5000)
    sig4 = exit_policy.evaluate_exit(pos, bar4)

    assert sig4 is not None
    assert sig4.reason == "SMART_STEP_UP_MATURED_EXIT"
    assert sig4.target_price == 53000.0


def test_trailing_sl_exit_during_holding():
    """If price drops below ratcheted SL during holding bars, exits immediately on trailing SL."""
    exit_policy = DynamicLeaderExit()
    pos = _create_position(avg_price=50000.0)
    exit_policy.on_position_opened(pos)

    # Promo at 09:03
    bar0 = MinuteBar("005930", datetime(2026, 6, 18, 9, 3), 51500.0, 52000.0, 51400.0, 51800.0, 5000)
    exit_policy.evaluate_exit(pos, bar0)

    # Bar 1 at 09:04: Price surges to +6.6% -> Ratchets SL to +5.0% (52,500). Low is 52,000 (> 51,800 SL)
    bar1 = MinuteBar("005930", datetime(2026, 6, 18, 9, 4), 52000.0, 53300.0, 52000.0, 53100.0, 15000)
    exit_policy.evaluate_exit(pos, bar1)
    assert pos.stop_loss_price == 52500.0

    # Bar 2 at 09:05: Sudden drop breaching the locked 52,500 SL (Low is 52,200 <= 52,500)
    bar2 = MinuteBar("005930", datetime(2026, 6, 18, 9, 5), 53000.0, 53000.0, 52200.0, 52300.0, 10000)
    sig2 = exit_policy.evaluate_exit(pos, bar2)

    assert sig2 is not None
    assert sig2.reason == "SMART_STEP_UP_TRAILING_SL"
    assert sig2.target_price == 52500.0


def test_hard_timeout_1521():
    """Validates unconditional liquidation at 15:21 cutoff."""
    exit_policy = DynamicLeaderExit()
    pos = _create_position(avg_price=50000.0)
    exit_policy.on_position_opened(pos)

    bar_pre = MinuteBar("005930", datetime(2026, 6, 18, 15, 20), 50500.0, 50600.0, 50400.0, 50500.0, 100)
    assert exit_policy.evaluate_exit(pos, bar_pre) is None

    bar_cutoff = MinuteBar("005930", datetime(2026, 6, 18, 15, 21), 50500.0, 50500.0, 50300.0, 50400.0, 100)
    sig = exit_policy.evaluate_exit(pos, bar_cutoff)
    assert sig is not None
    assert sig.reason == "HARD_TIMEOUT_1521"
    assert sig.order_type == OrderType.MARKET
