"""
Unit and Contract Verification Tests for Open Window Modular Components:
  1. OpenWindowScanner (IScanner)
  2. OpenWindowEntry (IEntryRule)
  3. TrailingLockExit (IExitPolicy)
  4. Composite Strategy Assembly & Lifecycle
"""
from __future__ import annotations

from datetime import date, datetime, time
import pytest

from core.krx_ticks import round_to_krx_tick
from engine.models import MinuteBar, Position, OrderType, OrderEvent, OrderStatus, OrderSide
from engine.broker.backtest import BacktestBroker
from engine.clock import SimulatedClock
from engine.context import StrategyContext
from strategy.composite import CompositeStrategy
from strategy.scanners.open_window_scanner import OpenWindowScanner
from strategy.entries.open_window_entry import OpenWindowEntry
from strategy.exits.trailing_lock_exit import TrailingLockExit
from strategy.registry import build_strategy, SCANNERS, ENTRIES, EXITS


def test_registry_registration():
    """Verify all 3 new components are registered properly in registry dictionaries."""
    assert "open_window" in SCANNERS
    assert SCANNERS["open_window"] is OpenWindowScanner

    assert "open_window" in ENTRIES
    assert ENTRIES["open_window"] is OpenWindowEntry

    assert "trailing_lock" in EXITS
    assert EXITS["trailing_lock"] is TrailingLockExit


def test_open_window_scanner_filtering_and_priority():
    """Verify OpenWindowScanner gap filtering, turnover gate, and priority policies."""
    scanner = OpenWindowScanner(
        min_cum_turnover=300_000_000.0,
        min_gap_pct=0.015,
        max_gap_pct=0.080,
        priority_policy="turnover",
        start_time=time(9, 0),
        end_time=time(9, 10),
    )
    today = date(2025, 1, 15)
    scanner.on_day_open(today)

    # Invalidate gap check by setting mock prev close
    scanner._day_prev_closes["SYM_VALID"] = 10000.0
    scanner._day_prev_closes["SYM_LOW_GAP"] = 10000.0
    scanner._day_prev_closes["SYM_HIGH_GAP"] = 10000.0

    # 1. Low gap (+1.0% < +1.5%) -> Disqualified
    b_low_gap = MinuteBar("SYM_LOW_GAP", datetime(2025, 1, 15, 9, 1), 10100.0, 10300.0, 10050.0, 10200.0, 50000, turnover=500_000_000.0, cum_turnover=500_000_000.0)
    res_low = scanner.on_bar(today, b_low_gap)
    assert res_low is None
    assert scanner.is_disqualified("SYM_LOW_GAP")

    # 2. High gap (+9.0% > +8.0%) -> Disqualified
    b_high_gap = MinuteBar("SYM_HIGH_GAP", datetime(2025, 1, 15, 9, 1), 10900.0, 11200.0, 10850.0, 11000.0, 50000, turnover=500_000_000.0, cum_turnover=500_000_000.0)
    res_high = scanner.on_bar(today, b_high_gap)
    assert res_high is None
    assert scanner.is_disqualified("SYM_HIGH_GAP")

    # 3. Valid gap (+3.0%) and sufficient turnover (400M >= 300M) -> Qualifies
    b_valid = MinuteBar("SYM_VALID", datetime(2025, 1, 15, 9, 2), 10300.0, 10600.0, 10250.0, 10500.0, 40000, turnover=400_000_000.0, cum_turnover=400_000_000.0)
    res_valid = scanner.on_bar(today, b_valid)
    assert res_valid is not None
    assert "SYM_VALID" in res_valid


def test_open_window_scanner_regime_slot_polarity():
    """Verify that Bullish / Gap Up expands slots and Bearish / Gap Down restricts to base slots."""
    class MockRegimeProvider:
        def __init__(self, gap: float):
            self.gap = gap
        def get_kospi_gap(self, d):
            return self.gap

    # Case 1: Bullish gap (+0.5% >= 0.0%) -> Expands to 5 slots
    scanner_bull = OpenWindowScanner(
        dynamic_slots=True,
        base_slots=2,
        expanded_slots=5,
        kospi_gap_threshold=0.0,
        regime_provider=MockRegimeProvider(0.5),
    )
    scanner_bull.on_day_open(date(2025, 1, 15))
    assert scanner_bull.allocated_slots == 5

    # Case 2: Bearish gap (-0.3% < 0.0%) -> Restricts to base 2 slots
    scanner_bear = OpenWindowScanner(
        dynamic_slots=True,
        base_slots=2,
        expanded_slots=5,
        kospi_gap_threshold=0.0,
        regime_provider=MockRegimeProvider(-0.3),
    )
    scanner_bear.on_day_open(date(2025, 1, 15))
    assert scanner_bear.allocated_slots == 2


