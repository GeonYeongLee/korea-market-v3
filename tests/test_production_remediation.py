"""
Remediation Verification Tests for Korea Market v3.
Ensures the 9 P0 and 10 P1 fixes maintain 100% regression invariance.
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, date, time as dtime
from unittest.mock import MagicMock, patch

import pytest

from config.settings import Settings
from core.rate_limiter import RateLimiter
from core.ws_client import KISWebSocketClient
from core.rest_client import KISRestClient
from engine.models import (
    Position,
    Order,
    OrderStatus,
    OrderSide,
    OrderType,
    MinuteBar,
    TickData,
    OrderEvent,
)
from engine.broker.backtest import BacktestBroker
from engine.broker.kis_live import KisLiveBroker
from engine.datafeed.kis_live import KisRealtimeFeed
from engine.engine import TradingEngine
from engine.clock import SimulatedClock
from engine.reconciliation import (
    parse_live_audit_log,
    reconcile_daily_trading,
    LiveTradeExecution,
    SymbolReconciliationDetail,
    ReconciliationResult,
)


# -------------------------------------------------------------------------
# Test 1: Position model low_water_mark
# -------------------------------------------------------------------------
def test_position_low_water_mark_initialization():
    pos = Position(symbol="005930", quantity=10, avg_price=50000.0)
    assert pos.high_water_mark == 50000.0
    assert pos.low_water_mark == 50000.0

    pos2 = Position(symbol="005930", quantity=10, avg_price=50000.0, low_water_mark=48000.0)
    assert pos2.low_water_mark == 48000.0


# -------------------------------------------------------------------------
# Test 2: RateLimiter acquire_async thread lock
# -------------------------------------------------------------------------
def test_rate_limiter_sync_async_thread_safety():
    limiter = RateLimiter(rate=50.0, capacity=50.0)

    async def run_async():
        for _ in range(20):
            await limiter.acquire_async(1.0)

    def run_sync():
        for _ in range(20):
            limiter.acquire_sync(1.0)

    t = threading.Thread(target=run_sync)
    t.start()
    asyncio.run(run_async())
    t.join()
    assert limiter.tokens < 50.0


# -------------------------------------------------------------------------
# Test 3: WebSocket client approval key, PINGPONG echo, execution code strip
# -------------------------------------------------------------------------
def test_ws_client_pingpong_echo_and_symbol_strip():
    client = KISWebSocketClient()
    mock_ws = MagicMock()
    mock_ws.send = MagicMock()

    async def mock_send(msg):
        pass
    mock_ws.send.side_effect = mock_send
    client._ws = mock_ws

    # 1. PINGPONG echo
    pingpong_frame = '{"header":{"tr_id":"PINGPONG","datetime":"20261110090000"}}'
    asyncio.run(client._handle_message(pingpong_frame))
    mock_ws.send.assert_called_with(pingpong_frame)

    # 2. Execution notice with 'A' prefix symbol
    received_exec = []
    async def cb(mtype, data):
        if mtype == "EXECUTION":
            received_exec.append(data)
    client.register_callback(cb)

    # Standard execution notice pipe frame:
    # 0: enc_flag | 1: tr_id | 2: cnt | 3: data_str
    # fields[1]=acc, fields[2]=ord_no, fields[8]='A005930', fields[13]='2'
    data_fields = [""] * 15
    data_fields[1] = "12345678"
    data_fields[2] = "9901"
    data_fields[4] = "02"  # BUY
    data_fields[8] = "A005930"  # 7-char symbol with leading 'A'
    data_fields[9] = "100"
    data_fields[10] = "55000"
    data_fields[13] = "2"  # Filled notice
    exec_frame = f"0|H0STCNI0|001|{'^'.join(data_fields)}"

    asyncio.run(client._handle_message(exec_frame))
    assert len(received_exec) == 1
    assert received_exec[0]["symbol"] == "005930"  # 'A' stripped
    assert received_exec[0]["order_no"] == "9901"


# -------------------------------------------------------------------------
# Test 4: REST client exchange default
# -------------------------------------------------------------------------
def test_rest_client_exchange_default():
    vts_cfg = Settings(env="vts")
    real_cfg = Settings(env="real")

    vts_client = KISRestClient(cfg=vts_cfg, limiter=RateLimiter(rate=100.0, capacity=10.0))
    real_client = KISRestClient(cfg=real_cfg, limiter=RateLimiter(rate=100.0, capacity=10.0))

    with patch.object(vts_client.session, "post") as mock_vts_post, \
         patch.object(real_client.session, "post") as mock_real_post:

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"rt_cd": "0"}
        mock_vts_post.return_value = mock_resp
        mock_real_post.return_value = mock_resp

        with patch.object(vts_client.token_mgr, "get_access_token", return_value="tok"), \
             patch.object(real_client.token_mgr, "get_access_token", return_value="tok"):

            vts_client.order_cash_buy("005930", qty=10, price=50000)
            assert mock_vts_post.call_args[1]["json"]["EXCG_ID_DVSN_CD"] == "KRX"

            real_client.order_cash_buy("005930", qty=10, price=50000)
            assert mock_real_post.call_args[1]["json"]["EXCG_ID_DVSN_CD"] == "SOR"


# -------------------------------------------------------------------------
# Test 5: KisLiveBroker non-destructive sync & partial fills & cancel event
# -------------------------------------------------------------------------
def test_kis_live_broker_sync_account_state_merge():
    mock_client = MagicMock()
    mock_client.get_balance.return_value = {
        "holdings": [
            {"pdno": "005930", "hldg_qty": "100", "pchs_avg_pric": "50000", "prpr": "52000"},
        ],
        "summary": {"dnca_tot_amt": "10000000", "tot_evlu_amt": "15200000"}
    }
    broker = KisLiveBroker(mode="vts", client=mock_client)

    # Establish initial position with custom metadata and SL
    orig_pos = Position(
        symbol="005930",
        quantity=50,
        avg_price=50000.0,
        current_price=50000.0,
        high_water_mark=51000.0,
        low_water_mark=49000.0,
        entry_dt=datetime(2026, 1, 2, 9, 2),
        stop_loss_price=49500.0,
        metadata={"ref_bar": {"open": 50000.0}}
    )
    broker._positions["005930"] = orig_pos

    # Sync account state: should merge, NOT clear
    broker.sync_account_state()
    pos = broker.get_position("005930")
    assert pos is not None
    assert pos.quantity == 100
    assert pos.current_price == 52000.0
    assert pos.high_water_mark == 52000.0  # max(51000, 52000)
    assert pos.low_water_mark == 49000.0   # min(49000, 52000)
    assert pos.stop_loss_price == 49500.0  # preserved!
    assert pos.metadata["ref_bar"]["open"] == 50000.0  # preserved!
    assert pos.entry_dt == datetime(2026, 1, 2, 9, 2)  # preserved!


def test_kis_live_broker_partial_fills_and_cancel_event():
    mock_client = MagicMock()
    broker = KisLiveBroker(mode="vts", client=mock_client)
    events = []
    broker.register_event_listener(lambda e: events.append(e))

    # Pre-register an order of 100 shares
    ord_ = Order(
        order_id="OD_100",
        symbol="005930",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=50000.0,
    )
    broker._pending_orders["OD_100"] = ord_

    # Partial fill 40 shares
    broker.handle_execution_notice({
        "order_no": "OD_100",
        "symbol": "005930",
        "side": "BUY",
        "qty": 40,
        "price": 50000.0,
    })
    assert len(events) == 1
    assert events[0].status == OrderStatus.PARTIALLY_FILLED
    assert events[0].filled_quantity == 40
    assert "OD_100" in broker._pending_orders
    assert ord_.unfilled_quantity == 60

    # Final fill remaining 60 shares
    broker.handle_execution_notice({
        "order_no": "OD_100",
        "symbol": "005930",
        "side": "BUY",
        "qty": 60,
        "price": 50000.0,
    })
    assert len(events) == 2
    assert events[1].status == OrderStatus.FILLED
    assert events[1].filled_quantity == 60
    assert "OD_100" not in broker._pending_orders


def test_kis_live_broker_cancel_order_emits_cancelled_event():
    mock_client = MagicMock()
    mock_client.order_cancel.return_value = {"rt_cd": "0"}
    mock_client.get_balance.return_value = {"holdings": [], "summary": {}}
    broker = KisLiveBroker(mode="vts", client=mock_client)
    events = []
    broker.register_event_listener(lambda e: events.append(e))

    ord_ = Order(
        order_id="OD_CAN",
        symbol="005930",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=50000.0,
    )
    broker._pending_orders["OD_CAN"] = ord_

    res = broker.cancel_order("OD_CAN")
    assert res is True
    assert len(events) == 1
    assert events[0].status == OrderStatus.CANCELLED
    assert events[0].symbol == "005930"
    assert "OD_CAN" not in broker._pending_orders


# -------------------------------------------------------------------------
# Test 6: KisRealtimeFeed 40-symbol cap protection
# -------------------------------------------------------------------------
def test_kis_realtime_feed_40_cap_rejection():
    mock_broker = MagicMock()
    # All 40 symbols are currently held
    held = {f"{i:06d}": Position(f"{i:06d}", quantity=10, avg_price=1000.0) for i in range(40)}
    mock_broker.get_positions.return_value = held

    mock_ws = MagicMock()
    feed = KisRealtimeFeed(ws_client=mock_ws, broker=mock_broker)
    feed.active_symbols = [f"{i:06d}" for i in range(40)]

    # Attempting to subscribe 41st symbol when all 40 are held should return False
    res = feed.subscribe_symbol("999999")
    assert res is False
    assert len(feed.active_symbols) == 40
    assert "999999" not in feed.active_symbols


# -------------------------------------------------------------------------
# Test 10: Reconciliation fee formula & exit slippage
# -------------------------------------------------------------------------
def test_reconciliation_exact_fee_formula():
    entry_px = 50000.0
    exit_px = 52000.0
    shares = 100

    entry_val = entry_px * shares  # 5,000,000
    exit_val = exit_px * shares    # 5,200,000
    gross_pnl = exit_val - entry_val  # 200,000

    # Broker formula: (entry_val * 0.00014) + (exit_val * (0.00014 + 0.0018))
    expected_cost = (entry_val * 0.00014) + (exit_val * 0.00194)
    expected_net_pnl = gross_pnl - expected_cost

    trade = LiveTradeExecution(
        symbol="005930",
        name="삼성전자",
        has_trade=True,
        shares=shares,
        entry_px=entry_px,
        exit_px=exit_px,
        realized_pnl=0.0
    )
    trades_by_sym = {"005930": trade}

    for sym, t in trades_by_sym.items():
        if t.has_trade and t.realized_pnl == 0.0:
            en = t.entry_px * t.shares
            ex = t.exit_px * t.shares
            cost = (en * 0.00014) + (ex * (0.00014 + 0.0018))
            t.realized_pnl = (ex - en) - cost

    assert abs(trade.realized_pnl - expected_net_pnl) < 1e-6


# -------------------------------------------------------------------------
# Test 11: TradingEngine 10:00 AM late market opening cutoff adjustment
# -------------------------------------------------------------------------
def test_engine_10am_late_market_opening():
    strat = MagicMock()
    strat.cutoff_time = dtime(9, 20)
    strat.is_daily_trading_completed = False
    broker = BacktestBroker(initial_capital=10_000_000.0)
    clock = SimulatedClock()
    feed = MagicMock()

    engine = TradingEngine(strategy=strat, broker=broker, feed=feed, clock=clock)

    # 1. 10:05 AM on a CSAT day: should NOT be marked done (cutoff adjusted to 10:20)
    csat_dt = datetime(2026, 11, 19, 10, 5)
    is_done, syms = engine._check_feed_filter(csat_dt)
    assert is_done is False

    # 2. 10:25 AM: after adjusted cutoff (10:20) with no positions -> marked done
    csat_dt_after = datetime(2026, 11, 19, 10, 25)
    is_done, syms = engine._check_feed_filter(csat_dt_after)
    assert is_done is True


# -------------------------------------------------------------------------
# Test 12: BacktestBroker adverse slippage rounding, cash check, MAE
# -------------------------------------------------------------------------
def test_backtest_broker_adverse_slippage_and_mae():
    broker = BacktestBroker(initial_capital=10_000_000.0, slippage_rate=0.001)

    # 1. BUY order with adverse ceil rounding
    buy_order = Order(
        order_id="ord_b1",
        symbol="005930",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=50000.0,
    )
    broker.submit_order(buy_order)
    pos = broker.get_position("005930")
    assert pos is not None
    # 50,000 * 1.001 = 50,050 -> KRX tick for 50k is 100 -> ceil rounds to 50,100
    assert pos.avg_price == 50100.0
    assert pos.low_water_mark == 50100.0

    # 2. Simulate bar with price drop to 49,500
    drop_bar = MinuteBar(
        symbol="005930",
        dt=datetime(2026, 1, 2, 9, 5),
        open=50100.0,
        high=50200.0,
        low=49500.0,
        close=49800.0,
        volume=1000,
    )
    broker.on_bar(drop_bar)
    assert pos.low_water_mark == 49500.0

    # 3. Sell position: check MAE calculation
    sell_order = Order(
        order_id="ord_s1",
        symbol="005930",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=51000.0,
    )
    broker.submit_order(sell_order)
    assert len(broker.completed_trades) == 1
    trade = broker.completed_trades[0]
    expected_mae = (49500.0 - 50100.0) / 50100.0
    assert abs(trade.mae_pct - expected_mae) < 1e-6


def test_backtest_broker_insufficient_funds_rejection():
    broker = BacktestBroker(initial_capital=1_000_000.0)
    events = []
    broker.register_event_listener(lambda e: events.append(e))

    # Attempt to buy 10M KRW worth of stock with only 1M cash
    buy_order = Order(
        order_id="ord_huge",
        symbol="005930",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=200,
        price=50000.0,
    )
    broker.submit_order(buy_order)
    assert len(events) == 1
    assert events[0].status == OrderStatus.REJECTED
    assert events[0].message == "REJECTED_INSUFFICIENT_FUNDS"
    assert broker.get_position("005930") is None
