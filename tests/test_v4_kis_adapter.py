"""
Unit Tests for Korea Market v4 KIS Adapters & Live CLI (Zero Network Calls).
Tests `KisRealtimeFeed`, `KisLiveBrokerAdapter`, `KisConditionSearchAdapter`,
and `v4/live_cli.py` using mocked `KISRestClient` and WebSocket objects.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

from config.settings import Settings
from core.rate_limiter import RateLimiter
from v4.adapters.kis.broker import KisLiveBrokerAdapter
from v4.adapters.kis.condition_search import KisConditionSearchAdapter
from v4.adapters.kis.feed import MAX_WS_SLOTS, KisRealtimeFeed
from v4.adapters.kis.venue import KisVenueResolver
from v4.models import OrderRequest, OrderSide, OrderStatus, OrderType, Position, TickEvent


def _make_tick_record(sym: str, hhmmss: str, price: str, open_p: str = "10000", low_p: str = "9900") -> str:
    fields = ["0"] * 46
    fields[0], fields[1], fields[2], fields[6], fields[7], fields[9] = sym, hhmmss, price, price, open_p, low_p
    fields[10], fields[11], fields[12], fields[13], fields[14] = price, price, "100", "5000", "50000000"
    return "^".join(fields)


def test_feed_routing_and_40_slot_protection():
    cfg = Settings(env="real")
    broker = MagicMock()
    broker.get_positions.return_value = {
        "000001": Position(symbol="000001", quantity=10, avg_price=10000.0, current_price=10000.0)
    }
    pend = MagicMock()
    pend.symbol = "000002"
    broker.get_pending_orders.return_value = [pend]

    feed = KisRealtimeFeed(cfg=cfg, broker=broker, nxt_symbols={"005930"})
    assert feed.poll_interval_sec == 0.25

    async def _run():
        assert await feed.subscribe("005930") is True
        assert feed._subscriptions["005930"] == "H0UNCNT0"
        assert await feed.subscribe("999990") is True
        assert feed._subscriptions["999990"] == "H0STCNT0"

        # Fill up to 40 slots with protected symbols + unprotected symbols
        await feed.subscribe("000001")
        await feed.subscribe("000002")
        for i in range(10, 46):
            await feed.subscribe(f"1000{i:02d}")
        assert len(feed._subscriptions) == MAX_WS_SLOTS

        # Next subscribe must evict an unprotected symbol, never 000001 or 000002
        assert await feed.subscribe("200001") is True
        assert "000001" in feed._subscriptions
        assert "000002" in feed._subscriptions
        assert len(feed._subscriptions) == MAX_WS_SLOTS

        # When all 40 slots are protected, new subscribe must be rejected
        broker.get_positions.return_value = {
            s: Position(symbol=s, quantity=1, avg_price=1000.0, current_price=1000.0)
            for s in feed._subscriptions
        }
        assert await feed.subscribe("300001") is False

    asyncio.run(_run())


def test_feed_multi_record_and_premarket_isolation():
    cfg = Settings(env="real")
    feed = KisRealtimeFeed(cfg=cfg, nxt_symbols={"005930"})
    r1 = _make_tick_record("005930", "090105", "50100", open_p="49000", low_p="48500")
    r2 = _make_tick_record("005930", "090106", "50200", open_p="49000", low_p="48500")
    raw_un = f"0|H0UNCNT0|2|{r1}^{r2}"
    raw_krx = f"0|H0STCNT0|1|{_make_tick_record('999990', '090107', '20500', open_p='20000', low_p='19900')}"

    async def _run():
        await feed._handle_raw_message(raw_un)
        await feed._handle_raw_message(raw_krx)
        events = []
        while not feed._queue.empty():
            events.append(feed._queue.get_nowait())
        ticks = [e for e in events if isinstance(e, TickEvent)]
        assert len(ticks) == 3
        # H0UNCNT0 must isolate day_open / krx_day_open / krx_day_low (08:00~08:50 protection)
        assert ticks[0].price == 50100.0 and ticks[1].price == 50200.0
        assert ticks[0].day_open == 0.0 and ticks[0].krx_day_open == 0.0 and ticks[0].krx_day_low == 0.0
        assert ticks[0].raw_day_open == 49000.0 and ticks[0].raw_day_low == 48500.0
        # H0STCNT0 populates KRX day_open / krx_day_low
        assert ticks[2].day_open == 20000.0 and ticks[2].krx_day_low == 19900.0

    asyncio.run(_run())


def test_broker_tick_rounding_no_retry_and_lockout():
    client = MagicMock()
    client.order_cash_buy.return_value = {"rt_cd": "1", "msg1": "INSUFFICIENT_FUNDS"}
    broker = KisLiveBrokerAdapter(cfg=Settings(env="real"), client=client)

    async def _run():
        req = OrderRequest(symbol="005930", side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=10, price=50050.0)
        for _ in range(3):
            ev = await broker.submit_order(req)
            assert ev.status == OrderStatus.REJECTED
        # No retry on rt_cd == "1" -> exactly 3 REST calls
        assert client.order_cash_buy.call_count == 3
        # 50050 BUY rounds down to 50000 KRX tick (100-won tick)
        assert client.order_cash_buy.call_args[0][2] == 50000

        # 4th BUY hits >= 3 consecutive rejection lockout without calling REST
        ev4 = await broker.submit_order(req)
        assert ev4.status == OrderStatus.REJECTED
        assert ev4.message == "BUY_LOCKOUT_CONSECUTIVE_REJECTIONS"
        assert client.order_cash_buy.call_count == 3

    asyncio.run(_run())


def test_broker_phase2_timeout_recovery():
    client = MagicMock()
    client.order_cash_buy.side_effect = requests.exceptions.Timeout("read timed out")
    client.inquire_daily_ccld.return_value = [
        {
            "odno": "ORD_P2",
            "pdno": "005930",
            "ord_qty": "10",
            "sll_buy_dvsn_cd": "02",
            "tot_ccld_qty": "10",
            "avg_prvs": "50000",
        }
    ]
    broker = KisLiveBrokerAdapter(cfg=Settings(env="real"), client=client)

    async def _run():
        with patch("v4.adapters.kis.broker_orders.asyncio.sleep", return_value=None):
            ev = await broker.submit_order(
                OrderRequest(symbol="005930", side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=10, price=50000.0)
            )
        assert ev.status == OrderStatus.FILLED
        assert ev.order_id == "ORD_P2"
        assert ev.filled_quantity == 10
        assert broker.get_position("005930").quantity == 10

    asyncio.run(_run())


def test_broker_ws_dedup_partial_sell_pnl_and_sync():
    client = MagicMock()
    client.order_cash_buy.return_value = {"rt_cd": "0", "output": {"ODNO": "B1"}}
    client.order_cash_sell.return_value = {"rt_cd": "0", "output": {"ODNO": "S1"}}
    client.order_cancel.return_value = {"rt_cd": "1", "msg1": "ALREADY_FILLED"}
    client.inquire_daily_ccld.return_value = [{"odno": "B1", "tot_ccld_qty": "10", "avg_prvs": "10000"}]
    broker = KisLiveBrokerAdapter(cfg=Settings(env="real"), client=client, initial_capital=1_000_000.0)

    events = []
    broker.on_execution_event(events.append)

    async def _run():
        await broker.submit_order(
            OrderRequest(symbol="005930", side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=10, price=10000.0)
        )
        # WS execution notice fills B1 completely
        broker.handle_execution_notice({"order_no": "B1", "symbol": "005930", "side": "BUY", "qty": 10, "price": 10000.0})
        assert broker.get_position("005930").quantity == 10

        # cancel_order after WS full fill must NOT double-apply fill
        cancelled = await broker.cancel_order("B1", symbol="005930")
        assert cancelled is False
        assert broker.get_position("005930").quantity == 10

        # Partial SELL fills accumulate net_pnl onto final FILLED OrderEvent
        await broker.submit_order(
            OrderRequest(symbol="005930", side=OrderSide.SELL, order_type=OrderType.LIMIT, quantity=10, price=10500.0)
        )
        broker.handle_execution_notice({"order_no": "S1", "symbol": "005930", "side": "SELL", "qty": 4, "price": 10500.0})
        assert events[-1].status == OrderStatus.PARTIALLY_FILLED
        broker.handle_execution_notice({"order_no": "S1", "symbol": "005930", "side": "SELL", "qty": 6, "price": 10500.0})
        final_ev = events[-1]
        assert final_ev.status == OrderStatus.FILLED
        assert final_ev.cum_filled_quantity == 10
        assert final_ev.net_pnl == pytest.approx(sum(t.net_pnl for t in broker.get_completed_trades()))
        assert final_ev.net_pnl > 0.0

        # sync_open_orders reconciles holdings & explicit 0 cash
        client.get_balance.return_value = {
            "summary": {"dnca_tot_amt": "0", "tot_evlu_amt": "500000"},
            "holdings": [{"pdno": "000660", "hldg_qty": "5", "pchs_avg_pric": "100000", "prpr": "100000"}],
        }
        client.inquire_daily_ccld.return_value = [
            {"odno": "P1", "pdno": "000660", "rmn_qty": "2", "ord_qty": "2", "ord_unpr": "99000", "sll_buy_dvsn_cd": "02"}
        ]
        open_evs = await broker.sync_open_orders()
        assert broker.get_balance().cash == 0.0
        assert broker.get_position("000660").quantity == 5
        assert len(open_evs) == 1 and open_evs[0].order_id == "P1"

    asyncio.run(_run())


def test_condition_search_turnover_and_un_j_isolation():
    client = MagicMock()
    client.cfg = Settings(env="real")
    client.get_condition_search_result.return_value = [
        {"code": "005930", "name": "삼성전자", "open": "50000", "low": "49800", "price": "51000", "trade_amt": "1500000"}
    ]

    def _price_side_effect(sym: str, market_div: str):
        if market_div == "UN":
            return {"stck_oprc": "48000", "stck_lwpr": "47500", "stck_hgpr": "51500",
                    "stck_prpr": "51000", "acml_tr_pbmn": "3000000000", "acml_vol": "60000"}
        return {"stck_oprc": "50000", "stck_lwpr": "49800", "stck_hgpr": "51500",
                "stck_prpr": "51000", "acml_tr_pbmn": "2000000000", "acml_vol": "40000", "stck_prdy_clpr": "49500"}

    client.get_current_price.side_effect = _price_side_effect
    adapter = KisConditionSearchAdapter(
        client=client, nxt_symbols={"005930"}, rate_limiter=RateLimiter(rate=18.0, capacity=18.0)
    )

    async def _run():
        now_dt = datetime(2026, 11, 16, 9, 5, 0)
        cands = await adapter.poll_candidates(now_dt)
        assert len(cands) == 1
        assert cands[0].krx_cum_turnover == 1_500_000_000.0  # trade_amt * 1000.0

        snap = await adapter.get_intraday_snapshot("005930", now_dt)
        assert snap is not None
        assert snap.cum_turnover == 3_000_000_000.0  # from UN
        assert snap.day_open == 50000.0 and snap.day_low == 49800.0  # strictly from J

        # UN exception fallback to J
        client.get_current_price.side_effect = lambda sym, m: (
            (_ for _ in ()).throw(RuntimeError("UN failed")) if m == "UN" else _price_side_effect(sym, "J")
        )
        snap_fb = await adapter.get_intraday_snapshot("005930", now_dt)
        assert snap_fb is not None
        assert snap_fb.cum_turnover == 2_000_000_000.0
        assert snap_fb.day_open == 50000.0

    asyncio.run(_run())


def test_v4_live_cli_safety_gate():
    from v4.live_cli import cmd_v4_live

    args = argparse.Namespace(yes=False, capital=10_000_000.0)
    with pytest.raises(SystemExit) as exc:
        cmd_v4_live(args)
    assert exc.value.code == 1


def test_venue_resolver_and_j_first_minute_bars():
    venue = KisVenueResolver({"005930"})
    assert venue.ws_tr_id("005930") == "H0UNCNT0" and venue.ws_tr_id("999990") == "H0STCNT0"
    assert venue.order_exchange("005930") == "SOR" and venue.order_exchange("999990") == "KRX"
    assert venue.minute_bar_divs() == ("J", "UN") and KisVenueResolver(None).is_nxt_or_none("005930") is None

    client = MagicMock()
    client.cfg = Settings(env="real")
    calls = []

    def _bars_side_effect(sym: str, to_hour: str, m_div: str):
        calls.append(m_div)
        return [] if m_div == "J" else [{"stck_cntg_hour": "090500", "cntg_vol": "10", "stck_prpr": "50000"}]

    client.get_today_minute_bars.side_effect = _bars_side_effect
    adapter = KisConditionSearchAdapter(client=client, venue=venue, rate_limiter=RateLimiter(rate=18.0, capacity=18.0))
    bars = asyncio.run(adapter.get_recent_minute_bars("005930", datetime(2026, 11, 16, 9, 10, 0)))
    assert calls == ["J", "UN"] and len(bars) == 1

    client.order_cancel.return_value = {"rt_cd": "0"}
    broker = KisLiveBrokerAdapter(cfg=Settings(env="real"), client=client, venue=venue)
    assert asyncio.run(broker.cancel_order("ORD1", symbol="005930")) is True
    assert client.order_cancel.call_args.kwargs.get("symbol") == "005930"


def test_broker_sor_fallback_to_krx():
    client = MagicMock()
    client.order_cash_buy.side_effect = [
        {"rt_cd": "1", "msg1": "SOR 주문불가 종목"},
        {"rt_cd": "0", "output": {"ODNO": "KRX1"}},
    ]
    broker = KisLiveBrokerAdapter(cfg=Settings(env="real"), client=client, venue=KisVenueResolver({"005930"}))
    ev = asyncio.run(broker.submit_order(
        OrderRequest(symbol="005930", side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=1, price=50000.0)
    ))
    assert ev.status == OrderStatus.PENDING and ev.order_id == "KRX1"
    assert [c[0][4] for c in client.order_cash_buy.call_args_list] == ["SOR", "KRX"]
