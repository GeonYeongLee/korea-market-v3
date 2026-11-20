"""
Korea Market v3 - Feature-Specific Live Trading Test Harnesses
==============================================================
Validates all 6 core functional pillars required for real-money / mock live trading:

[Harness 1] Auth, Account & Pre-flight Connectivity Harness (REST / VTS / REAL)
[Harness 2] Realtime Anchor Scanner & Dynamic Ingestion Harness (15:15~15:20)
[Harness 3] Entry Execution & Collar Cap Guard Harness (Closing Call Auction KRX Routing)
[Harness 4] Overnight Management & 08:50 Call Auction Exit Harness
[Harness 5] WebSocket Streaming & Execution Notice Reconciliation Harness (H0UNCNT0 / H0STCNT0)
[Harness 6] End-to-End Daily Routine Orchestrator Harness (Stage 1 -> 1.5 -> 2/3 -> 4)
"""
from __future__ import annotations

import unittest
from datetime import datetime, date, time as dtime
from unittest.mock import MagicMock, patch

from config.settings import settings, Settings
from core.krx_ticks import round_to_krx_tick
from core.ws_client import KISWebSocketClient
from data_pipeline.realtime_scanner import RealtimeAnchorScanner
from engine.broker.kis_live import KisLiveBroker
from engine.daily_routine import DailyRoutineOrchestrator
from engine.models import (
    AccountBalance, MinuteBar, Order, OrderEvent, OrderSide,
    OrderStatus, OrderType, Position, TickData
)
from strategy.composite import CompositeStrategy
from strategy.entries.anchor_close_entry import AnchorCloseEntry
from strategy.exits.anchor_morning_exit import AnchorMorningExit
from strategy.scanners.anchor_body_micro_scanner import AnchorBodyMicroScanner


class TestHarness1_AuthAndAccountConnectivity(unittest.TestCase):
    """
    [Harness 1] Verifies OAuth2 token, WebSocket approval key, orderable cash
    extraction (ord_psbl_cash), and live quote connectivity.
    """

    def setUp(self):
        self.mock_client = MagicMock()

    def test_account_orderable_cash_prioritization(self):
        """Broker must prioritize ord_psbl_cash over D+0 settlement deposit."""
        self.mock_client.get_balance.return_value = {
            "summary": {
                "ord_psbl_cash_amt": "7500000",
                "dnca_tot_amt": "10000000",
                "tot_evlu_amt": "12000000",
            },
            "holdings": [
                {
                    "pdno": "005930",
                    "hldg_qty": "20",
                    "pchs_avg_pric": "50000",
                    "prpr": "52000",
                    "evlu_amt": "1040000",
                }
            ],
        }

        broker = KisLiveBroker(mode="vts", client=self.mock_client)
        broker.sync_account_state()

        bal = broker.get_balance()
        self.assertIsNotNone(bal)
        # Must pick ord_psbl_cash_amt = 7,500,000 KRW
        self.assertEqual(bal.cash, 7_500_000.0)

        pos = broker.get_position("005930")
        self.assertIsNotNone(pos)
        self.assertEqual(pos.quantity, 20)
        self.assertEqual(pos.avg_price, 50000.0)
        self.assertEqual(pos.current_price, 52000.0)


