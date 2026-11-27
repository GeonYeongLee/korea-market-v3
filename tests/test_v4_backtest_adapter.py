"""Comprehensive tests for Korea Market v4 Backtest Adapters & Unified CLI Backtest."""
from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from datetime import date, datetime, time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from core.krx_ticks import round_to_krx_tick
from v4.adapters.backtest import (
    BacktestBrokerAdapter, BacktestClock, BacktestConditionSearchAdapter,
    BacktestRealtimeFeed, CurrentDayStore, ParquetRegimeDataAdapter,
    load_day_bar_index, resolve_data_dir,
)
from v4.cli import run_v4_backtest
from v4.models import (
    BarCloseEvent, MinuteBar, MinutePulseEvent, OrderEvent,
    OrderRequest, OrderSide, OrderStatus, OrderType,
)


def _write_minute_parquet(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "symbol": pa.array([r["symbol"] for r in rows], type=pa.string()),
            "timestamp": pa.array([r["dt"] for r in rows], type=pa.timestamp("us")),
            "open": pa.array([float(r["open"]) for r in rows], type=pa.float64()),
            "high": pa.array([float(r["high"]) for r in rows], type=pa.float64()),
            "low": pa.array([float(r["low"]) for r in rows], type=pa.float64()),
            "close": pa.array([float(r["close"]) for r in rows], type=pa.float64()),
            "volume": pa.array([int(r["volume"]) for r in rows], type=pa.int64()),
            "turnover": pa.array([float(r["turnover"]) for r in rows], type=pa.float64()),
            "vwap": pa.array([float(r.get("vwap", r["close"])) for r in rows], type=pa.float64()),
            "is_premarket": pa.array([bool(r.get("is_pm", False)) for r in rows], type=pa.bool_()),
        }
    )
    pq.write_table(table, path)


def test_parquet_regime_data_adapter_features_and_late_open(tmp_path: Path) -> None:
    daily_path = tmp_path / "daily_bars_2y.parquet"
    index_path = tmp_path / "index_daily.parquet"
    nxt_path = tmp_path / "nxt_symbols.json"
    data_dir = tmp_path / "daily"

    dates = [date(2025, 12, 24 + i) for i in range(6)] + [date(2026, 1, 2)]
    closes = [10000.0, 10200.0, 10100.0, 10300.0, 10400.0, 10500.0, 12000.0]
    pcs = [9900.0, 10000.0, 10200.0, 10100.0, 10300.0, 10400.0, 10500.0]
    adtos = [1e9, 1.1e9, 1.2e9, 1.3e9, 1.4e9, 1.5e9, 9.9e9]
    pq.write_table(pa.table({
        "date": pa.array(dates, type=pa.date32()), "symbol": pa.array(["005930"] * len(dates), type=pa.string()),
        "open": pa.array(closes, type=pa.float64()), "high": pa.array([c + 300.0 for c in closes], type=pa.float64()),
        "low": pa.array([c - 200.0 for c in closes], type=pa.float64()), "close": pa.array(closes, type=pa.float64()),
        "prev_close": pa.array(pcs, type=pa.float64()), "turnover": pa.array(adtos, type=pa.float64()),
        "adto20": pa.array(adtos, type=pa.float64()),
    }), daily_path)
    pq.write_table(pa.table({
        "date": pa.array([date(2026, 1, 2)], type=pa.date32()), "ks_gap_pct": pa.array([0.45], type=pa.float64()),
        "kq_gap_pct": pa.array([-0.15], type=pa.float64()), "ks_trend": pa.array([0.02], type=pa.float64()),
        "kq_trend": pa.array([-0.01], type=pa.float64()), "market_gap_std20": pa.array([0.80], type=pa.float64()),
        "is_bear_market": pa.array([False], type=pa.bool_()),
    }), index_path)
    nxt_path.write_text(json.dumps({"symbols": ["005930"]}), encoding="utf-8")
    _write_minute_parquet(
        data_dir / "20260102" / "005930.parquet",
        [
            {"symbol": "005930", "dt": datetime(2026, 1, 2, 10, 0), "open": 10600, "high": 10700, "low": 10550, "close": 10650, "volume": 100, "turnover": 1e6},
            {"symbol": "005930", "dt": datetime(2026, 1, 2, 15, 19), "open": 10800, "high": 10850, "low": 10790, "close": 10820, "volume": 50, "turnover": 5e5},
        ],
    )

    adapter = ParquetRegimeDataAdapter(
        data_dir=str(data_dir),
        daily_file=str(daily_path),
        index_file=str(index_path),
        nxt_file=str(nxt_path),
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 2),
        day_store=CurrentDayStore(),
    )
    regime = asyncio.run(adapter.get_index_regime(date(2026, 1, 2)))
    feats = asyncio.run(adapter.get_symbol_features(date(2026, 1, 2)))
    prev_closes = asyncio.run(adapter.get_prev_closes(date(2026, 1, 2)))

    assert regime.market_open_time == time(10, 0)
    assert regime.market_close_time == time(15, 19)
    assert regime.ks_gap_pct == pytest.approx(0.45)
    assert prev_closes["005930"] == pytest.approx(10500.0)
    f = feats["005930"]
    assert f.is_nxt_eligible is True
    assert f.prior_adto20 == pytest.approx(1.5e9)  # t-1 ADTO20, never 9.9e9 on day t
    assert f.prior_atr20_pct == pytest.approx(500.0 / 10500.0)