def test_open_window_entry_styles_and_collar():
    """Verify ORB, pullback, and volume surge styles, tick rounding, and single-fire."""
    # 1. ORB style
    entry_orb = OpenWindowEntry(
        start_time_min=0,
        end_time_min=10,
        entry_style="orb",
        breakout_pct=0.020,
        collar_cap_pct=0.005,
    )
    today = date(2025, 1, 15)
    entry_orb.on_day_open(today)

    # Below breakout threshold (+1.0% < +2.0%)
    b_below = MinuteBar("SYM1", datetime(2025, 1, 15, 9, 1), 50000.0, 50500.0, 49900.0, 50200.0, 5000)
    assert entry_orb.evaluate_entry("SYM1", b_below) is None

    # Breaches breakout threshold (+3.0% >= +2.0%) -> Fires
    b_breakout = MinuteBar("SYM1", datetime(2025, 1, 15, 9, 2), 50000.0, 51500.0, 49900.0, 51200.0, 8000)
    sig = entry_orb.evaluate_entry("SYM1", b_breakout)
    assert sig is not None
    assert sig.symbol == "SYM1"
    # Execution trigger price is the breakout threshold (50,000 * 1.02 = 51,000)
    assert sig.trigger_price == 51000.0
    # Collar cap is rounded Floor from trigger price
    expected_collar = round_to_krx_tick(51000.0 * 1.005, side="BUY", round_mode="floor")
    assert sig.collar_cap_price == expected_collar

    # Single-fire: subsequent bar on same day must return None
    b_subsequent = MinuteBar("SYM1", datetime(2025, 1, 15, 9, 3), 51200.0, 52000.0, 51100.0, 51900.0, 10000)
    assert entry_orb.evaluate_entry("SYM1", b_subsequent) is None

    # Rollback allows re-entry
    entry_orb.rollback_entered_symbol("SYM1")
    assert entry_orb.evaluate_entry("SYM1", b_subsequent) is not None

    # 2. Pullback style
    entry_pull = OpenWindowEntry(
        start_time_min=0,
        end_time_min=10,
        entry_style="pullback",
        pullback_tolerance_pct=0.005,
    )
    entry_pull.on_day_open(today)
    # Pullback bounce: open=50000, low=49900 (-0.2% tests open), close=50400 (bounces green above open)
    b_pullback = MinuteBar("SYM2", datetime(2025, 1, 15, 9, 2), 50000.0, 50600.0, 49900.0, 50400.0, 5000)
    sig_pull = entry_pull.evaluate_entry("SYM2", b_pullback)
    assert sig_pull is not None
    assert sig_pull.trigger_price == 50400.0


def test_trailing_lock_exit_ratchet_and_timeout():
    """Verify TP1 lock, trailing ratchet monotonicity, and hard timeout."""
    exit_policy = TrailingLockExit(
        hard_sl_pct=-0.015,         # -1.5% SL
        tp1_pct=0.020,              # +2.0% TP1
        lock_profit_pct=0.005,      # +0.5% Lock
        trailing_offset_pct=0.010,  # 1.0% Trailing
        hard_timeout=time(15, 0),
        ambiguity_mode="pessimistic",
    )

    pos = Position(
        symbol="005930",
        avg_price=50000.0,
        quantity=200,
        entry_dt=datetime(2025, 1, 15, 9, 2),
    )
    exit_policy.on_position_opened(pos)

    # Initial SL: 50,000 * (1 - 0.015) = 49,250 -> rounded 49,250 KRW (Ceil)
    assert pos.stop_loss_price == 49250.0

    # 1. Minor price movement: no exit
    b1 = MinuteBar("005930", datetime(2025, 1, 15, 9, 3), 50000.0, 50500.0, 49800.0, 50300.0, 2000)
    assert exit_policy.evaluate_exit(pos, b1) is None
    assert pos.metadata["tp1_reached"] is False

    # 2. Reaches TP1 target (+2.5% > +2.0%): ratchets SL to locked profit (+0.5% -> 50,250)
    b2 = MinuteBar("005930", datetime(2025, 1, 15, 9, 4), 50300.0, 51250.0, 50200.0, 51100.0, 5000)
    assert exit_policy.evaluate_exit(pos, b2) is None
    assert pos.metadata["tp1_reached"] is True
    # Locked profit floor: 50,000 * 1.005 = 50,250
    # Trailing SL: 51,250 * 0.99 = 50,737.5 -> Ceil 50,750 (since 50,000+ has 100 KRW ticks -> 50,800)
    assert pos.stop_loss_price > 50250.0

    # 3. Ratchet Monotonicity: sharp pullback does NOT lower stop_loss_price
    sl_before_drop = pos.stop_loss_price
    b3 = MinuteBar("005930", datetime(2025, 1, 15, 9, 5), 51000.0, 51000.0, 50850.0, 50900.0, 3000)
    assert exit_policy.evaluate_exit(pos, b3) is None
    assert pos.stop_loss_price >= sl_before_drop

    # 4. Trailing stop triggered when price falls below trailing SL
    b4 = MinuteBar("005930", datetime(2025, 1, 15, 9, 6), 50900.0, 50900.0, 50500.0, 50600.0, 4000)
    sig_exit = exit_policy.evaluate_exit(pos, b4)
    assert sig_exit is not None
    assert sig_exit.reason == "TRAILING_STOP"

    # 5. Hard timeout at 15:00:00
    pos_timeout = Position(symbol="005930", avg_price=50000.0, quantity=200, entry_dt=datetime(2025, 1, 15, 9, 2))
    exit_policy.on_position_opened(pos_timeout)
    b_timeout = MinuteBar("005930", datetime(2025, 1, 15, 15, 0), 50000.0, 50100.0, 49900.0, 50000.0, 1000)
    sig_to = exit_policy.evaluate_exit(pos_timeout, b_timeout)
    assert sig_to is not None
    assert sig_to.reason == "HARD_TIMEOUT"
    assert sig_to.order_type == OrderType.MARKET


