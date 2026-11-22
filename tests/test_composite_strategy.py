"""
Unit tests for CompositeStrategy Lego-block assembly.
Verifies that Scanner, Entry, and Exit components interact seamlessly.
"""
from datetime import datetime, date
import pytest

from core.krx_ticks import round_to_krx_tick
from engine.models import MinuteBar, Position, OrderEvent, OrderSide, OrderStatus, TickData
from engine.broker.backtest import BacktestBroker
from engine.clock import SimulatedClock
from engine.context import StrategyContext
from engine.engine import TradingEngine
from engine.risk.risk_manager import RiskManager
from strategy.registry import build_strategy, SCANNERS, ENTRIES, EXITS
from strategy.composite import CompositeStrategy
from strategy.scanners.morning_leader_scanner import MorningLeaderScanner
from strategy.entries.morning_breakout_entry import MorningBreakoutEntry
from strategy.exits.morning_leader_exit import MorningLeaderExit


def test_composite_strategy_assembly():
    # Build default strategy: morning_leader + morning_breakout + morning_leader
    strat = build_strategy(
        scanner_name="morning_leader",
        entry_name="morning_breakout",
        exit_name="morning_leader",
    )
    assert strat.scanner.name == "morning_leader_scanner"
    assert strat.entry.name == "morning_breakout_entry"
    assert strat.exit_policy.name == "morning_leader_exit"


def test_plug_and_play_exit_swap():
    # Verify plug-and-play exit swapping via registry
    from strategy.base import IExitPolicy
    from strategy.registry import EXITS

    class MockAlternativeExit(IExitPolicy):
        name = "mock_alternative_exit"
        def __init__(self, **kwargs):
            pass
        def evaluate_exit(self, position, bar=None, tick=None):
            return None

    EXITS["mock_alt"] = MockAlternativeExit
    strat = build_strategy(
        scanner_name="morning_leader",
        entry_name="morning_breakout",
        exit_name="mock_alt",
    )
    assert strat.exit_policy.name == "mock_alternative_exit"


def test_composite_strategy_execution_flow():
    broker = BacktestBroker(initial_capital=10_000_000.0)
    clock = SimulatedClock()
    context = StrategyContext(broker=broker, clock=clock)

    strat = build_strategy(
        dynamic_slots=False,
        max_market_gap=None,
        dynamic_turnover=False,
        bear_market_defense=False,
        scanner_kwargs={"min_gap_pct": None, "max_gap_pct": None},
    )
    today = date(2024, 10, 21)
    strat.on_start(context)
    strat.on_day_open(context, today)

    # 1. Bar that does NOT meet turnover threshold
    bar1 = MinuteBar(
        symbol="005930",
        dt=datetime(2024, 10, 21, 9, 1),
        open=50000.0,
        high=51000.0,
        low=49800.0,
        close=50500.0,
        volume=1000,
        turnover=50_000_000.0,  # 5000만 원 (1분봉 3억 미달)
        cum_turnover=50_000_000.0,
    )
    broker.on_bar(bar1)
    strat.on_bar(context, bar1)
    assert broker.get_position("005930") is None

    # 2. Bar that meets turnover (1분봉 4억, 누적 12억, 양봉 +2%)
    bar2 = MinuteBar(
        symbol="005930",
        dt=datetime(2024, 10, 21, 9, 2),
        open=50000.0,
        high=52300.0,  # +4.6% 돌파!
        low=49900.0,
        close=52000.0,
        volume=8000,
        turnover=400_000_000.0,
        cum_turnover=1_200_000_000.0,
    )
    broker.on_bar(bar2)
    strat.on_bar(context, bar2)

    # Should have triggered entry!
    pos = broker.get_position("005930")
    assert pos is not None
    assert pos.quantity > 0
    assert pos.phase == 1


