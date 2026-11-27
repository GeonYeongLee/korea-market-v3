"""Comprehensive tests for Korea Market v4 Strategy, DailyRiskGuard, and UnifiedTradingRunner."""
from __future__ import annotations

import ast
import asyncio
import pathlib
from datetime import date, datetime, time

from audit.universal_auditor import UniversalStrategyAuditor
from tests.fakes_v4 import (
    FakeBroker, FakeClock, FakeConditionSearch, FakeFeed, FakeRegimeData,
    make_v4_runner, make_v4_strategy as _make_strategy,
)
from v4.candidate_gate import process_condition_candidates, update_forming_bar
from v4.models import (
    AccountBalance, BarCloseEvent, ConditionCandidate, IntradaySnapshot, MarketIndexRegime,
    MinuteBar, MinutePulseEvent, OrderEvent, OrderRequest, OrderSide, OrderStatus,
    Position, SymbolFeatures, TickEvent,
)
from v4.order_executor import OrderExecutor
from v4.risk import DailyRiskGuard
from v4.runner import UnifiedTradingRunner
from v4.strategy import AllWeatherEntryV4, AllWeatherScannerV4


def _run_coro(coro):
    return asyncio.run(coro)


def test_same_minute_bar0_entry_no_tp_leak_and_phase1_sl() -> None:
    dt_pulse = datetime(2026, 11, 26, 9, 2, 0)
    cond = FakeConditionSearch()
    cond.polls[dt_pulse] = [
        ConditionCandidate("005930", day_open=50_000.0, krx_day_low=49_900.0, prior_krx_day_low=49_900.0, krx_cum_turnover=1_500_000_000.0, bar_open=52_000.0, bar_high=54_500.0, seq="0", as_of=dt_pulse, is_nxt_eligible=True)
    ]
    bar0 = MinuteBar("005930", dt_pulse, 52_000.0, 54_500.0, 51_800.0, 54_200.0, 10_000, 500_000_000.0, cum_turnover=1_500_000_000.0)
    bar1 = MinuteBar("005930", datetime(2026, 11, 26, 9, 3, 0), 53_700.0, 53_800.0, 53_500.0, 53_600.0, 5_000, 300_000_000.0, cum_turnover=1_800_000_000.0)
    trades = _run_coro(make_v4_runner([MinutePulseEvent(dt_pulse), BarCloseEvent("005930", dt_pulse, bar0), BarCloseEvent("005930", bar1.dt, bar1)], cond=cond, regime=FakeRegimeData(prev_closes={"005930": 49_000.0})).run())
    assert len(trades) == 1 and trades[0].entry_dt == dt_pulse and trades[0].exit_reason == "EOD_FAILSAFE_LIQUIDATION"

    def run_bar0(bar: MinuteBar, krx_low: float, mode: str, enable_bar_sl: bool = True):
        st = _make_strategy(mode)
        st.exit_policy.enable_bar0_bar_sl = enable_bar_sl
        c = FakeConditionSearch()
        c.polls[dt_pulse] = [ConditionCandidate("005930", day_open=50_000.0, krx_day_low=krx_low, prior_krx_day_low=49_900.0, krx_cum_turnover=1_500_000_000.0, bar_open=bar.open, bar_high=bar.high, seq="0", as_of=dt_pulse)]
        return _run_coro(make_v4_runner([MinutePulseEvent(dt_pulse), BarCloseEvent("005930", dt_pulse, bar)], cond=c, regime=FakeRegimeData(prev_closes={"005930": 49_000.0}), strategy=st, mode=mode).run())

    bar0_crash = MinuteBar("005930", dt_pulse, 50_000.0, 52_600.0, 48_000.0, 52_000.0, 10_000, 500_000_000.0, cum_turnover=1_500_000_000.0)
    assert run_bar0(bar0_crash, 48_000.0, "pessimistic") == []
    t_opt = run_bar0(bar0_crash, 48_000.0, "optimistic")
    assert len(t_opt) == 1 and t_opt[0].exit_reason == "EOD_FAILSAFE_LIQUIDATION"

    # Normal green Bar 0 candle (open <= SL, low <= SL, close > SL) MUST NOT stop out in pessimistic
    bar_green = MinuteBar("005930", dt_pulse, 50_000.0, 52_600.0, 49_500.0, 52_000.0, 10_000, 500_000_000.0, cum_turnover=1_500_000_000.0)
    t_green = run_bar0(bar_green, 49_500.0, "pessimistic")
    assert len(t_green) == 1 and t_green[0].exit_reason == "EOD_FAILSAFE_LIQUIDATION"

    # Valid Bar 0 SL Case A (open > SL & low <= SL) and Case B (open <= SL & close <= SL)
    bar_sl_open_hi = MinuteBar("005930", dt_pulse, 51_800.0, 54_000.0, 50_500.0, 52_000.0, 10_000, 500_000_000.0, cum_turnover=1_500_000_000.0)
    bar_sl_close_lo = MinuteBar("005930", dt_pulse, 50_000.0, 52_600.0, 49_500.0, 51_000.0, 10_000, 500_000_000.0, cum_turnover=1_500_000_000.0)
    for b_sl, k_low in [(bar_sl_open_hi, 50_000.0), (bar_sl_close_lo, 49_500.0)]:
        tp, to = run_bar0(b_sl, k_low, "pessimistic"), run_bar0(b_sl, k_low, "optimistic")
        assert len(tp) == 1 and tp[0].exit_reason == "GRACE_EMERGENCY_SL"
        assert len(to) == 1 and to[0].exit_reason == "EOD_FAILSAFE_LIQUIDATION"
    t_dis = run_bar0(bar_sl_open_hi, 50_000.0, "pessimistic", enable_bar_sl=False)
    assert len(t_dis) == 1 and t_dis[0].exit_reason == "EOD_FAILSAFE_LIQUIDATION"
    st_tick = _make_strategy("pessimistic")
    st_tick.exit_policy.enable_bar0_bar_sl = False
    pos_tick = Position("005930", 10, 52_600.0, 52_600.0, entry_dt=dt_pulse)
    st_tick.exit_policy.on_position_opened(pos_tick)
    sig_tick = st_tick.exit_policy.evaluate_exit(pos_tick, tick=TickEvent("005930", dt_pulse, 51_500.0, 1))
    assert sig_tick is not None and sig_tick.reason == "GRACE_EMERGENCY_SL"


