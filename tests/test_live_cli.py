"""
Unit Tests for Live System & CLI Extensions.
Verifies CLI argument parsing, safety gates, RealtimeLeaderScanner callbacks,
and pre-flight helpers.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, date, time
from unittest.mock import MagicMock, patch

import pytest

from data_pipeline.realtime_scanner import RealtimeLeaderScanner, parse_kis_minute_bars
from check_live_readiness import mask_string
from main import main


def test_mask_string():
    assert mask_string("") == "N/A"
    assert mask_string("12345", 4, 2) == "*****"
    masked = mask_string("1234567890", 2, 2)
    assert masked.startswith("12")
    assert masked.endswith("90")
    assert "*" in masked





def test_parse_kis_minute_bars():
    raw = [
        {
            "stck_bsop_date": "20261110",
            "stck_cntg_hour": "090100",
            "stck_oprc": "50000",
            "stck_hgpr": "51000",
            "stck_lwpr": "49900",
            "stck_prpr": "50800",
            "cntg_vol": "1000",
            "acml_tr_pbmn": "50000000",
        },
        {
            "stck_bsop_date": "20261110",
            "stck_cntg_hour": "090000",
            "stck_oprc": "49500",
            "stck_hgpr": "50000",
            "stck_lwpr": "49500",
            "stck_prpr": "50000",
            "cntg_vol": "2000",
            "acml_tr_pbmn": "100000000",
        },
    ]
    bars = parse_kis_minute_bars("005930", raw)
    assert len(bars) == 2
    # Verify sorted ascending
    assert bars[0].dt < bars[1].dt
    assert bars[0].open == 49500
    assert bars[1].close == 50800


def test_realtime_leader_scanner_callbacks_and_feed():
    mock_client = MagicMock()
    mock_client.get_today_minute_bars.return_value = [
        {
            "stck_bsop_date": "20261110",
            "stck_cntg_hour": "090000",
            "stck_oprc": "10000",
            "stck_hgpr": "10500",
            "stck_lwpr": "9950",
            "stck_prpr": "10400",
            "cntg_vol": "5000",
            "acml_tr_pbmn": "1200000000",
        }
    ]
    mock_feed = MagicMock()
    mock_feed.active_symbols = []

    mock_scanner_comp = MagicMock()
    mock_strategy = MagicMock()
    mock_strategy.scanner = mock_scanner_comp
    mock_strategy.on_bar.side_effect = lambda ctx, bar: mock_scanner_comp.on_bar(bar.date, bar)

    scanner = RealtimeLeaderScanner(
        client=mock_client,
        datafeed=mock_feed,
        strategy=mock_strategy,
    )

    cb_events = []
    scanner.register_callback(lambda sym, meta: cb_events.append((sym, meta)))

    # Feed condition search symbol
    scanner.feed_condition_search_symbol("005930", {"open": 10000, "price": 10400})

    # Assertions
    assert "005930" in scanner._promoted_symbols
    assert len(cb_events) == 1
    assert cb_events[0][0] == "005930"
    mock_scanner_comp.feed_condition_search_symbol.assert_called_with("005930")
    mock_scanner_comp.on_bar.assert_called()
    mock_feed.subscribe_symbol.assert_called_with("005930")


def test_realtime_leader_scanner_scan_cycle_filtering():
    mock_client = MagicMock()
    mock_client.get_condition_search_result.return_value = [
        # Candidate 1: Meets all conditions (+4.0% gain, 15억 TO, low = open)
        {
            "code": "005930",
            "name": "삼성전자",
            "open": "10000",
            "price": "10400",
            "low": "10000",
            "high": "10450",
            "trade_amt": "1500000",  # 15억
            "acml_vol": "150000",
            "chgrate": "4.0",
        },
        # Candidate 2: Rejected by low gain (+1.0%)
        {
            "code": "000660",
            "name": "SK하이닉스",
            "open": "10000",
            "price": "10100",
            "low": "9950",
            "high": "10150",
            "trade_amt": "2000000",
            "acml_vol": "200000",
            "chgrate": "1.0",
        },
        # Candidate 3: Rejected by turnover (2억)
        {
            "code": "035420",
            "name": "NAVER",
            "open": "10000",
            "price": "10500",
            "low": "10000",
            "high": "10550",
            "trade_amt": "200000",  # 2억
            "acml_vol": "20000",
            "chgrate": "5.0",
        },
        # Candidate 4: Rejected by MAE breach (low dropped -3%)
        {
            "code": "035720",
            "name": "카카오",
            "open": "10000",
            "price": "10400",
            "low": "9700",  # -3%
            "high": "10450",
            "trade_amt": "1500000",
            "acml_vol": "150000",
            "chgrate": "4.0",
        },
    ]

    mock_feed = MagicMock()
    mock_feed.active_symbols = []

    scanner = RealtimeLeaderScanner(
        client=mock_client,
        datafeed=mock_feed,
        min_gain_pct=3.0,
        min_turnover_krw=800_000_000.0,
        max_mae_pct=-2.0,
    )

    promoted = scanner.scan_cycle()

    assert promoted == ["005930"]
    assert "005930" in scanner._promoted_symbols
    assert "035720" in scanner._mae_blacklisted


def test_cli_live_safety_gate():
    """Verify `python main.py live` without `--yes` exits with code 1."""
    test_args = ["main.py", "live"]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


def test_cli_routine_live_safety_gate():
    """Verify `python main.py routine --mode live` without `--yes` exits with code 1."""
    test_args = ["main.py", "routine", "--mode", "live"]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


def test_cli_mock_invocation():
    """Verify `python main.py mock` initializes orchestrator with mock mode."""
    test_args = ["main.py", "mock", "--nowait"]
    with patch.object(sys, "argv", test_args):
        with patch("engine.daily_routine.DailyRoutineOrchestrator") as mock_orch_cls:
            mock_orch = MagicMock()
            mock_orch_cls.return_value = mock_orch
            main()
            mock_orch_cls.assert_called_once_with(
                mode="mock",
                scanner_name="all_weather",
                entry_name="all_weather",
                exit_name="all_weather",
                capital=10_000_000.0,
            )
            mock_orch.run.assert_called_once_with(wait_for_market_open=False)


def test_cli_check_live_invocation():
    """Verify `python main.py check-live --skip-ws` calls run_preflight_checks."""
    test_args = ["main.py", "check-live", "--env", "vts", "--skip-ws"]
    with patch.object(sys, "argv", test_args):
        with patch("check_live_readiness.run_preflight_checks", return_value=True) as mock_check:
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 0
            mock_check.assert_called_once_with(env_override="vts", skip_ws=True)


def test_rest_client_volume_rank_no_small_krw_inflation():
    """Verify <100M KRW turnover is NOT multiplied by 1,000,000 in UN merge."""
    from core.rest_client import KISRestClient
    from config.settings import Settings
    from core.rate_limiter import RateLimiter

    client = KISRestClient(cfg=Settings(env="vts"), limiter=RateLimiter(rate=100.0, capacity=10.0))
    with patch.object(
        client,
        "_fetch_volume_rank_single",
        side_effect=[
            [{"mksc_shrn_iscd": "005930", "acml_tr_pbmn": "50000000", "acml_vol": "1000"}],
            [],
        ],
    ):
        items = client.get_volume_rank(market_div="UN")
        assert len(items) == 1
        assert items[0]["_tot_to"] == 50_000_000.0


def test_composite_tick_entry_and_no_duplicate_sell_storm():
    """Verify live on_tick triggers breakout entry immediately & blocks duplicate SELL ticks while pending."""
    from engine.models import TickData, Position, MinuteBar
    from strategy.registry import build_strategy

    strategy = build_strategy(scanner_name="all_weather", entry_name="all_weather", exit_name="all_weather", dynamic=True)
    ctx = MagicMock()
    ctx.get_balance.return_value = MagicMock(cash=10_000_000.0)
    pos_holder = {}
    ctx.get_position.side_effect = lambda sym: pos_holder.get(sym)
    ctx.buy.return_value = "ORD_BUY_1"
    ctx.liquidate.return_value = "ORD_SELL_1"

    strategy.on_day_open(ctx, date(2026, 11, 16))
    strategy.register_symbol_day_open("999990", day_open=50000.0, day_low=50000.0, prev_close=50000.0)
    b1 = MinuteBar("999990", datetime(2026, 11, 16, 9, 1), 50000.0, 52000.0, 50000.0, 51500.0, 60000, turnover=3.0e9, cum_turnover=3.0e9)
    strategy.register_symbol_minute_bars("999990", [b1], day_open=50000.0, prev_close=50000.0, is_promoted=True)
    assert "999990" in strategy._active_candidates

    tick_entry = TickData(symbol="999990", dt=datetime(2026, 11, 16, 9, 2, 12), price=52300.0, volume=100, turnover=5.23e6, cum_turnover=3.1e9, day_open=50000.0, day_low=50000.0)
    strategy.on_tick(ctx, tick_entry)
    assert ctx.buy.call_count == 1
    assert "999990" in strategy._pending_buy_symbols

    pos_holder["999990"] = Position(symbol="999990", quantity=90, avg_price=52300.0, current_price=52300.0, high_water_mark=52300.0, entry_dt=datetime(2026, 11, 16, 9, 2, 12), stop_loss_price=51500.0, take_profit_price=54500.0)
    strategy.on_tick(ctx, TickData(symbol="999990", dt=datetime(2026, 11, 16, 9, 3, 1), price=51400.0, volume=10))
    strategy.on_tick(ctx, TickData(symbol="999990", dt=datetime(2026, 11, 16, 9, 3, 2), price=51300.0, volume=10))
    assert ctx.liquidate.call_count == 1


def test_p0_fixes_pending_orders_pre_entry_bar_and_partial_cancel():
    """Verify P0/P1 Round-3 fixes: KisLiveBroker.get_pending_orders, pre-entry bar skip, partial cancel count, UN enrichment."""
    from engine.models import TickData, Position, MinuteBar, Order, OrderEvent, OrderSide, OrderStatus, OrderType
    from engine.broker.kis_live import KisLiveBroker
    from config.settings import Settings
    from core.rate_limiter import RateLimiter
    from strategy.registry import build_strategy

    # 1. KisLiveBroker.get_pending_orders exposes pending orders cleanly
    mock_client = MagicMock()
    broker = KisLiveBroker(mode="vts", client=mock_client)
    broker._pending_orders["O1"] = Order(order_id="O1", symbol="005930", side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=10, price=50000.0)
    assert len(broker.get_pending_orders()) == 1

    # 2. Mid-minute tick entry + same-minute completed bar skips pre-entry exit & syncs ref_bar; partial cancel keeps slot count
    strategy = build_strategy(scanner_name="all_weather", entry_name="all_weather", exit_name="all_weather", dynamic=True)
    ctx = MagicMock()
    ctx.get_balance.return_value = MagicMock(cash=10_000_000.0)
    pos = Position(symbol="999990", quantity=40, avg_price=52300.0, current_price=52300.0, high_water_mark=52300.0, entry_dt=datetime(2026, 11, 16, 9, 2, 40), stop_loss_price=51300.0, take_profit_price=54000.0)
    ctx.get_position.side_effect = lambda sym: pos if sym == "999990" else None
    strategy.on_day_open(ctx, date(2026, 11, 16))
    strategy._daily_trade_count = 1

    # Completed 09:02 bar had low=49,500 BEFORE 09:02:40 entry -> must NOT liquidate, must update ref_bar
    b_same_min = MinuteBar("999990", datetime(2026, 11, 16, 9, 2), 50000.0, 52500.0, 49500.0, 52400.0, 50000, turnover=2.6e9, cum_turnover=2.6e9)
    strategy.on_bar(ctx, b_same_min)
    assert ctx.liquidate.call_count == 0
    assert pos.metadata["ref_bar"]["low"] == 49500.0
    assert pos.metadata["ref_bar"]["close"] == 52400.0
    assert pos.phase == 2
    assert pos.stop_loss_price >= 51200.0

    # Cancelling unfilled remainder of partially filled BUY must NOT decrement _daily_trade_count
    strategy.on_order_event(ctx, OrderEvent(order_id="O2", symbol="999990", side=OrderSide.BUY, status=OrderStatus.CANCELLED, filled_price=0.0, filled_quantity=0, dt=datetime(2026, 11, 16, 9, 3, 25)))
    assert strategy._daily_trade_count == 1

    # 3. VOLUME_RANK_UN missing OHLC enriches via get_current_price(market_div="UN") & caches cooldown
    vr_client = MagicMock()
    vr_client.get_condition_search_result.return_value = []
    vr_client.get_volume_rank.return_value = [{"mksc_shrn_iscd": "005930", "hts_kor_isnm": "삼성전자", "stck_prpr": "52500", "_tot_to": 5_000_000_000.0, "acml_vol": "100000"}]
    vr_client.get_current_price.return_value = {"stck_prpr": "52500", "stck_oprc": "50000", "stck_hgpr": "52800", "stck_lwpr": "49900", "stck_prdy_clpr": "50000", "prdy_ctrt": "5.0"}
    mock_feed = MagicMock()
    mock_feed.active_symbols = []
    scanner = RealtimeLeaderScanner(client=vr_client, datafeed=mock_feed, min_gain_pct=3.0, min_turnover_krw=800_000_000.0, max_mae_pct=-2.0, volume_rank_interval_sec=0.0)
    promoted = scanner.scan_cycle()
    assert promoted == ["005930"]
    vr_client.get_current_price.assert_called_once_with("005930", market_div="UN")

    # Second unpromoted symbol cached within cooldown does not hammer REST
    vr_client2 = MagicMock()
    vr_client2.get_condition_search_result.return_value = []
    vr_client2.get_volume_rank.return_value = [{"mksc_shrn_iscd": "000660", "hts_kor_isnm": "SK하이닉스", "stck_prpr": "101000", "_tot_to": 5_000_000_000.0, "acml_vol": "100000"}]
    vr_client2.get_current_price.return_value = {"stck_prpr": "101000", "stck_oprc": "100000", "stck_hgpr": "101500", "stck_lwpr": "99800", "stck_prdy_clpr": "100000", "prdy_ctrt": "1.0"}
    scanner2 = RealtimeLeaderScanner(client=vr_client2, datafeed=mock_feed, min_gain_pct=3.0, min_turnover_krw=800_000_000.0, max_mae_pct=-2.0, volume_rank_interval_sec=0.0)
    assert scanner2.scan_cycle() == []
    assert scanner2.scan_cycle() == []
    assert vr_client2.get_current_price.call_count == 1

    # 4. HTS KRX-only low turnover (< 8억) enriches via get_current_price(market_div="UN") and promotes if KRX+NXT >= 8억
    hts_client = MagicMock()
    hts_client.get_condition_search_result.return_value = [
        {"code": "003350", "name": "한국화장품제조", "price": "10400", "open": "10000", "high": "10450", "low": "9950", "trade_amt": "600000", "chgrate": "4.0"}
    ]
    hts_client.get_current_price.return_value = {"acml_tr_pbmn": "1100000000"}
    hts_client.get_today_minute_bars.return_value = []
    hts_feed = MagicMock()
    hts_feed.active_symbols = []
    hts_scanner = RealtimeLeaderScanner(
        client=hts_client, datafeed=hts_feed, enable_volume_rank_feeder=False,
        min_gain_pct=3.0, min_turnover_krw=800_000_000.0, max_mae_pct=-1.2,
    )
    assert hts_scanner.scan_cycle() == ["003350"]
    hts_client.get_current_price.assert_called_once_with("003350", market_div="UN")

    # 5. Mid-minute promotion preserves current forming bar's true open in _forming_bars for snap_ok & on_tick
    strat2 = build_strategy(scanner_name="morning_leader", entry_name="morning_breakout", exit_name="morning_leader", dynamic=False)
    strat2.on_day_open(ctx, date(2026, 11, 26))
    strat2.scanner.feed_condition_search_symbol("003350")
    now_min = datetime.now().replace(second=0, microsecond=0)
    b_cur = MinuteBar(symbol="003350", dt=now_min, open=10000.0, high=10360.0, low=9980.0, close=10360.0, volume=100000, turnover=1_020_000_000.0, cum_turnover=1_200_000_000.0)
    strat2.register_symbol_minute_bars("003350", [b_cur], day_open=10000.0, day_low=9980.0, day_high=10360.0, current_price=10360.0, cum_turnover=1_200_000_000.0)
    assert "003350" in strat2._forming_bars
    assert strat2._forming_bars["003350"].open == 10000.0