def test_morning_leader_reference_bar_preservation():
    """Verify that Bar 0 (entry bar) OHLC is preserved and used for Phase 2 midpoint SL."""
    broker = BacktestBroker(initial_capital=10_000_000.0)
    clock = SimulatedClock()
    context = StrategyContext(broker=broker, clock=clock)

    strat = build_strategy(
        exit_name="morning_leader",
        dynamic_turnover=False,
        bear_market_defense=False,
        scanner_kwargs={"min_gap_pct": None, "max_gap_pct": None},
    )
    broker.register_event_listener(lambda ev: strat.on_order_event(context, ev))
    today = date(2024, 10, 21)
    strat.on_start(context)
    strat.on_day_open(context, today)

    # Bar 0 (09:02): Entry Trigger Bar
    bar0 = MinuteBar(
        symbol="005930",
        dt=datetime(2024, 10, 21, 9, 2),
        open=50000.0,
        high=53000.0,  # +6.0% 돌파!
        low=49900.0,
        close=53000.0,
        volume=8000,
        turnover=400_000_000.0,
        cum_turnover=1_200_000_000.0,
    )
    broker.on_bar(bar0)
    strat.on_bar(context, bar0)

    pos = broker.get_position("005930")
    assert pos is not None
    assert pos.phase == 1
    # Check that ref_bar was properly preserved in metadata
    assert "ref_bar" in pos.metadata
    assert pos.metadata["ref_bar"]["open"] == 50000.0
    assert pos.metadata["ref_bar"]["close"] == 53000.0
    assert pos.metadata["ref_bar"]["dt"] == datetime(2024, 10, 21, 9, 2)

    # Emergency SL in Phase 1 (-2.0%)
    expected_emergency_sl = round_to_krx_tick(pos.avg_price * 0.98, side="SELL", round_mode="ceil")
    assert pos.stop_loss_price == expected_emergency_sl

    # Bar 1 (09:03): Next Bar - Should trigger Phase 2 promotion
    # Using open/close such that MFE < +3.0% (to avoid step-up trailing trigger)
    # and ensuring Bar 0 midpoint (51,500) is used, not Bar 1 midpoint (52,800)
    bar1 = MinuteBar(
        symbol="005930",
        dt=datetime(2024, 10, 21, 9, 3),
        open=52800.0,
        high=53000.0,  # MFE < +3.0%
        low=52500.0,
        close=52800.0,
        volume=5000,
        turnover=200_000_000.0,
        cum_turnover=1_400_000_000.0,
    )
    broker.on_bar(bar1)
    strat.on_bar(context, bar1)

    assert pos.phase == 2
    # Midpoint of Bar 0: (50000 + 53000) / 2 = 51500.0
    expected_ref_midpoint = round_to_krx_tick((50000.0 + 53000.0) / 2.0, side="SELL", round_mode="ceil")
    eff_emergency_sl = abs(pos.metadata.get("emergency_sl_pct", 0.02))
    floor_sl = round_to_krx_tick(pos.avg_price * (1.0 - eff_emergency_sl), side="SELL", round_mode="ceil")
    expected_target_sl = max(expected_ref_midpoint, floor_sl)
    assert pos.stop_loss_price == expected_target_sl
    assert pos.stop_loss_price != 52800.0


def test_risk_manager_feedback_loop():
    """Verify that SELL OrderEvent net_pnl is fed into RiskManager and triggers circuit breaker."""
    broker = BacktestBroker(initial_capital=10_000_000.0)
    clock = SimulatedClock()
    risk_manager = RiskManager(max_daily_loss=100_000.0, max_consecutive_losses=2)
    strat = build_strategy(
        dynamic_slots=False,
        max_market_gap=None,
        dynamic_turnover=False,
        bear_market_defense=False,
        scanner_kwargs={"min_gap_pct": None, "max_gap_pct": None},
    )

    class DummyFeed:
        def stream_events(self):
            return []

    engine = TradingEngine(
        strategy=strat,
        broker=broker,
        feed=DummyFeed(),
        clock=clock,
        risk_manager=risk_manager,
    )

    # Initial state
    assert risk_manager._daily_realized_pnl == 0.0
    assert risk_manager._consecutive_losses == 0
    assert risk_manager._circuit_broken is False

    # 1st loss trade
    loss_event_1 = OrderEvent(
        order_id="ord_1",
        symbol="005930",
        side=OrderSide.SELL,
        status=OrderStatus.FILLED,
        filled_price=49000.0,
        filled_quantity=10,
        dt=datetime(2024, 10, 21, 9, 30),
        net_pnl=-40_000.0,
    )
    engine._handle_order_event(loss_event_1)

    assert risk_manager._daily_realized_pnl == -40_000.0
    assert risk_manager._consecutive_losses == 1
    assert risk_manager._circuit_broken is False

    # 2nd consecutive loss trade (triggers max_consecutive_losses = 2)
    loss_event_2 = OrderEvent(
        order_id="ord_2",
        symbol="000660",
        side=OrderSide.SELL,
        status=OrderStatus.FILLED,
        filled_price=150000.0,
        filled_quantity=5,
        dt=datetime(2024, 10, 21, 10, 0),
        net_pnl=-70_000.0,
    )
    engine._handle_order_event(loss_event_2)

    assert risk_manager._daily_realized_pnl == -110_000.0
    assert risk_manager._consecutive_losses == 2
    assert risk_manager._circuit_broken is True

    # Validate that subsequent BUY orders are blocked by RiskManager
    blocked_order_id = engine.context.buy(symbol="005930", quantity=10, price=50000.0)
    assert blocked_order_id is None


def test_daily_trade_count_rollback_on_rejection():
    """Verify that daily trade count is rolled back when BUY orders are rejected or cancelled."""
    broker = BacktestBroker(initial_capital=10_000_000.0)
    clock = SimulatedClock()
    context = StrategyContext(broker=broker, clock=clock)

    strat = build_strategy(max_daily_positions=1)
    today = date(2024, 10, 21)
    strat.on_start(context)
    strat.on_day_open(context, today)

    assert strat._daily_trade_count == 0

    # Simulate an order being placed
    strat._daily_trade_count = 1
    strat._pending_ref_bars["005930"] = {"open": 50000.0}

    # Simulate REJECTED event
    reject_event = OrderEvent(
        order_id="ord_rej",
        symbol="005930",
        side=OrderSide.BUY,
        status=OrderStatus.REJECTED,
        filled_price=0.0,
        filled_quantity=0,
        dt=datetime(2024, 10, 21, 9, 5),
        message="ORDER_REJECTED",
    )
    strat.on_order_event(context, reject_event)

    # _daily_trade_count rolled back to 0
    assert strat._daily_trade_count == 0
    assert "005930" not in strat._pending_ref_bars

    # Simulate CANCELLED event
    strat._daily_trade_count = 1
    strat._pending_ref_bars["000660"] = {"open": 100000.0}
    cancel_event = OrderEvent(
        order_id="ord_can",
        symbol="000660",
        side=OrderSide.BUY,
        status=OrderStatus.CANCELLED,
        filled_price=0.0,
        filled_quantity=0,
        dt=datetime(2024, 10, 21, 9, 10),
        message="ORDER_CANCELLED",
    )
    strat.on_order_event(context, cancel_event)

    assert strat._daily_trade_count == 0
    assert "000660" not in strat._pending_ref_bars