def test_live_bar0_rollover_no_sl_retrigger() -> None:
    dt_tick, dt_bar = datetime(2026, 11, 26, 9, 2, 15), datetime(2026, 11, 26, 9, 2, 0)
    bar0 = MinuteBar("005930", dt_bar, 50_000.0, 52_600.0, 49_900.0, 52_400.0, 10_000, 500_000_000.0, cum_turnover=1_500_000_000.0)
    tick = TickEvent("005930", dt_tick, price=52_300.0, volume=100, turnover=5_230_000.0, cum_turnover=1_500_000_000.0, day_open=50_000.0, krx_day_low=49_900.0, tr_id="H0UNCNT0")
    cond = FakeConditionSearch()
    cond.polls[dt_bar] = [ConditionCandidate("005930", day_open=50_000.0, krx_day_low=49_900.0, krx_cum_turnover=1_500_000_000.0, curr_bar_surge_ok=True, seq="0", as_of=dt_bar)]
    trades = _run_coro(make_v4_runner([MinutePulseEvent(dt_bar), tick, BarCloseEvent("005930", dt_bar, bar0)], cond=cond, regime=FakeRegimeData(prev_closes={"005930": 49_000.0}), is_live_tick_driven=True).run())
    assert len(trades) == 1 and trades[0].exit_reason == "EOD_FAILSAFE_LIQUIDATION"