def test_condition_emulator_premarket_seq_causality_and_mae(tmp_path: Path) -> None:
    day_dir = tmp_path / "20261126"
    _write_minute_parquet(
        day_dir / "005930.parquet",
        [
            {"symbol": "005930", "dt": datetime(2026, 11, 26, 8, 0), "open": 170000, "high": 171000, "low": 168000, "close": 170500, "volume": 50000, "turnover": 100e9, "is_pm": True},
            {"symbol": "005930", "dt": datetime(2026, 11, 26, 9, 0), "open": 172900, "high": 173500, "low": 172500, "close": 173000, "volume": 5000, "turnover": 10e9},
            {"symbol": "005930", "dt": datetime(2026, 11, 26, 9, 1), "open": 173000, "high": 179500, "low": 172800, "close": 179000, "volume": 3000, "turnover": 5e9},
            {"symbol": "005930", "dt": datetime(2026, 11, 26, 9, 2), "open": 179000, "high": 180000, "low": 178500, "close": 179500, "volume": 1000, "turnover": 2e9},
            {"symbol": "005930", "dt": datetime(2026, 11, 26, 9, 3), "open": 179500, "high": 185000, "low": 179000, "close": 184000, "volume": 2000, "turnover": 4e9},
        ],
    )
    _write_minute_parquet(
        day_dir / "000660.parquet",
        [
            {"symbol": "000660", "dt": datetime(2026, 11, 26, 9, 0), "open": 100000, "high": 100500, "low": 99800, "close": 100000, "volume": 100, "turnover": 1e9},
            {"symbol": "000660", "dt": datetime(2026, 11, 26, 9, 1), "open": 100000, "high": 104000, "low": 98500, "close": 103500, "volume": 200, "turnover": 2e9},
        ],
    )
    day_idx = load_day_bar_index(str(day_dir), date(2026, 11, 26))

    async def _check() -> None:
        emu = BacktestConditionSearchAdapter(execution_mode="pessimistic")
        emu.set_day_metadata(date(2026, 11, 26), prev_closes={"005930": 170000.0, "000660": 99000.0}, nxt_flags={"005930": True, "000660": False})
        emu.advance_minute(day_idx, datetime(2026, 11, 26, 9, 1))
        c_pess = await emu.poll_candidates(datetime(2026, 11, 26, 9, 1), seq="0")
        assert [c.symbol for c in c_pess] == ["005930"]
        c0 = c_pess[0]
        assert c0.day_open == 172900.0 and c0.krx_day_low == 172500.0 and c0.prior_krx_day_low == 172500.0
        assert c0.krx_cum_turnover == pytest.approx(115e9)
        assert c0.curr_bar_surge_ok is True and c0.prev_bar_surge_ok is False and c0.is_nxt_eligible is True

        # seq="1" at 09:02 keeps 005930 via prev_bar (09:01 >= open+1m), while seq="0" drops it
        emu.advance_minute(day_idx, datetime(2026, 11, 26, 9, 3))
        assert await emu.poll_candidates(datetime(2026, 11, 26, 9, 2), seq="0") == []
        c_seq1 = await emu.poll_candidates(datetime(2026, 11, 26, 9, 2), seq="1")
        assert [c.symbol for c in c_seq1] == ["005930"]
        assert c_seq1[0].curr_bar_surge_ok is False and c_seq1[0].prev_bar_surge_ok is True

        # Strict as_of causality even after emulator previously reached 09:03
        snap = await emu.get_intraday_snapshot("005930", datetime(2026, 11, 26, 9, 1))
        bars = await emu.get_recent_minute_bars("005930", datetime(2026, 11, 26, 9, 1), limit=10)
        assert snap is not None and snap.cum_turnover == pytest.approx(115e9)
        assert snap.premarket_turnover == pytest.approx(100e9) and snap.premarket_low == 168000.0
        assert snap.day_low == 172500.0 and [b.dt for b in bars] == [datetime(2026, 11, 26, 9, 0), datetime(2026, 11, 26, 9, 1)]

        # Optimistic mode checks prior_krx_day_low (09:00 low=99800) so 000660 qualifies at 09:01
        emu.set_execution_mode("optimistic")
        c_opt = await emu.poll_candidates(datetime(2026, 11, 26, 9, 1), seq="0")
        assert {c.symbol for c in c_opt} == {"005930", "000660"}

    asyncio.run(_check())