def test_engine_feed_filter_coordination():
    """Verify that TradingEngine coordinates stream filtering correctly across trading phases."""
    from datetime import time
    from engine.datafeed.base import IDataFeed

    class MockDataFeed(IDataFeed):
        def __init__(self):
            self.filter_provider = None
        def register_filter(self, provider):
            self.filter_provider = provider
        def stream_events(self):
            return iter([])

    broker = BacktestBroker(initial_capital=10_000_000.0)
    clock = SimulatedClock()
    feed = MockDataFeed()
    strat = build_strategy(
        dynamic_slots=False,
        max_market_gap=None,
        dynamic_turnover=False,
        bear_market_defense=False,
        scanner_kwargs={"min_gap_pct": None, "max_gap_pct": None},
    )
    engine = TradingEngine(strategy=strat, broker=broker, feed=feed, clock=clock)

    assert feed.filter_provider is not None

    # 1. Morning 09:05 on a new date (2026-01-05): standard morning session
    is_done, syms = feed.filter_provider(datetime(2026, 1, 5, 9, 5))
    assert is_done is False
    assert syms is None
    assert engine._current_date == date(2026, 1, 5)

    # 2. Morning 09:25: no position opened, cutoff passed -> Day is done!
    is_done, syms = feed.filter_provider(datetime(2026, 1, 5, 9, 25))
    assert is_done is True
    assert syms is None

    # 3. Simulate an open position after cutoff: only held symbol streamed
    broker.positions["005930"] = MinuteBar(
        symbol="005930", dt=datetime(2026, 1, 5, 9, 10),
        open=50000.0, high=50000.0, low=50000.0, close=50000.0, volume=100
    )
    is_done, syms = feed.filter_provider(datetime(2026, 1, 5, 9, 30))
    assert is_done is False
    assert syms == {"005930"}

    # 4. Once position is closed and quota was reached -> Day done!
    broker.positions.clear()
    strat._daily_trade_count = 1
    is_done, syms = feed.filter_provider(datetime(2026, 1, 5, 9, 35))
    assert is_done is True

    # 5. Next trading day arrives (2026-01-06 09:00): rollover resets state, new day opens
    is_done, syms = feed.filter_provider(datetime(2026, 1, 6, 9, 0))
    assert is_done is False
    assert syms is None
    assert engine._current_date == date(2026, 1, 6)
    assert strat._daily_trade_count == 0


def test_dynamic_slots_kospi_gap_switching():
    """Verifies KOSPI gap dynamically switches between 2 slots (5M) and 5 slots (2M)."""
    class MockRegimeProvider:
        def __init__(self, gap_value):
            self.gap_value = gap_value
        def get_kospi_gap(self, current_date):
            return self.gap_value
        def get_kosdaq_gap(self, current_date):
            return self.gap_value
        def get_min_gap(self, current_date):
            return self.gap_value
        def get_max_gap(self, current_date):
            return self.gap_value

    scanner = MorningLeaderScanner()
    entry = MorningBreakoutEntry()
    exit_policy = MorningLeaderExit()

    # Case A: KOSPI gap >= 0.0% -> 2 slots @ 5M KRW
    strat_pos = CompositeStrategy(
        scanner=scanner,
        entry=entry,
        exit_policy=exit_policy,
        dynamic_slots=True,
        base_slots=2,
        expanded_slots=5,
        total_capital=10_000_000.0,
        regime_provider=MockRegimeProvider(0.45),
    )
    strat_pos.on_day_open(None, date(2026, 1, 5))
    assert strat_pos.max_daily_positions == 2
    assert strat_pos.current_slot_notional == 5_000_000.0
    assert entry.target_notional == 5_000_000.0

    # Case B: KOSPI gap < 0.0% -> 5 slots @ 2M KRW
    strat_neg = CompositeStrategy(
        scanner=scanner,
        entry=entry,
        exit_policy=exit_policy,
        dynamic_slots=True,
        base_slots=2,
        expanded_slots=5,
        total_capital=10_000_000.0,
        regime_provider=MockRegimeProvider(-0.80),
    )
    strat_neg.on_day_open(None, date(2026, 1, 6))
    assert strat_neg.max_daily_positions == 5
    assert strat_neg.current_slot_notional == 2_000_000.0
    assert entry.target_notional == 2_000_000.0

    # Case C: Severe Gap Down Filter (< -2.0%) -> 0 slots
    strat_crash = CompositeStrategy(
        scanner=scanner,
        entry=entry,
        exit_policy=exit_policy,
        dynamic_slots=True,
        base_slots=2,
        expanded_slots=5,
        severe_gap_down_filter=-2.0,
        total_capital=10_000_000.0,
        regime_provider=MockRegimeProvider(-2.50),
    )
    strat_crash.on_day_open(None, date(2026, 1, 7))
    assert strat_crash.max_daily_positions == 0
    assert strat_crash.current_slot_notional == 0.0


