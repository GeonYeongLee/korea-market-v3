"""
Quiet-Bar Wall-Clock 1-Minute Bar Accumulator for KIS Live WebSocket Feed.
Builds 1-minute bars from live ticks and flushes quiet bars at `M+1:00.1`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from v4.models import BarCloseEvent, MinuteBar, TickEvent


@dataclass(slots=True)
class _FormingBarState:
    symbol: str
    bar_dt: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    turnover: float
    cum_turnover: float
    vwap: float


class BarAccumulator:
    """Builds 1-minute bars from live ticks and flushes quiet bars at `M+1:00.1`."""

    def __init__(self) -> None:
        self._bars: Dict[str, _FormingBarState] = {}

    def _to_event(self, st: _FormingBarState) -> BarCloseEvent:
        bar = MinuteBar(
            symbol=st.symbol,
            dt=st.bar_dt,
            open=st.open,
            high=st.high,
            low=st.low,
            close=st.close,
            volume=st.volume,
            turnover=st.turnover if st.turnover > 0.0 else float(st.close * st.volume),
            vwap=st.vwap if st.vwap > 0.0 else st.close,
            cum_turnover=st.cum_turnover,
            is_premarket=False,
            source="kis_ws",
        )
        return BarCloseEvent(symbol=st.symbol, dt=st.bar_dt, bar=bar)

    def on_tick(self, tick: TickEvent) -> Optional[BarCloseEvent]:
        """Updates forming bar for `tick.symbol`; returns `BarCloseEvent` on minute rollover."""
        tick_min = tick.dt.replace(second=0, microsecond=0)
        st = self._bars.get(tick.symbol)
        closed: Optional[BarCloseEvent] = None

        if st is not None and st.bar_dt != tick_min:
            closed = self._to_event(st)
            st = None

        if st is None:
            self._bars[tick.symbol] = _FormingBarState(
                symbol=tick.symbol,
                bar_dt=tick_min,
                open=tick.price,
                high=tick.price,
                low=tick.price,
                close=tick.price,
                volume=tick.volume,
                turnover=tick.turnover,
                cum_turnover=tick.cum_turnover,
                vwap=tick.vwap or tick.price,
            )
        else:
            st.high = max(st.high, tick.price)
            st.low = min(st.low, tick.price)
            st.close = tick.price
            st.volume += tick.volume
            st.turnover += tick.turnover
            if tick.cum_turnover > st.cum_turnover:
                st.cum_turnover = tick.cum_turnover
            if tick.vwap > 0.0:
                st.vwap = tick.vwap
        return closed

    def flush_expired(self, now_dt: datetime) -> List[BarCloseEvent]:
        """Flushes any forming bar whose minute `M` has reached `M+1:00.1`."""
        cur_min = now_dt.replace(second=0, microsecond=0)
        closed: List[BarCloseEvent] = []
        for sym in list(self._bars.keys()):
            st = self._bars[sym]
            if st.bar_dt < cur_min and (now_dt - (st.bar_dt + timedelta(minutes=1))).total_seconds() >= 0.1:
                closed.append(self._to_event(self._bars.pop(sym)))
        return closed

    def discard(self, symbol: str) -> None:
        self._bars.pop(symbol, None)