def test_optimistic_vs_pessimistic_same_bar_mae_and_disqualification() -> None:
    d, dt_m = date(2026, 11, 26), datetime(2026, 11, 26, 9, 2, 0)
    cand = ConditionCandidate("005930", day_open=50_000.0, krx_day_low=49_100.0, prior_krx_day_low=49_900.0, krx_cum_turnover=1_500_000_000.0, bar_open=50_000.0, bar_high=52_000.0, seq="0", as_of=dt_m)
    sc_pess = AllWeatherScannerV4(execution_mode="pessimistic")
    sc_pess.on_day_open(d, prev_closes={"005930": 49_000.0})
    assert sc_pess.evaluate_candidate(cand) is False and sc_pess.is_disqualified("005930") is True
    sc_opt = AllWeatherScannerV4(execution_mode="optimistic")
    sc_opt.on_day_open(d, prev_closes={"005930": 49_000.0})
    assert sc_opt.evaluate_candidate(cand) is True and sc_opt.is_disqualified("005930") is True


def test_seq1_fast_pass_prohibition_blocks_cross_bar_splice() -> None:
    dt_m = datetime(2026, 11, 26, 9, 2, 0)
    cond = FakeConditionSearch()
    cond.polls[dt_m] = [ConditionCandidate("005930", day_open=50_000.0, krx_day_low=49_900.0, krx_cum_turnover=1_500_000_000.0, curr_bar_surge_ok=None, seq="1", as_of=dt_m)]
    cond.recent_bars["005930"] = [
        MinuteBar("005930", datetime(2026, 11, 26, 9, 1), 50_000.0, 52_000.0, 49_900.0, 51_500.0, 8_000, 400_000_000.0, cum_turnover=400_000_000.0),
        MinuteBar("005930", dt_m, 51_500.0, 52_000.0, 51_400.0, 51_800.0, 20_000, 1_100_000_000.0, cum_turnover=1_500_000_000.0),
    ]
    feed = FakeFeed([MinutePulseEvent(dt_m)])
    runner = UnifiedTradingRunner(_make_strategy(), FakeClock(), FakeRegimeData(prev_closes={"005930": 49_000.0}), cond, feed, FakeBroker(), DailyRiskGuard())
    _run_coro(runner.run())
    assert cond.recent_bar_calls == ["005930"] and "005930" not in feed.subscriptions


def test_minute_pulse_1521_non_nxt_zero_tick_liquidation() -> None:
    dt_entry, dt_1521 = datetime(2026, 11, 26, 9, 2, 0), datetime(2026, 11, 26, 15, 21, 0)
    bar0 = MinuteBar("006400", dt_entry, 52_000.0, 54_000.0, 51_800.0, 52_500.0, 10_000, 500_000_000.0, cum_turnover=1_500_000_000.0)
    cond = FakeConditionSearch()
    cond.polls[dt_entry] = [ConditionCandidate("006400", day_open=50_000.0, krx_day_low=49_900.0, krx_cum_turnover=1_500_000_000.0, bar_open=52_000.0, bar_high=54_000.0, seq="0", as_of=dt_entry, is_nxt_eligible=False)]
    trades = _run_coro(make_v4_runner([MinutePulseEvent(dt_entry), BarCloseEvent("006400", dt_entry, bar0), MinutePulseEvent(dt_1521)], cond=cond, regime=FakeRegimeData(prev_closes={"006400": 49_000.0})).run())
    assert len(trades) == 1 and trades[0].exit_dt == dt_1521 and trades[0].exit_reason == "HARD_TIMEOUT_1521"