def test_scanner_no_common_stock_filter_and_condition_search():
    """Verify removal of _is_common_stock and functioning of KIS HTS condition search hooks."""
    scanner = MorningLeaderScanner()
    today = date(2026, 1, 5)
    scanner.on_day_open(today)

    # Symbol ending with non-zero (e.g. preferred share '005935' or ETF '122630' or overseas '950130')
    bar_pref = MinuteBar(
        symbol="005935",
        dt=datetime(2026, 1, 5, 9, 2),
        open=50000.0,
        high=52000.0,
        low=49900.0,
        close=51500.0,
        volume=10000,
        turnover=500_000_000.0,
        cum_turnover=1_500_000_000.0,
    )
    # 1. Without condition universe: all tickers meeting criteria qualify (no common stock check)
    qualified = scanner.on_bar(today, bar_pref)
    assert qualified is not None
    assert "005935" in qualified

    # 2. When condition search universe is set: only symbols in universe are qualified
    scanner.on_day_open(today)
    scanner.set_condition_universe(["000660", "035420"])
    assert scanner.filter_universe(today) == ["000660", "035420"] or set(scanner.filter_universe(today)) == {"000660", "035420"}

    # '005935' is not in condition universe -> rejected
    assert scanner.on_bar(today, bar_pref) is None

    # Feed symbol via real-time hook
    scanner.feed_condition_search_symbol("005935")
    assert "005935" in scanner._condition_symbols
    qualified_now = scanner.on_bar(today, bar_pref)
    assert qualified_now is not None
    assert "005935" in qualified_now


def test_super_leader_step_up_promotion_and_matured_exit():
    """Verify Super Leader promotion at candle close and matured exit after 2 holding bars."""
    exit_policy = MorningLeaderExit(step_up_enabled=True, step_up_holding_bars=2)
    pos = Position(
        symbol="005930",
        quantity=200,
        avg_price=50000.0,
        current_price=50000.0,
        high_water_mark=50000.0,
        entry_dt=datetime(2026, 1, 5, 9, 2),
        phase=1,
        metadata={"ref_bar": {"open": 50000.0, "high": 52000.0, "low": 49800.0, "close": 51500.0, "dt": datetime(2026, 1, 5, 9, 2)}},
    )
    exit_policy.on_position_opened(pos)

    # Bar 1 (09:03): High touches TP (+3.0% = 51,500), Close reaches +3.6% (51,800 >= step2_price 51,800)
    # -> Promoted to Super Leader!
    bar1 = MinuteBar("005930", datetime(2026, 1, 5, 9, 3), 51500.0, 52000.0, 51400.0, 51800.0, 10000)
    sig1 = exit_policy.evaluate_exit(pos, bar1)
    assert sig1 is None  # No exit, promoted!
    assert pos.metadata.get("is_super_leader") is True
    assert pos.stop_loss_price >= 51800.0
    assert pos.metadata.get("step_up_bars_held") == 0

    # Bar 2 (09:04): 1st holding bar. Price stays above 51,800.
    bar2 = MinuteBar("005930", datetime(2026, 1, 5, 9, 4), 51900.0, 52500.0, 51850.0, 52200.0, 8000)
    sig2 = exit_policy.evaluate_exit(pos, bar2)
    assert sig2 is None  # Still holding
    assert pos.metadata.get("step_up_bars_held") == 1

    # Bar 3 (09:05): 2nd holding bar -> Holding bars reached (2 bars) -> Exit on SMART_STEP_UP_MATURED_EXIT
    bar3 = MinuteBar("005930", datetime(2026, 1, 5, 9, 5), 52200.0, 52600.0, 52000.0, 52400.0, 7000)
    sig3 = exit_policy.evaluate_exit(pos, bar3)
    assert sig3 is not None
    assert sig3.reason == "SMART_STEP_UP_MATURED_EXIT"
    assert sig3.target_price == 52400.0


def test_super_leader_trailing_sl_exit():
    """Verify Super Leader stop loss triggers if price drops below step2_price during holding."""
    exit_policy = MorningLeaderExit(step_up_enabled=True, step_up_holding_bars=2)
    pos = Position(
        symbol="005930",
        quantity=200,
        avg_price=50000.0,
        current_price=50000.0,
        high_water_mark=50000.0,
        entry_dt=datetime(2026, 1, 5, 9, 2),
        phase=1,
        metadata={"ref_bar": {"open": 50000.0, "high": 52000.0, "low": 49800.0, "close": 51500.0, "dt": datetime(2026, 1, 5, 9, 2)}},
    )
    exit_policy.on_position_opened(pos)

    # Bar 1 (09:03): Promotion candle
    bar1 = MinuteBar("005930", datetime(2026, 1, 5, 9, 3), 51500.0, 52000.0, 51400.0, 51800.0, 10000)
    exit_policy.evaluate_exit(pos, bar1)
    assert pos.metadata.get("is_super_leader") is True
    step2_px = pos.stop_loss_price

    # Bar 2 (09:04): Dips below step2_price -> Triggers SMART_STEP_UP_TRAILING_SL
    bar2 = MinuteBar("005930", datetime(2026, 1, 5, 9, 4), 51800.0, 51900.0, step2_px - 100.0, 51600.0, 8000)
    sig2 = exit_policy.evaluate_exit(pos, bar2)
    assert sig2 is not None
    assert sig2.reason == "SMART_STEP_UP_TRAILING_SL"
    assert sig2.target_price == step2_px