def test_feed_same_minute_pulse_subscribe_bar_ordering_and_fast_forward(tmp_path: Path) -> None:
    day_dir = tmp_path / "20261126"
    for sym, to in [("005930", 5e9), ("000660", 5e9), ("035420", 8e9)]:
        _write_minute_parquet(
            day_dir / f"{sym}.parquet",
            [
                {"symbol": sym, "dt": datetime(2026, 11, 26, 9, 0), "open": 10000, "high": 10100, "low": 9990, "close": 10050, "volume": 100, "turnover": 1e9},
                {"symbol": sym, "dt": datetime(2026, 11, 26, 9, 1), "open": 10050, "high": 10500, "low": 10000, "close": 10450, "volume": 500, "turnover": to},
                {"symbol": sym, "dt": datetime(2026, 11, 26, 9, 2), "open": 10450, "high": 10500, "low": 10400, "close": 10480, "volume": 100, "turnover": 1e9},
            ],
        )

    async def _run() -> None:
        feed = BacktestRealtimeFeed(
            data_dir=str(tmp_path),
            start_date=date(2026, 11, 26),
            end_date=date(2026, 11, 26),
            max_subscriptions=2,
            protected_symbols_fn=lambda: {"035420", "000660"},
            should_continue_day_fn=lambda dt: dt.time() <= time(9, 2),
        )
        await feed.start()
        assert await feed.subscribe("035420", True) is True
        assert await feed.subscribe("000660", False) is True
        assert await feed.subscribe("005930", True) is False  # all 2 slots protected

        feed.max_subscriptions = 3
        pulses: list[datetime] = []
        bars_0901: list[str] = []
        async for ev in feed.stream_events():
            if isinstance(ev, MinutePulseEvent):
                pulses.append(ev.dt)
                if ev.dt == datetime(2026, 11, 26, 9, 1):
                    await feed.subscribe("005930", True)
            elif isinstance(ev, BarCloseEvent) and ev.dt == datetime(2026, 11, 26, 9, 1):
                bars_0901.append(ev.symbol)
        await feed.stop()

        assert bars_0901 == ["035420", "000660", "005930"]
        assert pulses == [
            datetime(2026, 11, 26, 9, 0),
            datetime(2026, 11, 26, 9, 1),
            datetime(2026, 11, 26, 9, 2),
        ]

    asyncio.run(_run())


