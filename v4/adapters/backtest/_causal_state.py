"""
Causal Day State Accumulator for Backtest Condition Search & Snapshot Emulator.
Maintains strictly point-in-time (`<= minute_dt`) premarket and regular-session maps.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional, Set

from v4.adapters.backtest.parquet_loader import DayBarIndex
from v4.models import MinuteBar


class CausalDayState:
    """Incremental causal state accumulator over a `DayBarIndex`."""

    def __init__(self) -> None:
        self.current_date: Optional[date] = None
        self.day_idx: Optional[DayBarIndex] = None
        self.cursor_min_idx: int = 0
        self.current_min: Optional[datetime] = None

        self.day_open: Dict[str, float] = {}
        self.krx_day_low: Dict[str, float] = {}
        self.krx_day_high: Dict[str, float] = {}
        self.prior_low_before_min: Dict[str, float] = {}
        self.last_reg_min: Dict[str, datetime] = {}
        self.cum_turnover: Dict[str, float] = {}
        self.cum_volume: Dict[str, int] = {}
        self.pm_low: Dict[str, float] = {}
        self.pm_turnover: Dict[str, float] = {}
        self.latest_price: Dict[str, float] = {}
        self.latest_reg_price: Dict[str, float] = {}
        self.reg_bars_by_sym: Dict[str, List[MinuteBar]] = {}
        self.min_reg_syms: Dict[datetime, Set[str]] = {}

    def reset(self, trade_date: date, day_idx: Optional[DayBarIndex]) -> None:
        self.current_date = trade_date
        self.day_idx = day_idx
        self.cursor_min_idx = 0
        self.current_min = None
        for store in (
            self.day_open, self.krx_day_low, self.krx_day_high, self.prior_low_before_min,
            self.last_reg_min, self.cum_turnover, self.cum_volume, self.pm_low,
            self.pm_turnover, self.latest_price, self.latest_reg_price,
            self.reg_bars_by_sym, self.min_reg_syms,
        ):
            store.clear()

    def ingest_minute(self, day_idx: DayBarIndex, m_dt: datetime) -> None:
        start, end = day_idx.get_minute_range(m_dt)
        m_open = day_idx.market_open_time
        reg_syms: Optional[Set[str]] = None

        for i in range(start, end):
            sym = day_idx.syms[i]
            to_val = day_idx.tos[i]
            vol = day_idx.vols[i]
            close_p = day_idx.closes[i]
            low_p = day_idx.lows[i]
            high_p = day_idx.highs[i]
            open_p = day_idx.opens[i]

            self.cum_turnover[sym] = day_idx.cum_tos[i]
            self.cum_volume[sym] = self.cum_volume.get(sym, 0) + max(0, vol)
            if close_p > 0.0:
                self.latest_price[sym] = close_p

            if day_idx.is_pms[i] or day_idx.dts[i].time() < m_open:
                self.pm_turnover[sym] = self.pm_turnover.get(sym, 0.0) + max(0.0, to_val)
                if low_p > 0.0:
                    prev_pm = self.pm_low.get(sym, 0.0)
                    self.pm_low[sym] = min(prev_pm, low_p) if prev_pm > 0.0 else low_p
                continue

            if open_p <= 0.0 or high_p <= 0.0 or low_p <= 0.0 or close_p <= 0.0:
                continue
            self.day_open.setdefault(sym, open_p)
            if self.last_reg_min.get(sym) != m_dt:
                self.prior_low_before_min[sym] = self.krx_day_low.get(sym, 0.0)
                self.last_reg_min[sym] = m_dt
            prev_l = self.krx_day_low.get(sym, 0.0)
            self.krx_day_low[sym] = min(prev_l, low_p) if prev_l > 0.0 else low_p
            prev_h = self.krx_day_high.get(sym, 0.0)
            self.krx_day_high[sym] = max(prev_h, high_p) if prev_h > 0.0 else high_p
            self.latest_reg_price[sym] = close_p

            self.reg_bars_by_sym.setdefault(sym, []).append(day_idx.bar(i))
            if reg_syms is None:
                reg_syms = self.min_reg_syms.setdefault(m_dt, set())
            reg_syms.add(sym)

    def advance_minute(self, day_idx: DayBarIndex, minute_dt: datetime) -> None:
        """Advances causal emulator state forward up to `minute_dt` (inclusive)."""
        target_min = minute_dt.replace(second=0, microsecond=0)
        if self.current_date != day_idx.trade_date or self.day_idx is not day_idx:
            self.reset(day_idx.trade_date, day_idx)

        mins = day_idx.sorted_minutes
        n_mins = len(mins)
        while self.cursor_min_idx < n_mins and mins[self.cursor_min_idx] <= target_min:
            self.ingest_minute(day_idx, mins[self.cursor_min_idx])
            self.cursor_min_idx += 1
        self.current_min = target_min