def test_tp_fallback_stop_exit():
    """Verify REALTIME_TP_FALLBACK_STOP triggers when price touched TP (+3.0%) but closed below TP."""
    exit_policy = MorningLeaderExit()
    pos = Position(
        symbol="005930",
        quantity=200,
        avg_price=50000.0,
        current_price=50000.0,
        high_water_mark=50000.0,
        entry_dt=datetime(2026, 1, 5, 9, 2),
        phase=1,
        metadata={"ref_bar": {"open": 50000.0, "high": 52000.0, "low": 49800.0, "close": 51500.0, "dt": datetime(2026, 1, 5, 9, 2)}},
    )
    exit_policy.on_position_opened(pos)

    # Bar 1 (09:03): High reached 51,600 (+3.2% >= TP 51,500), but closed at 51,200 (< TP 51,500)
    bar1 = MinuteBar("005930", datetime(2026, 1, 5, 9, 3), 51400.0, 51600.0, 51000.0, 51200.0, 10000)
    sig = exit_policy.evaluate_exit(pos, bar1)
    assert sig is not None
    assert sig.reason == "REALTIME_TP_FALLBACK_STOP"
    assert sig.target_price == bar1.close  # Realistic execution at bar.close, not phantom TP price


def test_hard_timeout_1521():
    """Verify hard timeout at 15:21:00 unconditionally liquidates with Market order."""
    exit_policy = MorningLeaderExit()
    pos = Position(
        symbol="005930",
        quantity=200,
        avg_price=50000.0,
        current_price=50000.0,
        high_water_mark=50000.0,
        entry_dt=datetime(2026, 1, 5, 9, 2),
        phase=2,
    )
    exit_policy.on_position_opened(pos)

    # 15:20:00 -> No timeout
    bar_1520 = MinuteBar("005930", datetime(2026, 1, 5, 15, 20), 50500.0, 50600.0, 50400.0, 50500.0, 1000)
    sig_1520 = exit_policy.evaluate_exit(pos, bar_1520)
    assert sig_1520 is None

    # 15:21:00 -> Hard timeout triggers
    bar_1521 = MinuteBar("005930", datetime(2026, 1, 5, 15, 21), 50500.0, 50600.0, 50400.0, 50500.0, 1000)
    sig_1521 = exit_policy.evaluate_exit(pos, bar_1521)
    assert sig_1521 is not None
    assert sig_1521.reason == "HARD_TIMEOUT_1521"
    from engine.models import OrderType
    assert sig_1521.order_type == OrderType.MARKET


def test_morning_breakout_realtime_tick_window_evaluation():
    """Verify that realtime tick evaluation uses tick.dt.time() for window check."""
    entry = MorningBreakoutEntry(use_dynamic_breakout=False)
    entry.on_day_open(date(2024, 10, 21))

    bar = MinuteBar(
        symbol="005930",
        dt=datetime(2024, 10, 21, 9, 2),
        open=50000.0,
        high=52500.0,
        low=49800.0,
        close=52000.0,
        volume=5000,
    )

    # Tick at 09:04:00 (outside window 09:01 - 09:03) even though bar.dt is 09:02
    late_tick = TickData(
        symbol="005930",
        dt=datetime(2024, 10, 21, 9, 4, 1),
        price=52500.0,
        volume=10,
    )
    sig = entry.evaluate_entry("005930", bar, tick=late_tick)
    assert sig is None

    # Tick at 09:02:30 (inside window)
    valid_tick = TickData(
        symbol="005930",
        dt=datetime(2024, 10, 21, 9, 2, 30),
        price=52500.0,
        volume=10,
    )
    sig_valid = entry.evaluate_entry("005930", bar, tick=valid_tick)
    assert sig_valid is not None
    assert sig_valid.trigger_price <= 52500.0


def test_morning_breakout_zero_target_notional():
    """Verify that zero or negative target notional early exits immediately."""
    entry = MorningBreakoutEntry(target_notional=0.0, use_dynamic_breakout=False)
    entry.on_day_open(date(2024, 10, 21))

    bar = MinuteBar(
        symbol="005930",
        dt=datetime(2024, 10, 21, 9, 2),
        open=50000.0,
        high=53000.0,
        low=49800.0,
        close=52000.0,
        volume=5000,
    )
    assert entry.evaluate_entry("005930", bar) is None

    entry.target_notional = -1_000_000.0
    assert entry.evaluate_entry("005930", bar) is None