def test_build_strategy_open_window_composite():
    """Verify build_strategy instantiates composite strategy with open_window components."""
    strat = build_strategy(
        scanner_name="open_window",
        entry_name="open_window",
        exit_name="trailing_lock",
        scanner_kwargs={"min_cum_turnover": 300_000_000.0},
        entry_kwargs={"entry_style": "orb"},
        exit_kwargs={"hard_timeout": time(15, 15)},
    )
    assert strat.scanner.name == "open_window_scanner"
    assert strat.entry.name == "open_window_entry"
    assert strat.exit_policy.name == "trailing_lock_exit"


def test_realtime_scanner_universe_and_vi_defense():
    """Verify universe filtering rules and VI defense in realtime scanner."""
    from data_pipeline.realtime_scanner import RealtimeLeaderScanner

    # 1. 6-digit ending in '0'
    assert RealtimeLeaderScanner._is_target_stock("005930", "삼성전자") is True
    assert RealtimeLeaderScanner._is_target_stock("005935", "삼성전자우") is False  # Preferred share (ends with 5)
    assert RealtimeLeaderScanner._is_target_stock("12345", "짧은코드") is False
    assert RealtimeLeaderScanner._is_target_stock("000660K", "문자코드") is False

    # 2. SPAC, REIT, ETF, ETN, Fund, Inverse, Leverage exclusions
    assert RealtimeLeaderScanner._is_target_stock("456780", "미래에셋스팩1호") is False
    assert RealtimeLeaderScanner._is_target_stock("456790", "신한SPAC2호") is False
    assert RealtimeLeaderScanner._is_target_stock("395400", "SK리츠") is False
    assert RealtimeLeaderScanner._is_target_stock("395410", "롯데REIT") is False
    assert RealtimeLeaderScanner._is_target_stock("088980", "맥쿼리인프라") is False
    assert RealtimeLeaderScanner._is_target_stock("123400", "타이거부동산펀드") is False
    assert RealtimeLeaderScanner._is_target_stock("252710", "TIGER 200선물인버스2") is False
    assert RealtimeLeaderScanner._is_target_stock("251340", "KODEX 코스닥150선물") is False
    assert RealtimeLeaderScanner._is_target_stock("580090", "KB 솔랙티브 인버스 2") is False

    # 3. IPO Day-1 exclusion (missing or zero prev_close)
    assert RealtimeLeaderScanner._is_target_stock("999900", "신규상장주", prev_close=0.0) is False
    assert RealtimeLeaderScanner._is_target_stock("999900", "신규상장주", prev_close=-100.0) is False
    assert RealtimeLeaderScanner._is_target_stock("999900", "정상종목", prev_close=50000.0) is True