def test_daily_risk_guard_and_partial_fill_and_rejection_lockout() -> None:
    guard = DailyRiskGuard(max_daily_loss=500_000.0, max_consecutive_losses=2)
    bal = AccountBalance(10_000_000.0, 10_000_000.0)
    buy_req, sell_req = OrderRequest("005930", OrderSide.BUY, "LIMIT", 10, 50_000.0), OrderRequest("005930", OrderSide.SELL, "MARKET", 10, 49_000.0)
    guard.on_day_open(date(2026, 11, 26))
    guard.on_trade_completed(-100_000.0)
    assert guard.validate_order(buy_req, bal) is True
    guard.on_trade_completed(-100_000.0)
    assert guard.is_circuit_broken and not guard.validate_order(buy_req, bal) and guard.validate_order(sell_req, bal)
    guard.on_day_open(date(2026, 11, 27))
    guard.on_trade_completed(-500_000.0)
    assert guard.is_circuit_broken and not guard.validate_order(buy_req, bal)

    strat = _make_strategy(max_pos=2)
    strat.on_day_open(date(2026, 11, 26))
    strat._daily_trade_count = 1
    strat.handle_order_event(OrderEvent("o1", "005930", OrderSide.BUY, OrderStatus.CANCELLED, filled_quantity=0, cum_filled_quantity=40), None)
    assert strat._daily_trade_count == 1
    for i in range(3):
        strat._daily_trade_count += 1
        strat.handle_order_event(OrderEvent(f"r{i}", "000660", OrderSide.BUY, OrderStatus.REJECTED, filled_quantity=0, cum_filled_quantity=0, message="REJ"), None)
    assert strat.is_locked_out("000660") is True and strat._daily_trade_count == 1


def test_late_open_1000_shift_exact_sizing_stale_cancel_and_audit_suite() -> None:
    d = date(2026, 1, 2)
    strat1 = _make_strategy(max_pos=1, shift=True)
    regime_10 = MarketIndexRegime(d, 0.2, 0.3, market_open_time=time(10, 0), market_close_time=time(15, 19))
    strat1.on_day_open(d, regime=regime_10, cash=10_000_000.0)
    assert strat1.scanner.start_time == time(10, 1) and strat1.scanner.end_time == time(10, 15)
    assert strat1.cutoff_time == time(10, 20) and strat1.exit_policy.hard_timeout == time(15, 19)
    assert strat1.compute_order_quantity(10_000.0, 10_000_000.0) == 995
    strat2 = _make_strategy(max_pos=2, shift=True)
    strat2.on_day_open(d, regime=regime_10, cash=10_000_000.0)
    assert strat2.compute_order_quantity(10_000.0, 10_000_000.0) == 499

    dt_0, dt_stale = datetime(2026, 1, 2, 10, 2, 0), datetime(2026, 1, 2, 10, 2, 50)
    bar_10 = MinuteBar("005930", dt_0, 10_000.0, 10_500.0, 9_980.0, 10_400.0, 100_000, 1_000_000_000.0, cum_turnover=1_200_000_000.0)
    cond, broker = FakeConditionSearch(), FakeBroker()
    cond.polls[dt_0] = [ConditionCandidate("005930", day_open=10_000.0, krx_day_low=9_980.0, krx_cum_turnover=1_200_000_000.0, bar_open=10_000.0, bar_high=10_500.0, seq="0", as_of=dt_0)]
    broker.buy_outcomes["005930"] = OrderStatus.PENDING
    _run_coro(make_v4_runner([MinutePulseEvent(dt_0), BarCloseEvent("005930", dt_0, bar_10), MinutePulseEvent(dt_stale)], cond=cond, broker=broker, regime=FakeRegimeData({d: regime_10}, prev_closes={"005930": 9_900.0}), strategy=strat2, stale_buy_timeout_sec=45.0).run())
    assert broker.get_pending_orders() == []

    report = UniversalStrategyAuditor().audit(_make_strategy(max_pos=1))
    assert report.passed, [r for r in report.results if not r.passed]