def test_morning_breakout_rollback_entered_symbol():
    """Verify that rollback_entered_symbol properly removes symbol for re-evaluation."""
    entry = MorningBreakoutEntry(use_dynamic_breakout=False)
    entry.on_day_open(date(2024, 10, 21))

    bar = MinuteBar(
        symbol="005930",
        dt=datetime(2024, 10, 21, 9, 2),
        open=50000.0,
        high=52500.0,
        low=49800.0,
        close=52000.0,
        volume=5000,
    )

    # 1. First trigger succeeds
    sig = entry.evaluate_entry("005930", bar)
    assert sig is not None
    assert "005930" in entry._entered_symbols_today

    # 2. Subsequent call blocked by single-fire guard
    assert entry.evaluate_entry("005930", bar) is None

    # 3. Rollback upon broker rejection / order cancellation
    entry.rollback_entered_symbol("005930")
    assert "005930" not in entry._entered_symbols_today

    # 4. Symbol can trigger entry again
    sig_reentered = entry.evaluate_entry("005930", bar)
    assert sig_reentered is not None


def test_morning_leader_missing_ref_bar_sl_fallback():
    """Verify that if ref_bar is missing, center SL falls back to floor SL, NOT position.avg_price (breakeven)."""
    exit_policy = MorningLeaderExit(emergency_sl_pct=-0.020)
    pos = Position(
        symbol="005930",
        quantity=200,
        avg_price=50000.0,
        current_price=50000.0,
        high_water_mark=50000.0,
        entry_dt=datetime(2026, 1, 5, 9, 2),
        phase=1,
        metadata={},  # ref_bar is missing!
    )
    exit_policy.on_position_opened(pos)
    expected_floor_sl = round_to_krx_tick(50000.0 * 0.98, side="SELL", round_mode="ceil")

    # Bar 1 (09:03): Next Bar - Should trigger Phase 2 transition
    # Price dips to 49,500 (< 50,000 entry price, but > 49,000 floor_sl)
    bar1 = MinuteBar(
        symbol="005930",
        dt=datetime(2026, 1, 5, 9, 3),
        open=50000.0,
        high=50200.0,
        low=49500.0,
        close=50100.0,
        volume=5000,
    )
    sig = exit_policy.evaluate_exit(pos, bar1)

    # If it wrongly used ref_mid = position.avg_price (50,000), it would have stopped out at 50,000!
    assert sig is None
    assert pos.phase == 2
    assert pos.stop_loss_price == expected_floor_sl


def test_morning_leader_scanner_refactored_invariants():
    """Verify live open price tracking integrity, afternoon early return, NaN defense, and lifecycle hygiene."""
    today = date(2026, 1, 5)

    # 1. Lifecycle State Hygiene: max_mae_pct normalization
    scanner = MorningLeaderScanner(
        max_mae_pct=0.015,
        min_gap_pct=None,
        max_gap_pct=None,
        use_dynamic_turnover=False,
    )
    assert scanner.max_mae_pct == -0.015

    scanner.on_day_open(today)
    scanner.set_condition_universe(["000660"])  # 005930 is NOT in condition universe yet

    # 2. Live Open Price Tracking Integrity:
    # 09:00 bar arrives for 005930 (open=50,000, low=49,800)
    bar0900 = MinuteBar(
        symbol="005930",
        dt=datetime(2026, 1, 5, 9, 0),
        open=50000.0,
        high=50500.0,
        low=49800.0,
        close=50300.0,
        volume=5000,
        turnover=250_000_000.0,
        cum_turnover=250_000_000.0,
    )
    # At 09:00, bar is rejected (out of window and not in condition universe)
    assert scanner.on_bar(today, bar0900) is None
    # BUT true market open is registered!
    assert scanner.get_day_open("005930") == 50000.0

    # At 09:02, KIS HTS condition search discovers 005930
    scanner.feed_condition_search_symbol("005930")
    bar0902 = MinuteBar(
        symbol="005930",
        dt=datetime(2026, 1, 5, 9, 2),
        open=51000.0,  # 09:02 open is 51,000
        high=53000.0,
        low=50900.0,
        close=52500.0,
        volume=10000,
        turnover=500_000_000.0,
        cum_turnover=1_500_000_000.0,
    )
    qualified = scanner.on_bar(today, bar0902)
    assert qualified is not None
    assert "005930" in qualified
    # Crucial invariant: 09:02 bar open does NOT override 09:00 market open!
    assert scanner.get_day_open("005930") == 50000.0
    assert "005930" in qualified
    # Crucial invariant: 09:02 bar open does NOT override 09:00 market open!
    assert scanner.get_day_open("005930") == 50000.0

    # 3. Early Return for Afternoon Bars:
    bar_afternoon = MinuteBar(
        symbol="005930",
        dt=datetime(2026, 1, 5, 14, 0),
        open=53000.0,
        high=54000.0,
        low=52800.0,
        close=53900.0,
        volume=5000,
        turnover=300_000_000.0,
        cum_turnover=3_000_000_000.0,
    )
    assert scanner.on_bar(today, bar_afternoon) is None

    # 4. NaN/Inf Defense:
    bar_nan = MinuteBar(
        symbol="005930",
        dt=datetime(2026, 1, 5, 9, 2),
        open=50000.0,
        high=52000.0,
        low=49800.0,
        close=float("nan"),
        volume=1000,
        turnover=100_000_000.0,
        cum_turnover=1_500_000_000.0,
    )
    assert scanner.on_bar(today, bar_nan) is None

    # 5. Lifecycle State Hygiene: Condition symbols reset across days
    tomorrow = date(2026, 1, 6)
    scanner.on_day_open(tomorrow)
    assert len(scanner._condition_symbols) == 0
    assert scanner.get_day_open("005930") is None