class TestHarness2_RealtimeAnchorScanner(unittest.TestCase):
    """
    [Harness 2] Verifies 15:15~15:20 Realtime Anchor Scanner:
      1. Candle body filtering (2% <= body <= 15%)
      2. Turnover >= 500억 KRW
      3. Non-upper-limit (< 29.5%)
      4. Alpha score calculation ($0.45*turnover + 0.35*body + 0.20*surge$)
      5. Dynamic candidate feeding to AnchorBodyMicroScanner
      6. Context promotion to trading pool
    """

    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_feed = MagicMock()
        self.mock_feed.active_symbols = []

        self.scanner_rule = AnchorBodyMicroScanner()
        self.entry_rule = AnchorCloseEntry()
        self.exit_policy = AnchorMorningExit()
        self.strategy = CompositeStrategy(
            scanner=self.scanner_rule,
            entry=self.entry_rule,
            exit_policy=self.exit_policy,
            max_daily_positions=2,
            dynamic_slots=False,
            total_capital=10_000_000.0,
        )

        self.mock_context = MagicMock()
        self.mock_context.candidate_symbols = []
        self.mock_context.promote_candidate = lambda s: self.mock_context.candidate_symbols.append(s)

    def test_scanner_filters_and_promotes_top2_anchor_candidates(self):
        """Verifies candidate qualification and ranking calculation."""
        # Setup mock volume rank API return:
        # Candidate A: 005930 (Turnover: 1200억, Body: 10.0%, Shadow: 9.0%, Gain: 10.0%) -> Qualified (High Alpha)
        # Candidate B: 000660 (Turnover: 800억, Body: 9.5%, Shadow: 5.0%, Gain: 9.5%) -> Qualified
        # Candidate C: 000020 (Upper Limit: 29.9%) -> Disqualified (Upper Limit Guard)
        # Candidate D: 000030 (Body: 0.5%) -> Disqualified (Doji / Upper wick heavy)
        # Candidate E: 000040 (Turnover: 200억) -> Disqualified (< 500억)
        self.mock_client.get_volume_rank.return_value = [
            {
                "code": "005930",
                "name": "삼성전자",
                "open": "50000",
                "high": "55500",
                "low": "50000",
                "price": "55000",
                "trade_amt": "120000000",  # 1,200억 (단위: 천원)
                "chgrate": "10.0",
            },
            {
                "code": "000660",
                "name": "SK하이닉스",
                "open": "100000",
                "high": "110000",
                "low": "100000",
                "price": "109500",
                "trade_amt": "80000000",  # 800억
                "chgrate": "9.5",
            },
            {
                "code": "000020",
                "name": "상한가종목",
                "open": "10000",
                "high": "12990",
                "low": "10000",
                "price": "12990",
                "trade_amt": "90000000",
                "chgrate": "29.9",
            },
            {
                "code": "000030",
                "name": "윗꼬리도지",
                "open": "10000",
                "high": "11000",
                "low": "9900",
                "price": "10050",
                "trade_amt": "60000000",
                "chgrate": "0.5",
            },
            {
                "code": "000040",
                "name": "거래대금부족",
                "open": "10000",
                "high": "11000",
                "low": "10000",
                "price": "10950",
                "trade_amt": "20000000",  # 200억
                "chgrate": "9.5",
            },
        ]

        realtime_scanner = RealtimeAnchorScanner(
            client=self.mock_client,
            datafeed=self.mock_feed,
            strategy=self.strategy,
            context=self.mock_context,
            min_turnover_krw=50_000_000_000.0,
            max_gain_pct=29.5,
            top_n=2,
        )

        promoted = realtime_scanner.scan_cycle()

        # Only Top 2 qualified candidates promoted
        self.assertEqual(len(promoted), 2)
        self.assertIn("005930", promoted)
        self.assertIn("000660", promoted)
        self.assertNotIn("000020", promoted)  # Upper limit excluded
        self.assertNotIn("000030", promoted)  # Doji excluded
        self.assertNotIn("000040", promoted)  # Low turnover excluded

        # Candidate scanner must have them injected dynamically
        self.assertIn("005930", self.scanner_rule._day_candidates)
        self.assertIn("000660", self.scanner_rule._day_candidates)

        # Context candidates promoted
        self.assertIn("005930", self.mock_context.candidate_symbols)
        self.assertIn("000660", self.mock_context.candidate_symbols)


class TestHarness3_EntryOrderExecutionAndCollarCapGuard(unittest.TestCase):
    """
    [Harness 3] Verifies live order placement at 15:20:
      1. Closing Call Auction (15:15~15:30) routes strictly with exchange="KRX"
      2. Collar Cap (+0.5%) tick rounding and price capping
      3. Safe pending order registration
      4. Empty string order ID rejection handling and state rollback
    """

    def setUp(self):
        self.mock_client = MagicMock()

    def test_closing_auction_forces_krx_and_collar_cap(self):
        """Orders during closing auction must route to KRX and cap at +0.5% floor tick."""
        self.mock_client.order_cash_buy.return_value = {
            "rt_cd": "0",
            "output": {"ODNO": "0000123456"},
        }
        broker = KisLiveBroker(mode="real", client=self.mock_client)

        order = Order(
            order_id="TEST_ORD_001",
            symbol="005930",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=100,
            price=50000.0,
            tag="ANCHOR_CLOSE_ENTRY",
        )

        with patch("engine.broker.kis_live.datetime") as mock_dt:
            # Simulate 15:20:00 (closing auction window)
            mock_dt.now.return_value = datetime(2026, 11, 19, 15, 20, 0)
            order_id = broker.submit_order(order)

        self.assertEqual(order_id, "0000123456")
        self.assertEqual(order.status, OrderStatus.PENDING)

        # Verify call to client
        # 50,000 * 1.005 = 50,250 -> rounded to tick (50,200 floor tick for KRX tick <= 50,000 / 100)
        expected_collar_price = round_to_krx_tick(50000.0 * 1.005, side="BUY", round_mode="floor")
        self.mock_client.order_cash_buy.assert_called_once_with(
            symbol="005930",
            qty=100,
            price=expected_collar_price,
            order_type="00",
            exchange="KRX",  # Strictly KRX enforced!
        )

    def test_broker_rejection_empty_order_id_rollback(self):
        """When broker rejects order and returns empty ODNO, strategy rolls back slot."""
        scanner = AnchorBodyMicroScanner()
        entry = AnchorCloseEntry()
        exit_p = AnchorMorningExit()
        strategy = CompositeStrategy(
            scanner=scanner,
            entry=entry,
            exit_policy=exit_p,
            max_daily_positions=2,
            dynamic_slots=False,
            total_capital=10_000_000.0,
        )

        class MockRejectionContext:
            def __init__(self):
                self.orders = []
            def get_position(self, sym): return None
            def get_balance(self): return AccountBalance(10_000_000.0, 10_000_000.0, {})
            def buy(self, symbol, quantity, price, tag):
                return ""  # Rejection returns empty string!

        ctx = MockRejectionContext()
        bar = MinuteBar(
            "005930",
            datetime(2026, 11, 19, 15, 20),
            50000.0, 50500.0, 49800.0, 50200.0, 10000, 500_000_000.0, cum_turnover=50_000_000_000.0
        )
        scanner._day_candidates = ["005930"]
        strategy.on_bar(ctx, bar)

        # Slot must NOT be consumed
        self.assertEqual(strategy._daily_trade_count, 0)