def test_broker_adapter_slippage_fees_ticks_and_net_pnl() -> None:
    async def _run() -> None:
        clock = BacktestClock(datetime(2026, 11, 26, 9, 1))
        broker = BacktestBrokerAdapter(initial_capital=10_000_000.0)
        events: list[OrderEvent] = []
        broker.on_execution_event(events.append)

        buy_ev = await broker.submit_order(
            OrderRequest("005930", OrderSide.BUY, OrderType.LIMIT, 10, 50_000.0, tag="BRK", created_at=clock.now(), client_order_id="cid-1")
        )
        exp_buy_p = float(round_to_krx_tick(50_000.0 * 1.00035, side="BUY", round_mode="ceil"))
        assert exp_buy_p == 50_100.0 and buy_ev.filled_price == exp_buy_p
        assert buy_ev.cum_filled_quantity == 10 and buy_ev.client_order_id == "cid-1"

        bar_next = MinuteBar("005930", datetime(2026, 11, 26, 9, 2), 50_500.0, 52_500.0, 50_200.0, 52_000.0, 1000)
        broker.on_bar(bar_next)
        pos = broker.get_position("005930")
        assert pos is not None and pos.high_water_mark == 52_500.0 and pos.low_water_mark == 50_100.0

        sell_ev = await broker.submit_order(
            OrderRequest("005930", OrderSide.SELL, OrderType.MARKET, 10, 52_000.0, tag="TP", created_at=bar_next.dt)
        )
        exp_sell_p = float(round_to_krx_tick(52_000.0 * 0.99965, side="SELL", round_mode="floor"))
        assert exp_sell_p == 51_900.0 and sell_ev.filled_price == exp_sell_p
        buy_comm = exp_buy_p * 10 * 0.00014
        sell_comm = exp_sell_p * 10 * 0.00014
        sell_tax = exp_sell_p * 10 * 0.0018
        exp_net = (exp_sell_p - exp_buy_p) * 10 - sell_comm - sell_tax - buy_comm
        assert sell_ev.net_pnl == pytest.approx(exp_net)
        trades = broker.get_completed_trades()
        assert len(trades) == 1 and trades[0].net_pnl == pytest.approx(exp_net)
        assert len(events) == 2 and broker.get_balance().cash == pytest.approx(10_000_000.0 + exp_net)

        rej = await broker.submit_order(OrderRequest("005930", OrderSide.BUY, OrderType.LIMIT, 1000, 50_000.0, created_at=bar_next.dt))
        assert rej.status == OrderStatus.REJECTED and rej.message == "REJECTED_INSUFFICIENT_FUNDS"

    asyncio.run(_run())


def test_end_to_end_smoke_backtest_nov23_to_nov25() -> None:
    data_dir = resolve_data_dir()
    if not (Path(data_dir) / "20261123").is_dir():
        pytest.skip("Real dataset 2026-11-23..2026-11-25 unavailable")

    args = Namespace(
        scanner="all_weather",
        entry="all_weather",
        exit="all_weather",
        start="2026-11-23",
        end="2026-11-25",
        capital=10_000_000.0,
        slots=None,
        min_gap=0.00,
        max_gap=0.10,
        max_mae=-0.015,
        dynamic=True,
        regime_mode="all_weather",
        strict_v3_parity=False,
    )
    shared_store = CurrentDayStore()
    shared_regime = ParquetRegimeDataAdapter(data_dir=data_dir, start_date=args.start, end_date=args.end, day_store=shared_store)
    pess = run_v4_backtest(args, "pessimistic", data_dir, day_store=shared_store, regime_adapter=shared_regime)
    opti = run_v4_backtest(args, "optimistic", data_dir, day_store=shared_store, regime_adapter=shared_regime)

    assert pess.total_trades >= 1 and opti.total_trades >= 1
    assert opti.total_net_pnl >= pess.total_net_pnl
    for t in pess.trades + opti.trades:
        assert t.entry_dt <= t.exit_dt
        assert t.quantity > 0 and t.entry_price > 0.0 and t.exit_price > 0.0


def test_corrupt_truncated_parquet_skipped_and_0900_excluded_in_seq1(tmp_path: Path) -> None:
    day_dir = tmp_path / "20261126"
    _write_minute_parquet(
        day_dir / "035420.parquet",
        [
            {"symbol": "035420", "dt": datetime(2026, 11, 26, 9, 0), "open": 10000, "high": 10500, "low": 9990, "close": 10400, "volume": 500, "turnover": 5e9},
            {"symbol": "035420", "dt": datetime(2026, 11, 26, 9, 1), "open": 10400, "high": 10450, "low": 10380, "close": 10420, "volume": 100, "turnover": 1e9},
        ],
    )
    (day_dir / "099520.parquet").write_bytes(b"PAR1bad")  # < 12 bytes truncated header
    (day_dir / "099521.parquet").write_bytes(b"PAR1" + b"\x00" * 16)  # missing footer PAR1

    day_idx = load_day_bar_index(str(day_dir), date(2026, 11, 26))
    assert set(day_idx.syms) == {"035420"}

    async def _check() -> None:
        emu = BacktestConditionSearchAdapter(execution_mode="pessimistic")
        emu.set_day_metadata(date(2026, 11, 26), prev_closes={"035420": 9900.0})
        emu.advance_minute(day_idx, datetime(2026, 11, 26, 9, 1))
        cands = await emu.poll_candidates(datetime(2026, 11, 26, 9, 1), seq="1")
        assert cands == [] or all(not c.prev_bar_surge_ok for c in cands)

    asyncio.run(_check())