def test_morning_leader_scanner_champion_surge_threshold():
    """Verify default min_body_pct=0.035 (+3.5% surge) and rejection of surges below 3.5%."""
    scanner = MorningLeaderScanner()
    assert scanner.min_body_pct == 0.035
    assert scanner.use_dynamic_turnover is True
    assert scanner.min_cum_turnover == 1_000_000_000.0

    today = date(2026, 1, 5)
    scanner.on_day_open(today)

    # 1. Bar with high surge = +2.5% (< +3.5%) -> MUST BE REJECTED
    bar_below = MinuteBar(
        symbol="SYM_A",
        dt=datetime(2026, 1, 5, 9, 2),
        open=50000.0,
        high=51250.0,  # (51250 - 50000) / 50000 = +2.5%
        low=49900.0,
        close=51000.0,
        volume=10000,
        turnover=500_000_000.0,
        cum_turnover=1_500_000_000.0,
    )
    assert scanner.on_bar(today, bar_below) is None

    # 2. Bar with high surge = +3.4% (< +3.5%) -> MUST BE REJECTED
    bar_near = MinuteBar(
        symbol="SYM_B",
        dt=datetime(2026, 1, 5, 9, 2),
        open=50000.0,
        high=51700.0,  # (51700 - 50000) / 50000 = +3.4%
        low=49900.0,
        close=51500.0,
        volume=10000,
        turnover=500_000_000.0,
        cum_turnover=1_500_000_000.0,
    )
    assert scanner.on_bar(today, bar_near) is None

    # 3. Bar with high surge = +3.5% (== +3.5%) -> QUALIFIED
    bar_qual = MinuteBar(
        symbol="SYM_C",
        dt=datetime(2026, 1, 5, 9, 2),
        open=50000.0,
        high=51750.0,  # (51750 - 50000) / 50000 = +3.5%
        low=49900.0,
        close=51500.0,
        volume=10000,
        turnover=500_000_000.0,
        cum_turnover=1_500_000_000.0,
    )
    res = scanner.on_bar(today, bar_qual)
    assert res is not None
    assert "SYM_C" in res


def test_composite_exit_target_price_none_and_zero_guard():
    """Verify target_price=None and target_price=0.0 do not raise TypeError in on_bar."""
    class MockNoneTargetExit(MorningLeaderExit):
        def evaluate_exit(self, position, bar=None, tick=None):
            from engine.models import ExitSignal
            return ExitSignal(
                symbol=position.symbol,
                dt=bar.dt,
                target_price=None,
                reason="MANUAL_SL",
            )

    broker = BacktestBroker(initial_capital=10_000_000.0)
    clock = SimulatedClock()
    context = StrategyContext(broker=broker, clock=clock)

    strat = CompositeStrategy(
        scanner=MorningLeaderScanner(),
        entry=MorningBreakoutEntry(),
        exit_policy=MockNoneTargetExit(),
    )
    strat.on_start(context)
    strat.on_day_open(context, date(2026, 1, 5))

    # Create active position
    broker.positions["005930"] = Position(
        symbol="005930",
        quantity=10,
        avg_price=50000.0,
        current_price=50000.0,
    )

    bar = MinuteBar(
        symbol="005930",
        dt=datetime(2026, 1, 5, 9, 3),
        open=49000.0,
        high=50000.0,
        low=48000.0,
        close=48500.0,
        volume=1000,
    )

    # Must execute smoothly without TypeError (min comparison or format string)
    strat.on_bar(context, bar)
    assert broker.get_position("005930") is None


def test_composite_partial_fill_rollback_defense():
    """Verify slot is NOT rolled back if partial fill occurred (> 0 shares)."""
    broker = BacktestBroker(initial_capital=10_000_000.0)
    clock = SimulatedClock()
    context = StrategyContext(broker=broker, clock=clock)

    entry = MorningBreakoutEntry()
    strat = CompositeStrategy(
        scanner=MorningLeaderScanner(),
        entry=entry,
        exit_policy=MorningLeaderExit(),
        max_daily_positions=2,
    )
    strat.on_start(context)
    strat.on_day_open(context, date(2026, 1, 5))

    strat._daily_trade_count = 1
    entry._entered_symbols_today.add("005930")

    # 1. Partial fill cancelled: filled_quantity > 0 -> NO ROLLBACK
    partial_cancel = OrderEvent(
        order_id="ord_part",
        symbol="005930",
        side=OrderSide.BUY,
        status=OrderStatus.CANCELLED,
        filled_price=50000.0,
        filled_quantity=5,  # Partial fill!
        dt=datetime(2026, 1, 5, 9, 5),
    )
    strat.on_order_event(context, partial_cancel)
    assert strat._daily_trade_count == 1  # Retained!
    assert "005930" in entry._entered_symbols_today  # Retained!

    # 2. Zero fill cancelled: filled_quantity == 0 -> ROLLBACK
    strat._daily_trade_count = 1
    zero_cancel = OrderEvent(
        order_id="ord_zero",
        symbol="005930",
        side=OrderSide.BUY,
        status=OrderStatus.CANCELLED,
        filled_price=0.0,
        filled_quantity=0,  # Zero fill!
        dt=datetime(2026, 1, 5, 9, 6),
    )
    strat.on_order_event(context, zero_cancel)
    assert strat._daily_trade_count == 0  # Rolled back!
    assert "005930" not in entry._entered_symbols_today  # Rolled back!


