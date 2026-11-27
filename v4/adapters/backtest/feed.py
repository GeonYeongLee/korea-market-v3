"""
Deterministic Minute-Bar Realtime Feed Adapter for Korea Market v4 Backtesting.
Implements `IRealtimeFeedPort` by driving `BacktestConditionSearchAdapter`,
emitting `MinutePulseEvent` -> `BarCloseEvent` in `(dt ASC, turnover DESC, symbol ASC)`.
"""
from __future__ import annotations

import os
from collections import OrderedDict
from datetime import date, datetime, time, timedelta
from typing import AsyncIterator, Callable, List, Optional, Set

from v4.adapters.backtest.condition_emulator import BacktestConditionSearchAdapter
from v4.adapters.backtest.parquet_loader import (
    CurrentDayStore,
    list_trading_dates,
    parse_date_arg,
    resolve_data_dir,
)
from v4.interfaces import IRealtimeFeedPort
from v4.market_models import shift_from_0900
from v4.models import BarCloseEvent, MarketEvent, MinutePulseEvent


class BacktestRealtimeFeed(IRealtimeFeedPort):
    """Unified event pump streaming parquet 1m bars as `MinutePulseEvent` + `BarCloseEvent`."""

    def __init__(
        self,
        data_dir: Optional[str] = None,
        start_date: Optional[date | str] = None,
        end_date: Optional[date | str] = None,
        condition_emulator: Optional[BacktestConditionSearchAdapter] = None,
        day_store: Optional[CurrentDayStore] = None,
        symbols: Optional[Set[str]] = None,
        max_subscriptions: Optional[int] = None,
        protected_symbols_fn: Optional[Callable[[], Set[str]]] = None,
        should_continue_day_fn: Optional[Callable[[datetime], bool]] = None,
        scan_cutoff_time: time = time(9, 20),
        shift_scan_cutoff: bool = True,
    ) -> None:
        self.data_dir = resolve_data_dir(data_dir)
        self.start_date = parse_date_arg(start_date)
        self.end_date = parse_date_arg(end_date)
        self.condition_emulator = condition_emulator
        self.day_store = day_store or (condition_emulator.day_store if condition_emulator else CurrentDayStore())
        self.symbols = set(symbols) if symbols else None
        self.max_subscriptions = max_subscriptions
        self.protected_symbols_fn = protected_symbols_fn
        self.should_continue_day_fn = should_continue_day_fn
        self.scan_cutoff_time = scan_cutoff_time
        self.shift_scan_cutoff = shift_scan_cutoff

        self._subscriptions: OrderedDict[str, Optional[bool]] = OrderedDict()
        self._stopped: bool = False

    async def start(self) -> None:
        self._stopped = False

    async def stop(self) -> None:
        self._stopped = True
        self._subscriptions.clear()

    async def subscribe(self, symbol: str, is_nxt: Optional[bool] = None) -> bool:
        if symbol in self._subscriptions:
            if is_nxt is not None:
                self._subscriptions[symbol] = is_nxt
            return True

        if self.max_subscriptions is not None and len(self._subscriptions) >= self.max_subscriptions:
            protected = self.protected_symbols_fn() if self.protected_symbols_fn is not None else set()
            evict_target: Optional[str] = None
            for existing in self._subscriptions:
                if existing not in protected:
                    evict_target = existing
                    break
            if evict_target is None:
                return False
            del self._subscriptions[evict_target]

        self._subscriptions[symbol] = is_nxt
        return True

    async def unsubscribe(self, symbol: str) -> None:
        self._subscriptions.pop(symbol, None)

    def active_symbols(self) -> List[str]:
        return list(self._subscriptions.keys())

    async def stream_events(
        self, start_date: Optional[date] = None, end_date: Optional[date] = None
    ) -> AsyncIterator[MarketEvent]:
        eff_start = parse_date_arg(start_date) if start_date is not None else self.start_date
        eff_end = parse_date_arg(end_date) if end_date is not None else self.end_date
        dates = list_trading_dates(self.data_dir, eff_start, eff_end)

        for day_i, d in enumerate(dates):
            if self._stopped:
                break
            if day_i > 0:
                self._subscriptions.clear()

            day_dir = os.path.join(self.data_dir, d.strftime("%Y%m%d"))
            day_idx = self.day_store.get_or_load(day_dir, d, symbols=self.symbols)
            if not day_idx.sorted_minutes:
                continue

            if self.condition_emulator is not None:
                await self.condition_emulator.prepare_day(d, day_idx)

            m_open = day_idx.market_open_time
            m_close = day_idx.market_close_time
            eff_scan_end = (
                shift_from_0900(self.scan_cutoff_time, m_open) if self.shift_scan_cutoff else self.scan_cutoff_time
            )
            close_auction_start = min(time(15, 20), m_close)

            cur_dt = datetime.combine(d, m_open)
            end_dt = datetime.combine(d, m_close)
            emitted_any = False

            while cur_dt <= end_dt:
                if self._stopped:
                    break
                if emitted_any and self.should_continue_day_fn is not None and not self.should_continue_day_fn(cur_dt):
                    break

                if self.condition_emulator is not None:
                    self.condition_emulator.advance_minute(day_idx, cur_dt)

                cur_t = cur_dt.time()
                in_scan = cur_t <= eff_scan_end
                has_active = bool(self._subscriptions)
                in_auction = cur_t >= close_auction_start
                is_end = cur_t >= m_close

                if in_scan or has_active or in_auction:
                    emitted_any = True
                    yield MinutePulseEvent(dt=cur_dt, is_minute_boundary=True, is_session_end=is_end)

                if self._subscriptions:
                    start_i, end_i = day_idx.get_minute_range(cur_dt)
                    for idx in range(start_i, end_i):
                        sym = day_idx.syms[idx]
                        if sym not in self._subscriptions:
                            continue
                        if day_idx.is_pms[idx] or day_idx.dts[idx].time() < m_open:
                            continue
                        emitted_any = True
                        yield BarCloseEvent(symbol=sym, dt=cur_dt, bar=day_idx.bar(idx))

                cur_dt += timedelta(minutes=1)
