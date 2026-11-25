"""
Regression tests for non-strategy infrastructure and module bugfixes on main.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from config.settings import Settings
from core.krx_ticks import round_to_krx_tick
from core.rate_limiter import RateLimiter
from core.rest_client import KISRestClient
from core.ws_client import KISWebSocketClient
from data_pipeline.realtime_scanner import RealtimeLeaderScanner
from engine.broker.backtest import BacktestBroker
from engine.broker.kis_live import KisLiveBroker
from engine.clock import SimulatedClock
from engine.context import StrategyContext
from engine.datafeed.historical import HistoricalDataFeed
from engine.models import (
    EntrySignal,
    ExitSignal,
    MinuteBar,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from strategy.base import IEntryRule, IExitPolicy, IScanner
from strategy.composite import CompositeStrategy
from strategy.exits.dynamic_leader_exit import DynamicLeaderExit


def test_historical_feed_afternoon_none_active_syms(tmp_path):
    """
    filter_provider returning (False, None) past afternoon cutoff must continue
    streaming all symbols (not abort due to falsy None), whereas set() stops.
    """
    day_dir = tmp_path / "20260105"
    day_dir.mkdir(parents=True)
    df = pd.DataFrame([
        {
            "symbol": "005930",
            "timestamp": pd.Timestamp("2026-01-05 09:05:00"),
            "open": 50000.0,
            "high": 50200.0,
            "low": 49900.0,
            "close": 50100.0,
            "volume": 1000,
            "turnover": 50_100_000.0,
            "vwap": 50050.0,
        },
        {
            "symbol": "005930",
            "timestamp": pd.Timestamp("2026-01-05 13:00:00"),
            "open": 50500.0,
            "high": 50600.0,
            "low": 50400.0,
            "close": 50550.0,
            "volume": 500,
            "turnover": 25_275_000.0,
            "vwap": 50500.0,
        },
    ])
    df.to_parquet(day_dir / "part0.parquet", index=False)

    feed_all = HistoricalDataFeed(data_dir=str(tmp_path))
    feed_all.register_filter(lambda dt: (False, None))
    bars_all = list(feed_all.stream_events())
    assert len(bars_all) == 2
    assert bars_all[1].dt == datetime(2026, 1, 5, 13, 0)

    feed_empty = HistoricalDataFeed(data_dir=str(tmp_path))
    feed_empty.register_filter(lambda dt: (False, set()) if dt.hour >= 10 else (False, None))
    bars_empty = list(feed_empty.stream_events())
    assert len(bars_empty) == 1
    assert bars_empty[0].dt == datetime(2026, 1, 5, 9, 5)


def test_composite_strategy_non_sl_target_price_and_sync_reject_rollback():
    """
    1) Non-SL ExitSignal with explicit target_price > 0 executes at target_price, not bar.close.
    2) Synchronous buy failure (None/'') invokes entry.rollback_entered_symbol(symbol).
    """
    class StubScanner(IScanner):
        name = "stub_scanner"
        def filter_universe(self, current_date):
            return ["005930"]
        def on_bar(self, current_date, bar):
            return [bar.symbol]

    class StubEntry(IEntryRule):
        name = "stub_entry"
        def __init__(self):
            self.rollbacks = []
        def evaluate_entry(self, symbol, bar, tick=None, context_state=None):
            return EntrySignal(symbol=symbol, dt=bar.dt, trigger_price=bar.close, collar_cap_price=bar.close, reason="TEST")
        def rollback_entered_symbol(self, symbol):
            self.rollbacks.append(symbol)

    class StubExit(IExitPolicy):
        name = "stub_exit"
        def evaluate_exit(self, position, bar=None, tick=None):
            return ExitSignal(
                symbol=position.symbol,
                dt=bar.dt,
                order_type=OrderType.LIMIT,
                target_price=51200.0,
                reason="CUSTOM_TAKE_PROFIT",
            )

    scanner = StubScanner()
    entry = StubEntry()
    exit_policy = StubExit()
    strat = CompositeStrategy(
        scanner=scanner,
        entry=entry,
        exit_policy=exit_policy,
        dynamic_slots=False,
        max_market_gap=None,
        bear_market_defense=False,
    )

    mock_ctx = MagicMock()
    mock_ctx.get_balance.return_value = None

    # 1. Entry blocked synchronously (context.buy returns None) -> rollback_entered_symbol called
    mock_ctx.get_position.return_value = None
    mock_ctx.buy.return_value = None
    bar_entry = MinuteBar("005930", datetime(2026, 1, 5, 9, 5), 50000.0, 50500.0, 49900.0, 50400.0, 1000, 1e9, 1e9)
    strat.on_bar(mock_ctx, bar_entry)
    assert "005930" in entry.rollbacks

    # 2. Non-SL exit uses explicit target_price=51200.0 instead of bar.close=51900.0
    pos = Position(symbol="005930", quantity=10, avg_price=50000.0)
    mock_ctx.get_position.return_value = pos
    bar_exit = MinuteBar("005930", datetime(2026, 1, 5, 9, 10), 50400.0, 52000.0, 50300.0, 51900.0, 1000, 1e9, 1e9)
    strat.on_bar(mock_ctx, bar_exit)
    mock_ctx.liquidate.assert_called_once_with("005930", price=51200.0, tag="CUSTOM_TAKE_PROFIT")


def test_backtest_broker_partial_sell_and_balance_isolation():
    """
    BacktestBroker handles partial SELL quantities accurately without dropping
    remaining position shares, copies positions snapshots, and supports cancel_pending_orders.
    """
    broker = BacktestBroker(initial_capital=10_000_000.0, slippage_rate=0.0)
    broker.current_dt = datetime(2026, 1, 5, 9, 5)

    broker.submit_order(Order(
        order_id="b1", symbol="005930", side=OrderSide.BUY,
        order_type=OrderType.LIMIT, quantity=100, price=50000.0,
    ))
    assert broker.get_position("005930").quantity == 100

    # Balance snapshot isolation check
    bal = broker.get_balance()
    bal.positions.clear()
    assert "005930" in broker.positions

    # Partial sell 40 shares -> 60 remain
    broker.current_dt = datetime(2026, 1, 5, 9, 15)
    broker.submit_order(Order(
        order_id="s1", symbol="005930", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=40, price=52000.0, tag="PARTIAL_TP",
    ))
    rem = broker.get_position("005930")
    assert rem is not None
    assert rem.quantity == 60
    assert len(broker.completed_trades) == 1
    assert broker.completed_trades[0].quantity == 40
    assert broker.completed_trades[0].gross_pnl == pytest.approx((52000.0 - 50000.0) * 40)

    # cancel_pending_orders parity
    broker.pending_orders["p1"] = Order("p1", "005930", OrderSide.BUY, OrderType.LIMIT, 10, 50000.0)
    assert broker.cancel_pending_orders("005930") == ["p1"]
    assert "p1" not in broker.pending_orders


def test_kis_live_broker_notice_guards_raw_aliases_and_krx_routing():
    """
    Verifies cntg_yn/zero-qty notice filtering, raw KIS key aliases, NXT limit exemption,
    EOD_AUCTION KRX routing (preserving Two-Phase Idempotency), and ord_psbl_cash_amt priority.
    """
    mock_client = MagicMock()
    mock_client.order_cash_buy.return_value = {"rt_cd": "0", "output": {"ODNO": "OD_BUY"}}
    mock_client.order_cash_sell.return_value = {"rt_cd": "0", "output": {"ODNO": "OD_SELL"}}
    mock_client.get_balance.return_value = {
        "holdings": [],
        "summary": {
            "ord_psbl_cash_amt": "7000000",
            "ord_psbl_cash": "8000000",
            "dnca_tot_amt": "10000000",
            "tot_evlu_amt": "10000000",
        },
    }
    broker = KisLiveBroker(mode="real", client=mock_client)
    events = []
    broker.register_event_listener(lambda e: events.append(e))

    # 1. Non-fill receipt notice (cntg_yn="1") and qty=0 must be ignored
    broker.handle_execution_notice({
        "order_no": "OD_0", "symbol": "005930", "side": "BUY", "qty": 10, "price": 50000, "cntg_yn": "1"
    })
    broker.handle_execution_notice({
        "order_no": "OD_0", "symbol": "005930", "side": "BUY", "qty": 0, "price": 50000, "cntg_yn": "2"
    })
    assert len(events) == 0
    assert broker.get_position("005930") is None

    # 2. Raw KIS key aliases work seamlessly
    broker.handle_execution_notice({
        "ord_no": "OD_RAW", "pdno": "005930", "sll_buy_dvsn_cd": "02",
        "cntg_qty": "15", "cntg_unpr": "50000", "cntg_yn": "2"
    })
    assert broker.get_position("005930").quantity == 15

    # 3. EOD_AUCTION_1520 passes exchange="KRX"
    broker.submit_order(Order(
        order_id="", symbol="005930", side=OrderSide.SELL,
        order_type=OrderType.MARKET, quantity=15, tag="EOD_AUCTION_1520",
    ))
    mock_client.order_cash_sell.assert_called_once_with(
        symbol="005930", qty=15, price=0, order_type="01", exchange="KRX"
    )

    # 4. NXT limit exit with explicit price keeps Limit Order ("00"), not Market ("01")
    broker.submit_order(Order(
        order_id="", symbol="000660", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=10, price=51000.0, tag="NXT_EXIT",
    ))
    assert mock_client.order_cash_sell.call_args_list[1][1]["order_type"] == "00"
    assert mock_client.order_cash_sell.call_args_list[1][1]["price"] == 51000

    # 5. sync_account_state prioritizes ord_psbl_cash_amt
    broker.sync_account_state()
    assert broker.get_balance().cash == 7_000_000.0


def test_rest_client_volume_rank_no_small_turnover_inflation_and_null_guards():
    """
    KIS FHPST01710000 returns acml_tr_pbmn in KRW. Values < 100M KRW (e.g. 50M)
    must NOT be multiplied by 1,000,000x, and null JSON outputs must not crash.
    """
    client = KISRestClient(cfg=Settings(env="vts"), limiter=RateLimiter(rate=100.0, capacity=10.0))

    with patch.object(
        client,
        "_fetch_volume_rank_single",
        side_effect=[
            [{"mksc_shrn_iscd": "005930", "acml_tr_pbmn": "50000000", "acml_vol": "1000"}],
            [{"mksc_shrn_iscd": "005930", "acml_tr_pbmn": "10000000", "acml_vol": "200"}],
        ],
    ):
        ranked = client.get_volume_rank(market_div="UN")
    assert len(ranked) == 1
    assert ranked[0]["_tot_to"] == pytest.approx(60_000_000.0)
    assert ranked[0]["acml_tr_pbmn"] == "60000000"

    # Null JSON output fields defense
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"rt_cd": "0", "output": None, "output1": None, "output2": None}
    with patch.object(client, "_send_request", return_value=mock_resp), \
         patch.object(client.limiter, "acquire_sync"), \
         patch.object(client.token_mgr, "get_access_token", return_value="tok"):
        assert client.get_current_price("005930") == {}
        assert client.get_balance() == {"holdings": [], "summary": {}}
        assert client.get_condition_search_result() == []


def test_ws_client_closed_guard_malformed_notice_and_pingpong_safety():
    """Verifies ws.closed check, safe execution notice float coercion, and PINGPONG fault tolerance."""
    client = KISWebSocketClient()

    mock_ws = MagicMock()
    mock_ws.open = True
    mock_ws.closed = True
    client._ws = mock_ws
    assert client.is_connected is False

    # Short H0STCNI0 frame with empty/invalid price field must not throw ValueError
    notices = []
    async def cb(mtype, data):
        if mtype == "EXECUTION":
            notices.append(data)
    client.register_callback(cb)

    short_frame = "0|H0STCNI0|001|1234^OD1^005930^삼성전자^02^10^BAD_NUM"
    asyncio.run(client._handle_message(short_frame))
    assert len(notices) == 1
    assert notices[0]["qty"] == 10
    assert notices[0]["price"] == 0.0

    # PINGPONG send error is gracefully caught
    async def failing_send(msg):
        raise RuntimeError("socket closing")
    mock_ws.send = MagicMock(side_effect=failing_send)
    asyncio.run(client._handle_message('{"header":{"tr_id":"PINGPONG"}}'))


def test_realtime_leader_scanner_name_filter_and_quote_fallback():
    """
    Volume rank fallback must filter out ETFs/SPACs by hts_kor_isnm and fetch
    KRX current quote when stck_oprc/hgpr/lwpr are absent/zero.
    """
    mock_client = MagicMock()
    mock_client.get_condition_search_result.return_value = []
    mock_client.get_volume_rank.return_value = [
        {
            "mksc_shrn_iscd": "069500",
            "hts_kor_isnm": "KODEX 200",
            "stck_prpr": "35000",
            "_tot_to": 50_000_000_000.0,
        },
        {
            "mksc_shrn_iscd": "005930",
            "hts_kor_isnm": "삼성전자",
            "stck_prpr": "73500",
            "stck_oprc": "0",
            "stck_hgpr": "0",
            "stck_lwpr": "0",
            "_tot_to": 80_000_000_000.0,
        },
    ]
    mock_client.get_current_price.return_value = {
        "stck_prpr": "73500",
        "stck_oprc": "70000",
        "stck_hgpr": "74000",
        "stck_lwpr": "69800",
        "prdy_ctrt": "5.00",
    }

    scanner = RealtimeLeaderScanner(
        client=mock_client,
        min_turnover_krw=1_000_000_000.0,
        min_gain_pct=3.0,
        max_mae_pct=-2.0,
    )
    promoted = scanner.scan_cycle()
    assert "069500" not in promoted
    assert "069500" not in scanner._discovered_symbols
    assert "005930" in promoted
    assert scanner._price_cache["005930"]["day_open"] == 70000.0
    mock_client.get_current_price.assert_called_once_with("005930", market_div="J")


def test_dynamic_leader_exit_respects_dynamic_step_up_metadata():
    """
    When use_dynamic_targets=True stores an ATR-adjusted step_up_pct in position.metadata,
    DynamicLeaderExit must respect position.metadata['step_up_pct'] instead of self.step_up_pct.
    """
    exit_policy = DynamicLeaderExit(
        step_up_pct=0.035,
        step_up_holding_bars=2,
    )
    pos = Position(
        symbol="005930",
        quantity=10,
        avg_price=10000.0,
        entry_dt=datetime(2026, 1, 5, 9, 2),
        metadata={"prior_atr20_pct": 0.14},
    )
    exit_policy.on_position_opened(pos)
    # 0.35 * 0.14 -> clipped eff_tp=0.045 -> step_up_pct=0.050 (> base 0.035)
    assert pos.metadata["step_up_pct"] == pytest.approx(0.050)

    # Bar hitting TP (high=10550 >= 10450) but closing at +4.8% (above base 3.5%, below dynamic 5.0%)
    # -> must take TP fallback instead of promoting with stale self.step_up_pct=3.5%
    bar_48pct = MinuteBar("005930", datetime(2026, 1, 5, 9, 4), 10300.0, 10550.0, 10250.0, 10480.0, 1000)
    sig = exit_policy.evaluate_exit(pos, bar_48pct)
    assert sig is not None
    assert sig.reason == "SMART_TP_CLOSE_1M_NORMAL"

    # Fresh position: bar closing at +5.5% (>= dynamic 5.0%) promotes to phase 3 with floor >= 10,500
    pos2 = Position(
        symbol="005930",
        quantity=10,
        avg_price=10000.0,
        entry_dt=datetime(2026, 1, 5, 9, 2),
        metadata={"prior_atr20_pct": 0.14},
    )
    exit_policy.on_position_opened(pos2)
    bar_55pct = MinuteBar("005930", datetime(2026, 1, 5, 9, 5), 10400.0, 10600.0, 10350.0, 10550.0, 1000)
    sig2 = exit_policy.evaluate_exit(pos2, bar_55pct)
    assert sig2 is None
    assert pos2.metadata.get("is_super_leader") is True
    assert pos2.stop_loss_price == round_to_krx_tick(10000.0 * 1.050, side="SELL", round_mode="floor")
    bar_next = MinuteBar("005930", datetime(2026, 1, 5, 9, 6), 10550.0, 10600.0, 10520.0, 10580.0, 1000)
    exit_policy.evaluate_exit(pos2, bar_next)
    assert pos2.stop_loss_price >= round_to_krx_tick(10000.0 * 1.050, side="SELL", round_mode="floor")


def test_daily_routine_subscribes_existing_positions():
    """
    DailyRoutineOrchestrator must proactively subscribe pre-existing held positions
    to the live WebSocket datafeed on Stage 2 startup.
    """
    from datetime import time as dtime
    from engine.daily_routine import DailyRoutineOrchestrator

    held = {"005930": Position(symbol="005930", quantity=10, avg_price=50000.0)}
    mock_feed = MagicMock()
    mock_feed.ws_client = MagicMock(connection_state="CONNECTED")
    mock_feed.active_symbols = ["005930"]
    mock_feed.last_tick_time = 1e12

    mock_broker = MagicMock()
    mock_broker._pending_orders = {}
    mock_broker.get_positions.side_effect = lambda: {} if mock_feed.subscribe_symbol.called else held
    mock_broker.get_balance.return_value = MagicMock(cash=5_000_000.0)
    mock_broker.client.get_kospi_gap_and_market_regime.return_value = {
        "ks_gap_pct": 0.2, "kq_gap_pct": 0.3,
        "ks_trend": 0.01, "kq_trend": 0.01,
        "is_bear_market": False, "dynamic_max_gap": 1.0, "gap_std": 0.5,
    }

    orchestrator = DailyRoutineOrchestrator(mode="mock", target_date="20260105")
    orchestrator.strategy = MagicMock(
        cutoff_time=dtime(9, 25, 0),
        max_daily_positions=1,
        current_slot_notional=5_000_000.0,
        is_daily_trading_completed=True,
    )

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 5, 9, 5, 0)

    with patch("engine.daily_routine.datetime", FixedDateTime), \
         patch("engine.daily_routine.KisLiveBroker", return_value=mock_broker), \
         patch("engine.daily_routine.KisRealtimeFeed", return_value=mock_feed), \
         patch("engine.daily_routine.RealtimeLeaderScanner", return_value=MagicMock()), \
         patch("engine.daily_routine.TradingEngine", return_value=MagicMock(context=MagicMock())), \
         patch("core.ws_client.KISWebSocketClient", return_value=MagicMock()), \
         patch("time.sleep", return_value=None):
        orchestrator.run(wait_for_market_open=False)

    mock_feed.subscribe_symbol.assert_called_with("005930")
    mock_feed.stop.assert_called_once()


def test_composite_tp_fallback_stop_not_clamped_to_open_and_partial_cancel_keeps_slot():
    """
    1) REALTIME_TP_FALLBACK_STOP must use target_price (bar.close), NOT min(bar.open, bar.close).
    2) Partial BUY fill + remainder CANCELLED must preserve _daily_trade_count and not rollback.
    """
    from engine.models import OrderEvent

    class StubScanner(IScanner):
        name = "stub_scanner"
        def filter_universe(self, current_date):
            return ["005930"]
        def on_bar(self, current_date, bar):
            return [bar.symbol]

    class StubEntry(IEntryRule):
        name = "stub_entry"
        def __init__(self):
            self.rollbacks = []
        def evaluate_entry(self, symbol, bar, tick=None, context_state=None):
            return None
        def rollback_entered_symbol(self, symbol):
            self.rollbacks.append(symbol)

    class StubExit(IExitPolicy):
        name = "stub_exit"
        def evaluate_exit(self, position, bar=None, tick=None):
            return ExitSignal(
                symbol=position.symbol,
                dt=bar.dt,
                order_type=OrderType.MARKET,
                target_price=bar.close,
                reason="REALTIME_TP_FALLBACK_STOP",
            )

    entry = StubEntry()
    strat = CompositeStrategy(
        scanner=StubScanner(),
        entry=entry,
        exit_policy=StubExit(),
        max_daily_positions=2,
        dynamic_slots=False,
        max_market_gap=None,
        bear_market_defense=False,
    )
    mock_ctx = MagicMock()
    pos = Position(symbol="005930", quantity=10, avg_price=50000.0)
    mock_ctx.get_position.return_value = pos

    # Green bar opening at 50100, closing at 51200 -> must liquidate at 51200, NOT 50100
    bar = MinuteBar("005930", datetime(2026, 1, 5, 9, 5), 50100.0, 51600.0, 50000.0, 51200.0, 1000, 1e9, 1e9)
    strat.on_bar(mock_ctx, bar)
    mock_ctx.liquidate.assert_called_once_with("005930", price=51200.0, tag="REALTIME_TP_FALLBACK_STOP")

    # Partial fill followed by cancel of remaining quantity: position still held -> keep slot count
    strat._daily_trade_count = 1
    cancel_ev = OrderEvent(
        order_id="od1", symbol="005930", side=OrderSide.BUY,
        status=OrderStatus.CANCELLED, filled_price=0.0, filled_quantity=0,
    )
    strat.on_order_event(mock_ctx, cancel_ev)
    assert strat._daily_trade_count == 1
    assert entry.rollbacks == []


def test_kis_live_broker_net_pnl_cash_reserve_sell_dedup_and_phase2_sell():
    """
    Verifies live OrderEvent.net_pnl computation, pending BUY cash reservation,
    duplicate SELL suppression, missing-symbol notice guard, and Phase-2 sell zero-fill safety.
    """
    import requests

    mock_client = MagicMock()
    mock_client.order_cash_buy.return_value = {"rt_cd": "0", "output": {"ODNO": "OD_B1"}}
    mock_client.order_cash_sell.return_value = {"rt_cd": "0", "output": {"ODNO": "OD_S1"}}
    broker = KisLiveBroker(mode="vts", client=mock_client)
    broker._initial_capital = 10_000_000.0
    events = []
    broker.register_event_listener(lambda e: events.append(e))

    # 1. Pending BUY order reserves cash in get_balance()
    broker.submit_order(Order(
        order_id="", symbol="005930", side=OrderSide.BUY,
        order_type=OrderType.LIMIT, quantity=100, price=50000.0,
    ))
    assert broker.get_balance().cash < 5_000_000.0

    # 2. Fill BUY 100 @ 50,000 -> Sell 100 @ 55,000 emits positive net_pnl
    broker.handle_execution_notice({
        "order_no": "OD_B1", "symbol": "005930", "side": "BUY",
        "qty": 100, "price": 50000.0, "cntg_yn": "2",
    })
    broker.submit_order(Order(
        order_id="", symbol="005930", side=OrderSide.SELL,
        order_type=OrderType.MARKET, quantity=100, tag="HARD_TIMEOUT_1521",
    ))
    # Duplicate SELL while OD_S1 is pending must be suppressed
    dup_id = broker.submit_order(Order(
        order_id="", symbol="005930", side=OrderSide.SELL,
        order_type=OrderType.MARKET, quantity=100, tag="HARD_TIMEOUT_1521",
    ))
    assert dup_id == "OD_S1"
    assert mock_client.order_cash_sell.call_count == 1

    broker.handle_execution_notice({
        "order_no": "OD_S1", "symbol": "005930", "side": "SELL",
        "qty": 100, "price": 55000.0, "cntg_yn": "2",
    })
    sell_ev = [e for e in events if e.side == OrderSide.SELL and e.status == OrderStatus.FILLED][-1]
    assert sell_ev.net_pnl > 400_000.0

    # 3. Missing symbol execution notice must not create "" position
    broker.handle_execution_notice({
        "order_no": "UNKNOWN", "symbol": "", "side": "BUY", "qty": 10, "price": 10000.0, "cntg_yn": "2"
    })
    assert "" not in broker.get_positions()

    # 4. Phase-2 SELL timeout with ccld_qty=0 must keep position (mark PENDING, not FILLED)
    broker.handle_execution_notice({
        "order_no": "OD_B2", "symbol": "000660", "side": "BUY", "qty": 10, "price": 100000.0, "cntg_yn": "2"
    })
    mock_client.order_cash_sell.side_effect = requests.exceptions.Timeout("timeout")
    mock_client.inquire_daily_ccld.return_value = [
        {"odno": "OD_S2", "pdno": "000660", "ord_qty": "10", "ccld_qty": "0", "ccld_unpr": "0", "sll_buy_dvsn_cd": "01"}
    ]
    ret = broker.submit_order(Order(
        order_id="", symbol="000660", side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=10
    ))
    assert ret == "OD_S2"
    assert broker.get_position("000660") is not None
    assert broker.get_position("000660").quantity == 10


def test_open_window_scanner_prev_turnover_no_lookahead(tmp_path):
    """OpenWindowScanner._load_prev_day_cache must use t-1 turnover (shift(1)), not day t EOD turnover."""
    import strategy.scanners.open_window_scanner as ows_mod
    from strategy.scanners.open_window_scanner import OpenWindowScanner

    daily_path = tmp_path / "daily.parquet"
    pd.DataFrame([
        {"date": date(2026, 1, 2), "symbol": "005930", "prev_close": 49000.0, "turnover": 1_000_000_000.0},
        {"date": date(2026, 1, 5), "symbol": "005930", "prev_close": 50000.0, "turnover": 99_000_000_000.0},
    ]).to_parquet(daily_path, index=False)

    orig_cache = ows_mod._GLOBAL_PREV_DATA_CACHE
    try:
        ows_mod._GLOBAL_PREV_DATA_CACHE = None
        scanner = OpenWindowScanner(min_prev_day_turnover=50_000_000_000.0, daily_file=str(daily_path))
        # On 2026-01-05, t-1 turnover is 1B (< 50B), whereas day t EOD is 99B -> must NOT pass!
        qualified = scanner.filter_universe(date(2026, 1, 5))
        assert qualified == []
    finally:
        ows_mod._GLOBAL_PREV_DATA_CACHE = orig_cache


def test_dynamic_leader_exit_registry_disables_dynamic_targets():
    """build_strategy(..., exit_name='dynamic_leader', dynamic=False) must set use_dynamic_targets=False."""
    from strategy.registry import build_strategy

    strat = build_strategy(exit_name="dynamic_leader", dynamic=False)
    assert strat.exit_policy.use_dynamic_targets is False
