"""
Real-time Tick-to-Bar Aggregator for Korea Market v3.
Aggregates high-frequency KIS WebSocket tick data (H0UNCNT0, H0STCNT0) into
standardized v3 MinuteBar instances and dispatches sub-second tick risk events.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as dtime
from typing import Dict, Optional, Callable, List, Any, Union

from engine.models import MinuteBar, TickData

logger = logging.getLogger("korea_market.datafeed.tick_aggregator")

BarCallback = Callable[..., Any]
TickCallback = Callable[..., Any]


def _safe_dispatch_bar(cb: BarCallback, symbol: str, bar: MinuteBar) -> Any:
    try:
        return cb(symbol, bar)
    except TypeError:
        return cb(bar)


def _safe_dispatch_tick(cb: TickCallback, symbol: str, tick: Any, now: datetime) -> Any:
    try:
        return cb(symbol, tick, now)
    except TypeError:
        try:
            return cb(symbol, tick)
        except TypeError:
            return cb(tick)


class SymbolBarBuilder:
    """Accumulates ticks within a single 1-minute window for a specific symbol."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.current_minute_str: Optional[str] = None  # e.g., "0904"
        self.open: float = 0.0
        self.high: float = 0.0
        self.low: float = 0.0
        self.close: float = 0.0
        self.volume: int = 0
        self.turnover: float = 0.0
        self.cum_volume: int = 0
        self.cum_turnover: float = 0.0
        self.vwap: float = 0.0
        self.volume_power: float = 0.0
        self.tick_count: int = 0
        self.last_dt: Optional[datetime] = None

    def on_tick(self, tick: Union[Dict[str, Any], TickData]) -> Optional[MinuteBar]:
        """
        Processes incoming tick. If a new minute begins, returns the completed MinuteBar.
        """
        if isinstance(tick, TickData):
            price = float(tick.price)
            qty = int(tick.volume)
            now = tick.dt or datetime.now()
            tick_time_str = now.strftime("%H%M%S")
            cum_to = float(tick.cum_turnover)
            to_val = float(tick.turnover)
            tick_vwap = 0.0
            vol_power = 0.0
            cum_vol = 0
        else:
            price = float(tick.get("price", 0.0))
            qty = int(tick.get("volume", 0))
            tick_time_str = str(tick.get("time", ""))  # "HHMMSS"
            raw_dt = tick.get("dt")
            now = raw_dt if isinstance(raw_dt, datetime) else datetime.now()
            cum_to = float(tick.get("cum_turnover", 0.0))
            to_val = float(tick.get("turnover", 0.0))
            tick_vwap = float(tick.get("vwap", 0.0))
            vol_power = float(tick.get("volume_power", 0.0))
            cum_vol = int(tick.get("cum_volume", 0))

        if len(tick_time_str) >= 4:
            try:
                minute_key = tick_time_str[:4]
                hour = int(tick_time_str[:2])
                minute = int(tick_time_str[2:4])
                second = int(tick_time_str[4:6]) if len(tick_time_str) >= 6 else 0
                bar_dt = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
            except (ValueError, TypeError):
                minute_key = now.strftime("%H%M")
                bar_dt = now
        else:
            minute_key = now.strftime("%H%M")
            bar_dt = now

        completed_bar: Optional[MinuteBar] = None

        # Check for minute rollover
        if self.current_minute_str is not None and minute_key != self.current_minute_str:
            # Finalize previous bar
            prev_hour = int(self.current_minute_str[:2])
            prev_min = int(self.current_minute_str[2:4])
            prev_dt = (self.last_dt or now).replace(hour=prev_hour, minute=prev_min, second=0, microsecond=0)

            completed_bar = MinuteBar(
                symbol=self.symbol,
                dt=prev_dt,
                open=self.open,
                high=self.high,
                low=self.low,
                close=self.close,
                volume=self.volume,
                turnover=self.turnover if self.turnover > 0 else float(self.close * self.volume),
                vwap=self.vwap if self.vwap > 0 else self.close,
                cum_turnover=self.cum_turnover,
            )

            # Reset accumulator for the new minute
            self.open = price
            self.high = price
            self.low = price
            self.close = price
            self.volume = qty
            self.turnover = to_val if to_val > 0 else float(price * qty)
            self.tick_count = 1
            self.current_minute_str = minute_key
        elif self.current_minute_str is None:
            # First tick for this symbol
            self.current_minute_str = minute_key
            self.open = price
            self.high = price
            self.low = price
            self.close = price
            self.volume = qty
            self.turnover = to_val if to_val > 0 else float(price * qty)
            self.tick_count = 1
        else:
            # Continuing current minute
            if price > self.high:
                self.high = price
            if price < self.low:
                self.low = price
            self.close = price
            self.volume += qty
            self.turnover += to_val if to_val > 0 else float(price * qty)
            self.tick_count += 1

        # Update running aggregates
        if cum_to > 0:
            self.cum_turnover = cum_to
        if cum_vol > 0:
            self.cum_volume = cum_vol
        if tick_vwap > 0:
            self.vwap = tick_vwap
        elif self.cum_volume > 0 and self.cum_turnover > 0:
            self.vwap = self.cum_turnover / self.cum_volume
        elif self.vwap <= 0:
            self.vwap = self.close

        if vol_power > 0:
            self.volume_power = vol_power

        self.last_dt = bar_dt
        return completed_bar

    def flush_bar(self) -> Optional[MinuteBar]:
        """Flushes in-flight candle on EOD or shutdown."""
        if self.current_minute_str is None or self.volume == 0:
            return None
        prev_hour = int(self.current_minute_str[:2])
        prev_min = int(self.current_minute_str[2:4])
        dt = (self.last_dt or datetime.now()).replace(hour=prev_hour, minute=prev_min, second=0, microsecond=0)
        bar = MinuteBar(
            symbol=self.symbol,
            dt=dt,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            turnover=self.turnover if self.turnover > 0 else float(self.close * self.volume),
            vwap=self.vwap if self.vwap > 0 else self.close,
            cum_turnover=self.cum_turnover,
        )
        self.current_minute_str = None
        return bar