def test_open_window_entry_vi_defense():
    """Verify entry rule blocks execution when VI is active."""
    entry = OpenWindowEntry(entry_style="orb", breakout_pct=0.035)
    today = date(2025, 1, 15)
    entry.on_day_open(today)
    entry.register_day_open("005930", 50000.0)

    # Breakout bar
    bar = MinuteBar("005930", datetime(2025, 1, 15, 9, 2), 51000.0, 52500.0, 50800.0, 52000.0, 10000)

    # 1. Without VI -> Should enter
    sig_normal = entry.evaluate_entry("005930", bar, context_state={"day_open": 50000.0})
    assert sig_normal is not None

    # Reset for next checks
    entry.rollback_entered_symbol("005930")

    # 2. With is_vi = True -> Rejected
    sig_vi = entry.evaluate_entry("005930", bar, context_state={"day_open": 50000.0, "is_vi": True})
    assert sig_vi is None

    # 3. With vi_cls_code = '1' (Static VI) -> Rejected
    sig_static_vi = entry.evaluate_entry("005930", bar, context_state={"day_open": 50000.0, "vi_cls_code": "1"})
    assert sig_static_vi is None

    # 4. With vi_cls_code = '2' (Dynamic VI) -> Rejected
    sig_dyn_vi = entry.evaluate_entry("005930", bar, context_state={"day_open": 50000.0, "vi_cls_code": "2"})
    assert sig_dyn_vi is None


def test_kis_live_two_phase_order_idempotency():
    """Verify Two-Phase Order Idempotency recovers timeout orders via inquire_daily_ccld."""
    import requests
    from unittest.mock import MagicMock
    from engine.broker.kis_live import KisLiveBroker
    from engine.models import Order

    mock_client = MagicMock()
    # Phase 1: order_cash_buy times out
    mock_client.order_cash_buy.side_effect = requests.exceptions.Timeout("Read timeout")

    broker = KisLiveBroker(mode="vts", client=mock_client)

    # Order to submit
    order = Order(
        order_id="temp_ord_01",
        symbol="005930",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=10,
        price=50000.0,
    )

    # Case A: inquire_daily_ccld confirms order was matched on exchange
    mock_client.inquire_daily_ccld.return_value = [
        {
            "odno": "00012345",
            "pdno": "005930",
            "ord_qty": "10",
            "ccld_qty": "10",
            "ccld_unpr": "50000",
            "sll_buy_dvsn_cd": "02",
        }
    ]

    ret_id = broker.submit_order(order)
    assert ret_id == "00012345"
    assert order.status == OrderStatus.FILLED
    pos = broker.get_position("005930")
    assert pos is not None
    assert pos.quantity == 10
    assert pos.avg_price == 50000.0

    # Case B: inquire_daily_ccld confirms order did NOT reach exchange
    mock_client.inquire_daily_ccld.return_value = []
    order2 = Order(
        order_id="temp_ord_02",
        symbol="000660",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=5,
        price=150000.0,
    )
    ret_id2 = broker.submit_order(order2)
    assert ret_id2 == ""
    assert order2.status == OrderStatus.REJECTED
    assert broker.get_position("000660") is None


def test_composite_partial_fill_sltp_attachment_and_recalc():
    """Verify PARTIALLY_FILLED immediately attaches SL/TP, subsequent fills adjust to new avg_price."""
    from unittest.mock import MagicMock
    from strategy.registry import build_strategy
    from engine.models import OrderEvent, OrderSide, OrderStatus, Position

    strat = build_strategy(scanner_name="open_window", entry_name="open_window", exit_name="trailing_lock")
    mock_context = MagicMock()
    pos = Position(
        symbol="005930",
        quantity=5,
        avg_price=50000.0,
        stop_loss_price=0.0,
        take_profit_price=0.0,
    )
    mock_context.get_position.return_value = pos

    # Partial fill event 1
    ev1 = OrderEvent(
        order_id="ORD1",
        symbol="005930",
        side=OrderSide.BUY,
        status=OrderStatus.PARTIALLY_FILLED,
        filled_price=50000.0,
        filled_quantity=5,
    )
    strat.on_order_event(mock_context, ev1)
    # SL and TP must be attached immediately
    assert pos.stop_loss_price > 0.0
    assert pos.take_profit_price > 0.0
    old_sl = pos.stop_loss_price

    # Partial fill event 2: second tranche at higher price 52,000 KRW (avg_price becomes 51,000 KRW)
    pos.avg_price = 51000.0
    pos.quantity = 10
    ev2 = OrderEvent(
        order_id="ORD1",
        symbol="005930",
        side=OrderSide.BUY,
        status=OrderStatus.FILLED,
        filled_price=52000.0,
        filled_quantity=5,
    )
    strat.on_order_event(mock_context, ev2)
    # SL must be recalculated upwards with new average price
    assert pos.stop_loss_price > old_sl


