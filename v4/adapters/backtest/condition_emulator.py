"""
Causal Backtest HTS Condition Search & Snapshot Emulator for Korea Market v4.
Implements `IConditionSearchPort` with zero lookahead, strict 08:00 premarket
isolation for `day_open`/`krx_day_low`, and `seq="0"` vs `seq="1"` surge semantics.
"""
from __future__ import annotations

import math
import os
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional

from v4.adapters.backtest._causal_state import CausalDayState
from v4.adapters.backtest.parquet_loader import CurrentDayStore, DayBarIndex, resolve_data_dir
from v4.interfaces import IConditionSearchPort, IRegimeDataPort
from v4.market_models import shift_from_0900
from v4.models import ConditionCandidate, ExecutionMode, IntradaySnapshot, MinuteBar


class BacktestConditionSearchAdapter(IConditionSearchPort):
    """Causal 1m parquet emulator for KIS HTS condition search (`HHKST03900400`) & snapshots."""

    def __init__(
        self,
        regime_data: Optional[IRegimeDataPort] = None,
        execution_mode: ExecutionMode = "pessimistic",
        min_gap_pct: Optional[float] = 0.000,
        max_gap_pct: Optional[float] = 0.060,
        max_mae_pct: Optional[float] = -0.012,
        min_surge_pct: float = 0.035,
        data_dir: Optional[str] = None,
        day_store: Optional[CurrentDayStore] = None,
        optimistic_prior_mae: bool = True,
    ) -> None:
        self.regime_data = regime_data
        self.execution_mode: ExecutionMode = execution_mode
        self.min_gap_pct = min_gap_pct
        self.max_gap_pct = max_gap_pct
        self.max_mae_pct = -abs(max_mae_pct) if max_mae_pct is not None else None
        self.min_surge_pct = float(min_surge_pct)
        self.data_dir = resolve_data_dir(data_dir)
        self.day_store = day_store or CurrentDayStore()
        self.optimistic_prior_mae = optimistic_prior_mae

        self._state = CausalDayState()
        self._meta_date: Optional[date] = None
        self._prev_closes: Dict[str, float] = {}
        self._nxt_flags: Dict[str, Optional[bool]] = {}

    def set_execution_mode(self, mode: ExecutionMode) -> None:
        self.execution_mode = mode

    def set_day_metadata(
        self,
        trade_date: date,
        prev_closes: Optional[Dict[str, float]] = None,
        nxt_flags: Optional[Dict[str, Optional[bool]]] = None,
    ) -> None:
        """Directly registers per-day prev_close & NXT eligibility metadata (useful in tests)."""
        self._meta_date = trade_date
        self._prev_closes = dict(prev_closes) if prev_closes else {}
        self._nxt_flags = dict(nxt_flags) if nxt_flags else {}

    async def prepare_day(self, trade_date: date, day_idx: Optional[DayBarIndex] = None) -> None:
        """Initializes day index and loads causal prev_close / NXT metadata."""
        if self._state.current_date != trade_date or (day_idx is not None and self._state.day_idx is not day_idx):
            self._state.reset(trade_date, day_idx)
        await self._ensure_day_metadata(trade_date)

    async def _ensure_day_metadata(self, trade_date: date) -> None:
        if self._meta_date == trade_date:
            return
        self._meta_date = trade_date
        if self.regime_data is None:
            self._prev_closes = {}
            self._nxt_flags = {}
            return
        pcs = await self.regime_data.get_prev_closes(trade_date)
        feats = await self.regime_data.get_symbol_features(trade_date)
        self._prev_closes = dict(pcs)
        self._nxt_flags = {sym: f.is_nxt_eligible for sym, f in feats.items()}

    def advance_minute(self, day_idx: DayBarIndex, minute_dt: datetime) -> None:
        """Advances causal emulator state forward up to `minute_dt` (inclusive)."""
        self._state.advance_minute(day_idx, minute_dt)

    async def _sync_causal_to(self, as_of: datetime) -> None:
        d = as_of.date()
        target_min = as_of.replace(second=0, microsecond=0)
        await self._ensure_day_metadata(d)

        st = self._state
        if st.day_idx is None or st.current_date != d:
            day_dir = os.path.join(self.data_dir, d.strftime("%Y%m%d"))
            idx = self.day_store.get_or_load(day_dir, d) if os.path.isdir(day_dir) else DayBarIndex(trade_date=d)
            st.reset(d, idx)

        if st.current_min is not None and target_min < st.current_min:
            st.reset(d, st.day_idx)
        if st.day_idx is not None and (st.current_min is None or target_min > st.current_min):
            st.advance_minute(st.day_idx, target_min)

    def _bar_surged(self, bar: Optional[MinuteBar], min_time: Optional[time] = None) -> bool:
        if bar is None or bar.open <= 0.0 or bar.volume <= 0:
            return False
        if min_time is not None and bar.time < min_time:
            return False
        return (bar.high - bar.open) / bar.open >= self.min_surge_pct

    def _build_candidate(
        self, sym: str, m_dt: datetime, as_of: datetime, seq: str,
        day_open: float, prev_close: float, krx_day_low: float, prior_low: float,
        curr_bar: Optional[MinuteBar], curr_ok: bool, prev_ok: bool,
    ) -> ConditionCandidate:
        st = self._state
        cur_p = curr_bar.close if curr_bar is not None else st.latest_reg_price.get(sym, day_open)
        chgrate = ((cur_p - prev_close) / prev_close * 100.0) if (prev_close > 0.0 and cur_p > 0.0) else 0.0
        return ConditionCandidate(
            symbol=sym, name=sym, day_open=day_open, krx_day_low=krx_day_low,
            prior_krx_day_low=prior_low, krx_cum_turnover=st.cum_turnover.get(sym, 0.0),
            current_price=cur_p, prev_close=prev_close,
            bar_open=curr_bar.open if curr_bar is not None else 0.0,
            bar_high=curr_bar.high if curr_bar is not None else 0.0,
            bar_low=curr_bar.low if curr_bar is not None else 0.0,
            bar_close=curr_bar.close if curr_bar is not None else 0.0,
            bar_volume=curr_bar.volume if curr_bar is not None else 0,
            bar_turnover=curr_bar.turnover if curr_bar is not None else 0.0,
            bar_dt=curr_bar.dt if curr_bar is not None else m_dt,
            curr_bar_surge_ok=curr_ok, prev_bar_surge_ok=prev_ok,
            is_nxt_eligible=self._nxt_flags.get(sym), seq=seq, as_of=as_of, chgrate=chgrate,
        )

    async def poll_candidates(self, as_of: datetime, seq: str = "0") -> List[ConditionCandidate]:
        await self._sync_causal_to(as_of)
        st = self._state
        m_dt = as_of.replace(second=0, microsecond=0)
        m_open = st.day_idx.market_open_time if st.day_idx else time(9, 0)
        open_plus_1m = shift_from_0900(time(9, 1), m_open)
        prev_m_dt = m_dt - timedelta(minutes=1)

        curr_syms = st.min_reg_syms.get(m_dt, set())
        cand_syms = curr_syms if seq == "0" else (curr_syms | st.min_reg_syms.get(prev_m_dt, set()))
        if not cand_syms:
            return []

        out: List[ConditionCandidate] = []
        for sym in cand_syms:
            day_open = st.day_open.get(sym, 0.0)
            if day_open <= 0.0:
                continue
            prev_close = self._prev_closes.get(sym, 0.0)
            if prev_close > 0.0 and (self.min_gap_pct is not None or self.max_gap_pct is not None):
                gap = (day_open - prev_close) / prev_close
                if not math.isfinite(gap) or (self.min_gap_pct is not None and gap < self.min_gap_pct) or (
                    self.max_gap_pct is not None and gap > self.max_gap_pct
                ):
                    continue

            krx_day_low = st.krx_day_low.get(sym, 0.0)
            prior_low = st.prior_low_before_min.get(sym, 0.0) if st.last_reg_min.get(sym) == m_dt else krx_day_low
            mae_low = (prior_low if prior_low > 0.0 else day_open) if (self.execution_mode == "optimistic" and self.optimistic_prior_mae) else krx_day_low
            if self.max_mae_pct is not None and mae_low > 0.0:
                mae = (mae_low - day_open) / day_open
                if not math.isfinite(mae) or mae < self.max_mae_pct:
                    continue

            bars = st.reg_bars_by_sym.get(sym, ())
            curr_bar = bars[-1] if (bars and bars[-1].dt == m_dt) else None
            prev_bar = bars[-1] if (bars and bars[-1].dt == prev_m_dt) else (
                bars[-2] if (len(bars) >= 2 and bars[-2].dt == prev_m_dt) else None
            )
            curr_ok = self._bar_surged(curr_bar)
            prev_ok = self._bar_surged(prev_bar, min_time=open_plus_1m)
            if (seq == "0" and not curr_ok) or (seq != "0" and not (curr_ok or prev_ok)):
                continue

            out.append(
                self._build_candidate(
                    sym, m_dt, as_of, seq, day_open, prev_close, krx_day_low, prior_low, curr_bar, curr_ok, prev_ok
                )
            )

        out.sort(key=lambda c: (-c.krx_cum_turnover, c.symbol))
        return out

    async def get_intraday_snapshot(self, symbol: str, as_of: datetime) -> Optional[IntradaySnapshot]:
        await self._sync_causal_to(as_of)
        st = self._state
        cum_to = st.cum_turnover.get(symbol, 0.0)
        cum_vol = st.cum_volume.get(symbol, 0)
        day_open = st.day_open.get(symbol, 0.0)
        pm_to = st.pm_turnover.get(symbol, 0.0)
        if day_open <= 0.0 and cum_to <= 0.0 and cum_vol <= 0 and pm_to <= 0.0:
            return None

        nxt_flag = self._nxt_flags.get(symbol)
        is_nxt = bool(nxt_flag) if nxt_flag is not None else (pm_to > 0.0)
        return IntradaySnapshot(
            symbol=symbol,
            as_of=as_of,
            day_open=day_open,
            day_low=st.krx_day_low.get(symbol, 0.0),
            day_high=st.krx_day_high.get(symbol, 0.0),
            current_price=st.latest_reg_price.get(symbol, st.latest_price.get(symbol, 0.0)),
            cum_turnover=cum_to,
            cum_volume=cum_vol,
            is_nxt_eligible=is_nxt,
            source="parquet",
            prev_close=self._prev_closes.get(symbol, 0.0),
            premarket_low=st.pm_low.get(symbol, 0.0),
            premarket_turnover=pm_to,
        )

    async def get_recent_minute_bars(
        self, symbol: str, as_of: datetime, limit: int = 30
    ) -> List[MinuteBar]:
        await self._sync_causal_to(as_of)
        if limit <= 0:
            return []
        bars = self._state.reg_bars_by_sym.get(symbol, [])
        causal = [b for b in bars if b.dt <= as_of and b.volume > 0]
        return causal[-limit:]