class TickToBarAggregator:
    """
    Manages multi-symbol real-time tick-to-bar aggregation and dispatches callbacks.
    """

    def __init__(self):
        self.builders: Dict[str, SymbolBarBuilder] = {}
        self._bar_callbacks: List[BarCallback] = []
        self._tick_callbacks: List[TickCallback] = []

    def register_bar_callback(self, cb: BarCallback) -> None:
        """Callback triggered when a 1-minute bar finishes."""
        self._bar_callbacks.append(cb)

    def register_tick_callback(self, cb: TickCallback) -> None:
        """Callback triggered on EVERY tick for sub-second stop-loss and trailing stops."""
        self._tick_callbacks.append(cb)

    def handle_tick_sync(self, msg_type: str, tick: Union[Dict[str, Any], TickData]) -> Optional[MinuteBar]:
        """Synchronous entrypoint for simulation and replay without asyncio overhead."""
        if msg_type != "TICK":
            return None

        symbol = tick.symbol if isinstance(tick, TickData) else tick.get("symbol")
        if not symbol:
            return None

        if symbol not in self.builders:
            self.builders[symbol] = SymbolBarBuilder(symbol)

        builder = self.builders[symbol]
        # 1. Process bar builder first
        completed_bar = builder.on_tick(tick)
        now = tick.dt if isinstance(tick, TickData) else (tick.get("dt") if isinstance(tick.get("dt"), datetime) else (builder.last_dt or datetime.now()))
        if isinstance(tick, TickData):
            tick.dt = now
        elif isinstance(tick, dict) and "dt" not in tick:
            tick = dict(tick, dt=now)

        # 2. Dispatch finalized bar FIRST on minute rollover so bar M settles before tick M+1
        if completed_bar is not None:
            for bcb in self._bar_callbacks:
                try:
                    res = _safe_dispatch_bar(bcb, symbol, completed_bar)
                    if asyncio.iscoroutine(res):
                        try:
                            loop = asyncio.get_running_loop()
                            loop.create_task(res)
                        except RuntimeError:
                            asyncio.run(res)
                except Exception as e:
                    logger.error(f"Error in bar callback for {symbol}: {e}", exc_info=True)

        # 3. Dispatch immediate tick-level risk callback with aligned timestamp
        for tcb in self._tick_callbacks:
            try:
                res = _safe_dispatch_tick(tcb, symbol, tick, now)
                if asyncio.iscoroutine(res):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(res)
                    except RuntimeError:
                        asyncio.run(res)
            except Exception as e:
                logger.error(f"Error in tick callback for {symbol}: {e}")

        return completed_bar

    async def handle_tick(self, msg_type: str, tick: Union[Dict[str, Any], TickData]) -> Optional[MinuteBar]:
        """Entrypoint for KIS WebSocket client messages."""
        if msg_type != "TICK":
            return None

        symbol = tick.symbol if isinstance(tick, TickData) else tick.get("symbol")
        if not symbol:
            return None

        if symbol not in self.builders:
            self.builders[symbol] = SymbolBarBuilder(symbol)

        builder = self.builders[symbol]
        # 1. Process bar builder first
        completed_bar = builder.on_tick(tick)
        now = tick.dt if isinstance(tick, TickData) else (tick.get("dt") if isinstance(tick.get("dt"), datetime) else (builder.last_dt or datetime.now()))
        if isinstance(tick, TickData):
            tick.dt = now
        elif isinstance(tick, dict) and "dt" not in tick:
            tick = dict(tick, dt=now)

        # 2. Dispatch finalized bar FIRST on minute rollover so bar M settles before tick M+1
        if completed_bar is not None:
            for bcb in self._bar_callbacks:
                try:
                    res = _safe_dispatch_bar(bcb, symbol, completed_bar)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as e:
                    logger.error(f"Error in bar callback for {symbol}: {e}", exc_info=True)

        # 3. Dispatch immediate tick-level risk callback
        for tcb in self._tick_callbacks:
            try:
                res = _safe_dispatch_tick(tcb, symbol, tick, now)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.error(f"Error in tick callback for {symbol}: {e}")

        return completed_bar

    def flush(self) -> List[MinuteBar]:
        """Flushes all currently open bars."""
        flushed = []
        for builder in self.builders.values():
            bar = builder.flush_bar()
            if bar:
                flushed.append(bar)
        return flushed