def test_phase2_min_open_sl_dynamic_features_premarket_and_pure_dip() -> None:
    d, dt0, dt1 = date(2026, 11, 26), datetime(2026, 11, 26, 9, 2, 0), datetime(2026, 11, 26, 9, 3, 0)
    pm_bar = MinuteBar("005930", datetime(2026, 11, 26, 8, 30), 49_000.0, 49_500.0, 48_000.0, 49_000.0, 1_000, is_premarket=True)
    feat = SymbolFeatures("005930", prior_atr20_pct=0.08, prior_adto20=10_000_000_000.0, prev_close=49_000.0, is_nxt_eligible=True)
    cond, broker = FakeConditionSearch(), FakeBroker()
    cond.polls[dt0] = [ConditionCandidate("005930", day_open=50_000.0, krx_day_low=49_900.0, krx_cum_turnover=900_000_000.0, bar_open=51_200.0, bar_high=53_000.0, seq="0", as_of=dt0)]
    bar0 = MinuteBar("005930", dt0, 51_200.0, 53_000.0, 51_100.0, 52_000.0, 10_000, 500_000_000.0, cum_turnover=900_000_000.0)
    bar1 = MinuteBar("005930", dt1, 49_500.0, 50_000.0, 49_400.0, 49_800.0, 5_000, 200_000_000.0, cum_turnover=1_100_000_000.0)
    trades = _run_coro(make_v4_runner([BarCloseEvent("005930", pm_bar.dt, pm_bar), MinutePulseEvent(dt0), BarCloseEvent("005930", dt0, bar0), BarCloseEvent("005930", dt1, bar1)], cond=cond, broker=broker, regime=FakeRegimeData(features={"005930": feat}, prev_closes={"005930": 49_000.0}), strategy=_make_strategy("pessimistic", max_pos=1)).run())
    assert len(trades) == 1 and trades[0].exit_reason == "LOCKED_SL"
    assert [r for r in broker.submitted_requests if r.side == OrderSide.SELL][0].price == 49_500.0

    ent = AllWeatherEntryV4()
    ent.on_day_open(d, features={"LOWATR": SymbolFeatures("LOWATR", prior_atr20_pct=0.025, prior_adto20=10e9)})
    assert ent.evaluate_entry("LOWATR", MinuteBar("LOWATR", dt0, 50_000.0, 53_000.0, 49_900.0, 52_500.0, 1_000)) is None

    forbidden = {"pandas", "pyarrow", "os", "pathlib", "engine", "v4.adapters", "core.telemetry", "core.dynamic_features", "core.market_regime"}
    for p in pathlib.Path("v4/strategy").glob("*.py"):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                assert not any(a.name in forbidden for a in node.names), (p, node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not any(node.module == f or node.module.startswith(f + ".") for f in forbidden), (p, node.module)


def test_un_snapshot_gate_minor_scanner_mae_and_mid_minute() -> None:
    d, dt_m = date(2026, 11, 26), datetime(2026, 11, 26, 9, 2, 0)
    cond = FakeConditionSearch()
    cond.polls[dt_m] = [ConditionCandidate("005930", day_open=50_000.0, krx_day_low=49_900.0, krx_cum_turnover=300_000_000.0, seq="0", as_of=dt_m)]
    cond.snapshots["005930"] = IntradaySnapshot("005930", dt_m, 50_000.0, 49_900.0, 52_500.0, 52_400.0, 1_500_000_000.0, 30_000, is_nxt_eligible=True, source="UN")
    strat, feed_full = _make_strategy(), FakeFeed([], max_subscriptions=0)
    strat.on_day_open(d, prev_closes={"005930": 49_000.0})
    _run_coro(process_condition_candidates(strat, cond, feed_full, {}, {"005930": 49_000.0}, dt_m, "0"))
    assert "005930" in cond.snapshot_calls and "005930" not in strat._active_candidates  # MAJOR-1 + MINOR-5 cap

    sc = strat.scanner
    assert sc.evaluate_candidate(ConditionCandidate("005930", day_open=50_000.0, krx_day_low=49_900.0, krx_cum_turnover=1_500_000_000.0, curr_bar_surge_ok=False, seq="0", as_of=dt_m)) is False
    assert sc.evaluate_candidate(ConditionCandidate("005930", day_open=50_500.0, krx_day_low=49_900.0, krx_cum_turnover=1_500_000_000.0, seq="0", as_of=dt_m), recent_bars=[MinuteBar("005930", datetime(2026, 11, 26, 9, 0), 50_000.0, 50_100.0, 49_900.0, 50_050.0, 100)]) is True
    assert sc.get_day_open("005930") == 50_000.0

    cond.polls[dt_m].append(ConditionCandidate("000660", day_open=50_000.0, krx_day_low=49_900.0, krx_cum_turnover=1_500_000_000.0, seq="0", as_of=dt_m))
    events = [MinutePulseEvent(dt_m), TickEvent("005930", datetime(2026, 11, 26, 9, 2, 10), 52_600.0, 10), TickEvent("005930", datetime(2026, 11, 26, 9, 2, 20), 52_400.0, 10), TickEvent("000660", datetime(2026, 11, 26, 9, 2, 25), 49_000.0, 10)]
    st2 = _make_strategy(max_pos=2)
    trades = _run_coro(make_v4_runner(events, cond=cond, regime=FakeRegimeData(prev_closes={"005930": 49_000.0, "000660": 49_000.0}), strategy=st2, is_live_tick_driven=True).run())
    assert len(trades) == 1 and trades[0].symbol == "005930" and st2.scanner.is_disqualified("000660")
    fb = update_forming_bar({"005930": MinuteBar("005930", dt_m, 52_000.0, 52_400.0, 52_000.0, 52_400.0, 10, cum_turnover=1_500_000_000.0)}, TickEvent("005930", datetime(2026, 11, 26, 9, 3, 0), 52_400.0, 5, cum_turnover=0.0))
    assert fb.cum_turnover == 1_500_000_000.0

    # recent_bars[-1] seeding preserves true bar.open on seq="1" promotion
    c_rb = FakeConditionSearch()
    c_rb.polls[dt_m] = [ConditionCandidate("005930", day_open=50_000.0, krx_day_low=49_900.0, krx_cum_turnover=1_500_000_000.0, seq="1", as_of=dt_m)]
    c_rb.recent_bars["005930"] = [MinuteBar("005930", dt_m, 50_000.0, 52_600.0, 49_900.0, 52_400.0, 100, cum_turnover=1_500_000_000.0)]
    st_rb, f_rb = _make_strategy(), FakeFeed([])
    st_rb.on_day_open(d, prev_closes={"005930": 49_000.0})
    _run_coro(process_condition_candidates(st_rb, c_rb, f_rb, {}, {"005930": 49_000.0}, dt_m, "1"))
    assert "005930" in st_rb._forming_bars and st_rb._forming_bars["005930"].open == 50_000.0 and "005930" not in st_rb._mid_minute_promoted


def test_zero_cash_executor_race_exception_and_cancel_reconcile() -> None:
    d, dt0 = date(2026, 11, 26), datetime(2026, 11, 26, 9, 2, 0)
    bar0 = MinuteBar("005930", dt0, 50_000.0, 52_600.0, 49_900.0, 52_400.0, 100, cum_turnover=1_500_000_000.0)
    st0, b0 = _make_strategy(), FakeBroker(initial_capital=0.0)
    st0.on_day_open(d, prev_closes={"005930": 49_000.0}, cash=0.0)
    assert st0.compute_order_quantity(10_000.0, 0.0) == 0
    sig = st0.entry.evaluate_entry("005930", bar0, context_state={"day_open": 50_000.0})
    assert sig is not None and _run_coro(OrderExecutor(st0, b0, FakeClock(), DailyRiskGuard()).submit_entry_order("005930", bar0, sig)) is None
    assert "005930" not in st0.entry._entered_symbols_today

    st1, b1, f1 = _make_strategy(max_pos=3), FakeBroker(), FakeFeed([])
    st1.on_day_open(d, prev_closes={"005930": 49_000.0, "035420": 49_000.0, "000660": 49_000.0})
    _run_coro(f1.subscribe("000660", False))
    b1.positions["000660"] = Position("000660", 10, 50_000.0, 51_000.0, entry_dt=dt0)
    b1.emit_on_submit = False
    ex1 = OrderExecutor(st1, b1, FakeClock(), DailyRiskGuard(), unsubscribe_if_idle=lambda s: f1.unsubscribe(s))
    b1.on_submit_hook = lambda _: (b1.positions.pop("000660", None), b1.emit(OrderEvent("o_s", "000660", OrderSide.SELL, OrderStatus.FILLED, net_pnl=5_000.0, dt=dt0))) if "000660" in b1.positions else None
    pos_ok = _run_coro(ex1.submit_entry_order("005930", bar0, st1.entry.evaluate_entry("005930", bar0, context_state={"day_open": 50_000.0})))
    assert pos_ok is not None and "000660" in f1.unsubscribed and "005930" not in st1._pending_buy_symbols and st1._daily_trade_count == 1
    b1.buy_outcomes["035420"] = OrderStatus.REJECTED
    b1.on_submit_hook = lambda _: b1.emit(OrderEvent("o_other", "000660", OrderSide.SELL, OrderStatus.FILLED, dt=dt0))
    assert _run_coro(ex1.submit_entry_order("035420", bar0, st1.entry.evaluate_entry("035420", MinuteBar("035420", dt0, 50_000.0, 52_600.0, 49_900.0, 52_400.0, 100), context_state={"day_open": 50_000.0}))) is None
    assert "035420" not in st1._pending_buy_symbols and st1._daily_trade_count == 1 and "035420" not in st1.entry._entered_symbols_today

    b1.on_submit_hook = lambda _: (_ for _ in ()).throw(RuntimeError("boom"))
    assert _run_coro(ex1.submit_entry_order("035420", bar0, st1.entry.evaluate_entry("035420", MinuteBar("035420", dt0, 50_000.0, 52_600.0, 49_900.0, 52_400.0, 100), context_state={"day_open": 50_000.0}))) is None
    assert "035420" not in st1._pending_buy_symbols and st1._daily_trade_count == 1 and "035420" not in st1._pending_ref_bars and "035420" not in st1.entry._entered_symbols_today
    _run_coro(ex1.submit_exit_order("005930", pos_ok.quantity, 52_000.0, "TEST", dt0))
    assert "005930" not in st1._pending_sell_symbols

    st3, b3 = _make_strategy(), FakeBroker()
    r3 = UnifiedTradingRunner(st3, FakeClock(), FakeRegimeData(prev_closes={"005930": 49_000.0}), FakeConditionSearch(), FakeFeed([]), b3, DailyRiskGuard(), is_live_tick_driven=True)
    _run_coro(r3._ensure_day_open(d))
    st3._pending_buy_symbols.add("005930")
    b3.positions["005930"] = Position("005930", 50, 52_000.0, 52_000.0, entry_dt=dt0, phase=1, stop_loss_price=51_000.0)
    b3.pending["o_p"] = OrderEvent("o_p", "005930", OrderSide.BUY, OrderStatus.PENDING, dt=dt0)
    b3.on_cancel_hook = lambda _: setattr(b3.positions["005930"], "quantity", 80)
    _run_coro(r3._dispatch_event(TickEvent("005930", datetime(2026, 11, 26, 9, 2, 10), 50_900.0, 10)))
    assert [req.quantity for req in b3.submitted_requests if req.side == OrderSide.SELL] == [80]