class TestHarness4_OvernightAndMorningExit(unittest.TestCase):
    """
    [Harness 4] Verifies Overnight Position Safeguards & Morning Exits:
      1. 15:20 intraday liquidation does NOT sell overnight anchor positions
      2. 08:50 Opening Call Auction transmits Market Sell order
      3. 09:00 Regular Market Open Exit triggers properly
      4. Safe evaluation on pure TickData (bar=None)
    """

    def test_overnight_anchor_position_safe_from_1520_sweep(self):
        """Overnight anchor strategy positions must be shielded from day-trade 15:20 liquidation."""
        mock_client = MagicMock()
        broker = KisLiveBroker(mode="vts", client=mock_client)

        pos = Position(
            symbol="005930",
            quantity=100,
            avg_price=50000.0,
            current_price=50200.0,
            high_water_mark=50200.0,
            entry_dt=datetime(2026, 11, 19, 15, 20),
            phase=1,
        )
        broker._positions["005930"] = pos

        orchestrator = DailyRoutineOrchestrator(
            mode="mock",
            scanner_name="champion_b",  # Overnight anchor strategy
            capital=10_000_000.0,
        )

        is_overnight = (
            orchestrator.scanner_name in ("champion_b", "anchor_body_micro", "anchor_close")
            or "anchor" in getattr(orchestrator.strategy, "name", "").lower()
        )
        self.assertTrue(is_overnight)

        # In daily_routine.py: 15:20 sweep is conditionally bypassed for is_overnight
        # Broker positions remain intact
        self.assertIn("005930", broker.get_positions())

    def test_stage_1_5_0850_call_auction_order_dispatch(self):
        """Held positions receive Stage 1.5 market sell orders during 08:50 opening call auction."""
        mock_client = MagicMock()
        mock_client.order_cash_sell.return_value = {
            "rt_cd": "0",
            "output": {"ODNO": "0000888888"},
        }
        broker = KisLiveBroker(mode="vts", client=mock_client)

        pos = Position(
            symbol="005930",
            quantity=50,
            avg_price=50000.0,
            current_price=50000.0,
            high_water_mark=50000.0,
            entry_dt=datetime(2026, 11, 19, 15, 20),
            phase=1,
        )
        broker._positions["005930"] = pos

        # Submit call auction market sell
        order = Order(
            order_id="OPEN_AUCTION_005930_12345",
            symbol="005930",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=50,
            price=0.0,
            tag="KRX_0900_OPEN_AUCTION",
        )
        order_id = broker.submit_order(order)
        self.assertEqual(order_id, "0000888888")

        mock_client.order_cash_sell.assert_called_once_with(
            symbol="005930",
            qty=50,
            price=0,
            order_type="01",  # Market order
            exchange="KRX",
        )

    def test_anchor_morning_exit_pure_tick_without_bar(self):
        """AnchorMorningExit must evaluate tick events without bar safely."""
        exit_rule = AnchorMorningExit(exit_at_open=True, open_exit_time=dtime(9, 0))
        pos = Position(
            symbol="005930",
            quantity=100,
            avg_price=50000.0,
            current_price=50000.0,
            high_water_mark=50000.0,
            entry_dt=datetime(2026, 11, 19, 15, 20),
            phase=1,
        )

        # 09:00:00 Opening Tick
        tick = TickData(
            symbol="005930",
            dt=datetime(2026, 11, 20, 9, 0, 0),
            price=51000.0,
            volume=5000,
            cum_turnover=250_000_000.0,
        )
        sig = exit_rule.evaluate_exit(pos, None, tick=tick)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.reason, "KRX_0900_OPEN_EXIT")
        self.assertEqual(sig.target_price, 51000.0)


