"""Unit tests for v4 IAuditSink (NullAuditSink, JsonlAuditSink) and runner audit trail."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from tests.fakes_v4 import FakeConditionSearch, FakeRegimeData, make_v4_runner
from v4.audit_sink import JsonlAuditSink, NullAuditSink
from v4.models import BarCloseEvent, ConditionCandidate, MinuteBar, MinutePulseEvent


def test_null_audit_sink_noop() -> None:
    sink = NullAuditSink()
    sink.emit("CANDIDATE_POLL", datetime(2026, 11, 26, 9, 2), count=1)
    sink.close()


def test_jsonl_audit_sink_records_full_runner_lifecycle(tmp_path: Path) -> None:
    dt_pulse = datetime(2026, 11, 26, 9, 2, 0)
    dt_bar1 = datetime(2026, 11, 26, 9, 3, 0)
    cond = FakeConditionSearch()
    cond.polls[dt_pulse] = [
        ConditionCandidate(
            "005930", day_open=50_000.0, krx_day_low=49_900.0, prior_krx_day_low=49_900.0,
            krx_cum_turnover=1_500_000_000.0, bar_open=52_000.0, bar_high=54_500.0,
            seq="0", as_of=dt_pulse, is_nxt_eligible=True,
        ),
        ConditionCandidate(
            "000660", day_open=60_000.0, krx_day_low=59_900.0, prior_krx_day_low=59_900.0,
            krx_cum_turnover=1_500_000_000.0, bar_open=60_000.0, bar_high=62_500.0,
            seq="0", as_of=dt_pulse, is_nxt_eligible=False,
        ),  # GAP_OUT vs prev_close 50_000 (+20%)
        ConditionCandidate(
            "035420", day_open=50_000.0, krx_day_low=48_000.0, prior_krx_day_low=48_000.0,
            krx_cum_turnover=1_500_000_000.0, bar_open=50_000.0, bar_high=52_500.0,
            seq="0", as_of=dt_pulse, is_nxt_eligible=True,
        ),  # MAE_BREACH (-4%)
        ConditionCandidate(
            "035720", day_open=50_000.0, krx_day_low=49_900.0, prior_krx_day_low=49_900.0,
            krx_cum_turnover=100_000_000.0, bar_open=50_000.0, bar_high=52_500.0,
            seq="0", as_of=dt_pulse, is_nxt_eligible=True,
        ),  # TURNOVER_LOW
    ]
    bar0 = MinuteBar(
        "005930", dt_pulse, 52_000.0, 54_500.0, 51_800.0, 54_200.0,
        10_000, 500_000_000.0, cum_turnover=1_500_000_000.0,
    )
    bar1 = MinuteBar(
        "005930", dt_bar1, 53_700.0, 53_800.0, 53_500.0, 53_600.0,
        5_000, 300_000_000.0, cum_turnover=1_800_000_000.0,
    )

    out_path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(out_path)
    runner = make_v4_runner(
        [MinutePulseEvent(dt_pulse), BarCloseEvent("005930", dt_pulse, bar0), BarCloseEvent("005930", dt_bar1, bar1)],
        cond=cond,
        regime=FakeRegimeData(prev_closes={"005930": 49_000.0, "000660": 50_000.0, "035420": 49_000.0, "035720": 49_000.0}),
        audit_sink=sink,
    )
    try:
        trades = asyncio.run(runner.run())
    finally:
        sink.close()

    assert len(trades) == 1
    records = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    events_by_type: dict[str, list[dict]] = {}
    for rec in records:
        assert {"event", "dt", "symbol"}.issubset(rec.keys())
        events_by_type.setdefault(rec["event"], []).append(rec)

    polls = events_by_type.get("CANDIDATE_POLL", [])
    assert len(polls) == 1
    assert polls[0]["seq"] == "1"
    assert polls[0]["count"] == 4
    assert polls[0]["symbols"] == ["005930", "000660", "035420", "035720"]

    gates = {g["symbol"]: g for g in events_by_type.get("CANDIDATE_GATE", [])}
    assert set(gates.keys()) == {"005930", "000660", "035420", "035720"}
    for g in gates.values():
        assert {"seq", "passed", "reason", "day_open", "krx_day_low", "cum_turnover", "eff_min_turnover", "is_nxt"}.issubset(g.keys())
    assert gates["005930"]["passed"] is True and gates["005930"]["reason"] == "PASSED"
    assert gates["000660"]["passed"] is False and gates["000660"]["reason"] == "GAP_OUT"
    assert gates["035420"]["passed"] is False and gates["035420"]["reason"] == "MAE_BREACH"
    assert gates["035720"]["passed"] is False and gates["035720"]["reason"] == "TURNOVER_LOW"

    sigs = events_by_type.get("ENTRY_SIGNAL", [])
    assert len(sigs) == 1
    assert sigs[0]["symbol"] == "005930"
    assert sigs[0]["qty"] > 0
    assert sigs[0]["trigger_price"] > 0
    assert sigs[0]["reason"]

    orders = events_by_type.get("ORDER_EVENT", [])
    assert len(orders) >= 2
    for o in orders:
        assert {"side", "status", "requested_price", "filled_price", "filled_quantity", "net_pnl", "tag"}.issubset(o.keys())
    assert any(o["side"] == "BUY" and o["status"] == "FILLED" for o in orders)
    assert any(o["side"] == "SELL" and o["status"] == "FILLED" for o in orders)


def test_jsonl_audit_sink_swallows_io_errors(tmp_path: Path) -> None:
    class _BrokenStream:
        def write(self, _s: str) -> int:
            raise OSError("disk full")

        def flush(self) -> None:
            raise OSError("flush error")

        def close(self) -> None:
            raise OSError("close error")

    sink = JsonlAuditSink(tmp_path / "broken.jsonl")
    sink.close()
    sink._fp = _BrokenStream()  # type: ignore[assignment]

    # Must never raise on emit() or close(), and close() must clear _fp.
    sink.emit("CANDIDATE_POLL", datetime(2026, 11, 26, 9, 2), count=1)
    sink.close()
    assert sink._fp is None

    # Closed file object directly on real sink also must not raise.
    sink2 = JsonlAuditSink(tmp_path / "closed.jsonl")
    assert sink2._fp is not None
    sink2._fp.close()
    sink2.emit("CANDIDATE_POLL", datetime(2026, 11, 26, 9, 2), count=1)
    sink2.close()
    assert sink2._fp is None