def test_composite_division_by_zero_defense():
    """Verify CompositeStrategy handles zero positions or expanded slots without ZeroDivisionError."""
    # max_daily_positions = 0
    strat = CompositeStrategy(
        scanner=MorningLeaderScanner(),
        entry=MorningBreakoutEntry(),
        exit_policy=MorningLeaderExit(),
        max_daily_positions=0,
        base_slots=0,
        expanded_slots=0,
        dynamic_slots=True,
    )
    assert strat.current_slot_notional == 0.0

    strat.on_day_open(None, date(2026, 1, 5))
    assert strat.max_daily_positions >= 0
    assert strat.current_slot_notional >= 0.0


def test_composite_flexible_stop_loss_classification():
    """Verify exit reasons containing 'SL' or 'STOP' receive stop-loss gap-down execution logic."""
    from engine.models import ExitSignal

    class CustomSlExit(MorningLeaderExit):
        def __init__(self, reason: str, target: float):
            super().__init__()
            self.reason = reason
            self.target = target
        def evaluate_exit(self, position, bar=None, tick=None):
            return ExitSignal(
                symbol=position.symbol,
                dt=bar.dt,
                target_price=self.target,
                reason=self.reason,
            )

    broker = BacktestBroker(initial_capital=10_000_000.0)
    clock = SimulatedClock()
    context = StrategyContext(broker=broker, clock=clock)

    # 1. Stop loss exit with SL in reason (e.g. DYNAMIC_SL, TRAILING_STOP)
    strat_sl = CompositeStrategy(
        scanner=MorningLeaderScanner(),
        entry=MorningBreakoutEntry(),
        exit_policy=CustomSlExit("TRAILING_STOP", 51000.0),
    )
    strat_sl.on_day_open(context, date(2026, 1, 5))
    broker.positions["005930"] = Position("005930", quantity=10, avg_price=50000.0)

    # Bar opened lower than target (gap down past SL: open=50500 < target=51000)
    bar = MinuteBar("005930", datetime(2026, 1, 5, 9, 3), 50500.0, 51500.0, 50000.0, 50800.0, 1000)
    strat_sl.on_bar(context, bar)
    # Liquidated at min(bar.open, target_price) = min(50500, 51000) = 50500
    assert broker.get_position("005930") is None

    # 2. Non-SL exit (e.g. TAKE_PROFIT) executes at bar.close
    broker.positions["005930"] = Position("005930", quantity=10, avg_price=50000.0)
    strat_tp = CompositeStrategy(
        scanner=MorningLeaderScanner(),
        entry=MorningBreakoutEntry(),
        exit_policy=CustomSlExit("TAKE_PROFIT", 53000.0),
    )
    strat_tp.on_day_open(context, date(2026, 1, 5))
    strat_tp.on_bar(context, bar)
    # Liquidated at bar.close (50800.0)
    assert broker.get_position("005930") is None


def test_dynamic_capital_synchronization_and_cash_capping():
    """Verify strategy dynamically scales slot notional when account cash is under 10M KRW."""
    broker = BacktestBroker(initial_capital=695_010.0)
    clock = SimulatedClock()
    context = StrategyContext(broker=broker, clock=clock)

    strat = CompositeStrategy(
        scanner=MorningLeaderScanner(),
        entry=MorningBreakoutEntry(),
        exit_policy=MorningLeaderExit(),
        max_daily_positions=2,
        dynamic_slots=False,
        base_slots=2,
        total_capital=10_000_000.0,
        bear_market_defense=False,
        dynamic_max_gap=False,
        max_market_gap=None,
    )
    # 1. On day open, cash is 695,010 KRW (< 10M KRW)
    strat.on_day_open(context, date(2026, 1, 5))
    expected_effective = 695_010.0 * 0.995
    assert strat.effective_capital == pytest.approx(expected_effective, rel=1e-3)
    assert strat.current_slot_notional == pytest.approx(expected_effective / 2, rel=1e-3)
    assert strat.entry.target_notional == strat.current_slot_notional

    # 2. When available cash >= 10M KRW, caps exactly at 10M KRW
    broker_rich = BacktestBroker(initial_capital=20_000_000.0)
    context_rich = StrategyContext(broker=broker_rich, clock=clock)
    strat.on_day_open(context_rich, date(2026, 1, 5))
    assert strat.effective_capital == 10_000_000.0
    assert strat.current_slot_notional == 5_000_000.0