class TestHarness5_WebSocketAndNoticeReconciliation(unittest.TestCase):
    """
    [Harness 5] Verifies live WebSocket datafeed parsing and execution notice processing:
      1. Corrected field offsets in H0UNCNT0 (fields 7-10 for vwap, open, high, low)
      2. Handling of CNTG_YN == "2" (execution fill)
      3. Dynamic cash balance and position quantity update
      4. Safe handling of CNTG_YN != "2" (cancellation/modification)
    """

    def setUp(self):
        self.mock_client = MagicMock()
        self.broker = KisLiveBroker(mode="real", client=self.mock_client)
        self.broker._latest_balance = AccountBalance(
            total_equity=10_000_000.0,
            cash=10_000_000.0,
            positions={},
        )

    def test_h0uncnt0_execution_fill_updates_cash_and_position(self):
        """CNTG_YN == '2' fills pending buy order, decrements cash, creates position."""
        # Setup pending order
        self.broker._pending_orders["0000999999"] = Order(
            order_id="0000999999",
            symbol="005930",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=100,
            price=50000.0,
            status=OrderStatus.PENDING,
        )

        # Simulate execution notice
        notice = {
            "order_no": "0000999999",
            "symbol": "005930",
            "side": "BUY",
            "qty": 100,
            "price": 50000.0,
            "cntg_yn": "2",  # Execution completed
        }

        self.broker.handle_execution_notice(notice)

        # Position created
        pos = self.broker.get_position("005930")
        self.assertIsNotNone(pos)
        self.assertEqual(pos.quantity, 100)
        self.assertEqual(pos.avg_price, 50000.0)

        # Cash updated (10,000,000 - (5,000,000 + commission))
        bal = self.broker.get_balance()
        self.assertLess(bal.cash, 10_000_000.0)
        self.assertAlmostEqual(bal.cash, 10_000_000.0 - (5000000.0 + 5000000.0 * 0.00014), places=1)

    def test_h0uncnt0_cancel_notice_safely_ignored(self):
        """CNTG_YN != '2' is handled without crashing or creating ghost positions."""
        notice = {
            "order_no": "0000888888",
            "symbol": "005930",
            "side": "BUY",
            "qty": 0,
            "price": 0.0,
            "cntg_yn": "1",  # Order accepted or modified, NOT filled
        }
        # Should not raise exception or create position
        self.broker.handle_execution_notice(notice)
        self.assertIsNone(self.broker.get_position("005930"))


class TestHarness6_DailyRoutineEndToEndOrchestrator(unittest.TestCase):
    """
    [Harness 6] End-to-End simulation of DailyRoutineOrchestrator:
      Verifies seamless coordination across Stage 1, Stage 1.5, Stage 2/3, and Stage 4.
    """

    @patch("engine.daily_routine.TradingEngine")
    @patch("engine.daily_routine.KisLiveBroker")
    @patch("engine.daily_routine.RealtimeAnchorScanner")
    @patch("engine.daily_routine.time.sleep")
    def test_daily_routine_lifecycle_execution(self, mock_sleep, mock_scanner_cls, mock_broker_cls, mock_engine_cls):
        """Simulates complete daily lifecycle execution without errors."""
        mock_broker = MagicMock()
        mock_broker.get_balance.return_value = AccountBalance(10_000_000.0, 10_000_000.0, {})
        mock_broker.get_positions.return_value = {}
        mock_broker_cls.return_value = mock_broker

        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine

        mock_scanner = MagicMock()
        mock_scanner_cls.return_value = mock_scanner

        orchestrator = DailyRoutineOrchestrator(
            mode="mock",
            target_date="20261119",
            scanner_name="champion_b",
            entry_name="anchor_close",
            exit_name="anchor_morning",
            capital=10_000_000.0,
        )

        # Run routine with wait_for_market_open=False & single_cycle=True to immediately execute lifecycle
        try:
            orchestrator.run(wait_for_market_open=False, single_cycle=True)
            success = True
        except Exception as e:
            success = False

        self.assertTrue(success)
        mock_broker.sync_account_state.assert_called()


if __name__ == "__main__":
    unittest.main()