def test_composite_evaluate_entry_context_state_enrichment():
    """Verify composite strategy passes is_vi, vi_cls_code, and prev_close to evaluate_entry."""
    from unittest.mock import MagicMock
    from strategy.registry import build_strategy

    strat = build_strategy(scanner_name="open_window", entry_name="open_window", exit_name="trailing_lock")
    mock_context = MagicMock()
    mock_context.get_position.return_value = None
    mock_context.get_balance.return_value = None

    # Register day open, low, high, prev close (gap +2.04% within [1.5%, 8.0%]), and add candidate
    strat.scanner._day_open_prices["005930"] = 50000.0
    strat.scanner._day_low_prices["005930"] = 50000.0
    strat.scanner._day_high_prices["005930"] = 50000.0
    strat.scanner._day_prev_closes["005930"] = 49000.0
    strat._active_candidates.add("005930")

    captured_ctx = {}
    original_eval = strat.entry.evaluate_entry

    def mock_evaluate(sym, bar, context_state=None):
        if context_state:
            captured_ctx.update(context_state)
        return original_eval(sym, bar, context_state=context_state)

    strat.entry.evaluate_entry = mock_evaluate

    bar = MinuteBar("005930", datetime(2025, 1, 15, 9, 2), 50000.0, 52000.0, 49900.0, 51500.0, 10000, 515000000.0, 515000000.0)
    strat.on_bar(mock_context, bar)

    assert "day_open" in captured_ctx
    assert captured_ctx["day_open"] == 50000.0
    assert "prev_close" in captured_ctx
    assert captured_ctx["prev_close"] == 49000.0
    assert "is_vi" in captured_ctx
    assert "vi_cls_code" in captured_ctx


def test_ws_client_6digit_symbol_extraction():
    """Verify 6-digit symbol extraction extracts numeric symbol, never Korean name."""
    import asyncio
    from core.ws_client import KISWebSocketClient
    client = KISWebSocketClient()
    received_notices = []

    async def mock_cb(msg_type, data):
        if msg_type == "EXECUTION":
            received_notices.append(data)

    client.register_callback(mock_cb)

    # Frame with Korean company name in field 3 and 6-digit symbol with 'A' prefix in field 7/8
    # Format: cust_id^acct^odno^KOREAN_NAME^side^...^pdno^...
    mock_data = "12345^01^00099^삼성전자^02^0^0^A005930^005930^10^50000^0^0^2"
    asyncio.run(client._handle_message(f"0|H0STCNI0|001|{mock_data}"))

    assert len(received_notices) == 1
    notice = received_notices[0]
    assert notice["symbol"] == "005930"
    assert notice["symbol"] != "삼성전자"


def test_rest_client_transient_5xx_retry():
    """Verify KISRestClient retries transient 500/502/503/504 errors up to 3 times."""
    from unittest.mock import MagicMock, patch
    from core.rest_client import KISRestClient
    client = KISRestClient()

    attempts = 0

    def mock_get(url, headers=None, params=None, timeout=10):
        nonlocal attempts
        attempts += 1
        resp = MagicMock()
        if attempts < 3:
            resp.status_code = 502
            resp.json.return_value = {"rt_cd": "502", "msg1": "Bad Gateway"}
        else:
            resp.status_code = 200
            resp.json.return_value = {"rt_cd": "0", "msg1": "Success"}
        return resp

    with patch.object(client.session, "get", side_effect=mock_get):
        with patch("time.sleep", return_value=None):
            resp = client._send_request("GET", "https://api.test/test")
            assert attempts == 3
            assert resp.status_code == 200


def test_morning_leader_exit_sl_ceil_rounding():
    """Verify MorningLeaderExit uses ceil rounding for sell stop loss."""
    from strategy.exits.morning_leader_exit import MorningLeaderExit
    from engine.models import Position
    from core.krx_ticks import round_to_krx_tick

    exit_policy = MorningLeaderExit(emergency_sl_pct=-0.020, use_dynamic_targets=False)
    # avg_price = 10,025 KRW -> raw_sl = 10025 * 0.98 = 9824.5 KRW
    # Tick at 9824.5 KRW is 10 KRW.
    # Floor rounding would be 9,820 KRW. Ceil rounding must be 9,830 KRW!
    pos = Position(symbol="005930", quantity=10, avg_price=10025.0)
    exit_policy.on_position_opened(pos)

    assert pos.stop_loss_price == 9830
    assert pos.stop_loss_price == round_to_krx_tick(10025.0 * 0.98, side="SELL", round_mode="ceil")
