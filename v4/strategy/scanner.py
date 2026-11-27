"""
All-Weather Unified Scanner for Korea Market v4.
Pure synchronous IScannerComponent with zero disk I/O and zero Lookahead.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time
from typing import Dict, List, Optional, Set

from v4.interfaces import IScannerComponent
from v4.market_models import shift_from_0900
from v4.models import ConditionCandidate, ExecutionMode, IntradaySnapshot, MinuteBar, SymbolFeatures


class AllWeatherScannerV4(IScannerComponent):
    """
    Morning momentum leader scanner:
      - Gap in [0.0%, +6.0%], Regular KRX MAE >= -1.2%, 1m High Surge >= +3.5%
      - Dynamic turnover >= clip(0.08 * prior_adto20, 500M, 2.5B) (fallback 1.0B)
    """

    name: str = "all_weather_scanner"

    def __init__(
        self,
        min_cum_turnover: float = 1_000_000_000.0,
        min_body_pct: float = 0.035,
        max_mae_pct: Optional[float] = -0.012,
        min_gap_pct: Optional[float] = 0.000,
        max_gap_pct: Optional[float] = 0.060,
        start_time: time = time(9, 1),
        end_time: time = time(9, 15),
        use_dynamic_turnover: bool = True,
        shift_to_market_open: bool = True,
        execution_mode: ExecutionMode = "pessimistic",
        optimistic_prior_mae: bool = True,
    ) -> None:
        self.min_cum_turnover = min_cum_turnover
        self.min_body_pct = min_body_pct
        self.max_mae_pct = -abs(max_mae_pct) if max_mae_pct is not None else None
        self.min_gap_pct, self.max_gap_pct = min_gap_pct, max_gap_pct
        self.base_start_time, self.base_end_time = start_time, end_time
        self.start_time, self.end_time = start_time, end_time
        self.use_dynamic_turnover = use_dynamic_turnover
        self.shift_to_market_open = shift_to_market_open
        self.execution_mode: ExecutionMode = execution_mode
        self.optimistic_prior_mae = optimistic_prior_mae
        self.market_open_time, self.market_close_time = time(9, 0), time(15, 30)

        self._day_open_prices: Dict[str, float] = {}
        self._day_low_prices: Dict[str, float] = {}
        self._last_low_min: Dict[str, datetime] = {}
        self._disqualified_symbols: Set[str] = set()
        self._qualified_symbols: Set[str] = set()
        self._condition_symbols: Optional[Set[str]] = None
        self._day_prev_closes: Dict[str, float] = {}
        self._day_features: Dict[str, SymbolFeatures] = {}

    def on_day_open(
        self,
        current_date: date,
        prev_closes: Optional[Dict[str, float]] = None,
        features: Optional[Dict[str, SymbolFeatures]] = None,
        market_open_time: time = time(9, 0),
        market_close_time: time = time(15, 30),
        execution_mode: Optional[ExecutionMode] = None,
    ) -> None:
        self._day_open_prices.clear()
        self._day_low_prices.clear()
        self._last_low_min.clear()
        self._disqualified_symbols.clear()
        self._qualified_symbols.clear()
        if self._condition_symbols is not None:
            self._condition_symbols.clear()
        self.market_open_time, self.market_close_time = market_open_time, market_close_time
        if execution_mode is not None:
            self.execution_mode = execution_mode
        if self.shift_to_market_open:
            self.start_time = shift_from_0900(self.base_start_time, market_open_time)
            self.end_time = shift_from_0900(self.base_end_time, market_open_time)
        else:
            self.start_time, self.end_time = self.base_start_time, self.base_end_time
        self._day_prev_closes = dict(prev_closes) if prev_closes else {}
        self._day_features = dict(features) if features else {}

    def is_disqualified(self, symbol: str) -> bool: return symbol in self._disqualified_symbols
    def get_day_open(self, symbol: str) -> Optional[float]: return self._day_open_prices.get(symbol)
    def get_prev_close(self, symbol: str) -> Optional[float]: return self._day_prev_closes.get(symbol)
    def set_condition_universe(self, symbols: List[str]) -> None: self._condition_symbols = set(symbols)

    def feed_condition_search_symbol(self, symbol: str) -> None:
        if self._condition_symbols is None:
            self._condition_symbols = set()
        self._condition_symbols.add(symbol)

    def filter_universe(self, current_date: date) -> Optional[List[str]]:
        return list(self._condition_symbols) if self._condition_symbols is not None else None

    def register_day_open(
        self, symbol: str, day_open: float, krx_day_low: float = 0.0, prev_close: float = 0.0
    ) -> None:
        if prev_close > 0.0:
            self._day_prev_closes[symbol] = float(prev_close)
        if day_open > 0.0:
            self._day_open_prices.setdefault(symbol, float(day_open))
            eff_low = float(krx_day_low) if krx_day_low > 0.0 else float(day_open)
            prev = self._day_low_prices.get(symbol, 0.0)
            self._day_low_prices[symbol] = min(prev, eff_low) if prev > 0.0 else eff_low

    def effective_min_turnover(self, symbol: str) -> float:
        if self.use_dynamic_turnover and symbol in self._day_features:
            adto = self._day_features[symbol].prior_adto20
            if adto > 0.0 and math.isfinite(adto):
                return min(2_500_000_000.0, max(500_000_000.0, 0.08 * adto))
        return self.min_cum_turnover

    def check_live_tick_mae(self, symbol: str, day_open: float, krx_day_low: float) -> bool:
        """Checks regular-session KRX MAE on live ticks; disqualifies on breach."""
        if self.max_mae_pct is None or day_open <= 0.0 or krx_day_low <= 0.0:
            return False
        mae = (krx_day_low - day_open) / day_open
        if not math.isfinite(mae) or mae < self.max_mae_pct:
            self._disqualified_symbols.add(symbol)
            self._qualified_symbols.discard(symbol)
            return True
        return False

    def _validate_and_finalize(
        self,
        symbol: str,
        eval_dt: datetime,
        day_open: float,
        prev_close: float,
        prior_low: float,
        post_low: float,
        surge_pct: Optional[float],
        surge_flag: bool,
        cum_turnover: float,
    ) -> bool:
        """Single canonical qualification & post-bar MAE record shared by on_bar() & evaluate_candidate()."""
        use_opt_prior = self.execution_mode == "optimistic" and self.optimistic_prior_mae
        mae_low = (prior_low if prior_low > 0.0 else day_open) if use_opt_prior else post_low
        passed = self._core_filter(symbol, eval_dt, day_open, prev_close, mae_low, surge_pct, surge_flag, cum_turnover)
        if passed:
            self._qualified_symbols.add(symbol)
        if use_opt_prior and self.max_mae_pct is not None and day_open > 0.0 and post_low > 0.0:
            post_mae = (post_low - day_open) / day_open
            if not math.isfinite(post_mae) or post_mae < self.max_mae_pct:
                self._disqualified_symbols.add(symbol)
                self._qualified_symbols.discard(symbol)
        return passed

    def _core_filter(
        self,
        symbol: str,
        eval_dt: datetime,
        day_open: float,
        prev_close: float,
        mae_low: float,
        surge_pct: Optional[float],
        surge_flag: bool,
        cum_turnover: float,
    ) -> bool:
        if eval_dt.time() < self.start_time or eval_dt.time() > self.end_time or symbol in self._disqualified_symbols:
            return False
        if day_open > 0.0:
            if (self.min_gap_pct is not None or self.max_gap_pct is not None) and prev_close > 0.0:
                gap = (day_open - prev_close) / prev_close
                if not math.isfinite(gap) or (
                    (self.min_gap_pct is not None and gap < self.min_gap_pct)
                    or (self.max_gap_pct is not None and gap > self.max_gap_pct)
                ):
                    self._disqualified_symbols.add(symbol)
                    self._qualified_symbols.discard(symbol)
                    return False
            if self.max_mae_pct is not None and mae_low > 0.0:
                mae = (mae_low - day_open) / day_open
                if not math.isfinite(mae) or mae < self.max_mae_pct:
                    self._disqualified_symbols.add(symbol)
                    self._qualified_symbols.discard(symbol)
                    return False
        if surge_pct is not None:
            if not math.isfinite(surge_pct) or surge_pct < self.min_body_pct:
                return False
        elif not surge_flag:
            return False
        return math.isfinite(cum_turnover) and cum_turnover >= self.effective_min_turnover(symbol)

    def on_bar(self, current_date: date, bar: MinuteBar) -> Optional[List[str]]:
        nums = (bar.open, bar.high, bar.low, bar.close, bar.turnover, bar.cum_turnover)
        if not all(math.isfinite(x) for x in nums) or min(bar.open, bar.close, bar.high, bar.low) <= 0.0:
            return None
        if bar.high < bar.low or bar.turnover < 0.0 or bar.cum_turnover < 0.0 or bar.volume < 0:
            return None
        if getattr(bar, "is_premarket", False) or bar.time < self.market_open_time:
            return None

        bar_min = bar.dt.replace(second=0, microsecond=0)
        cached_low = self._day_low_prices.get(bar.symbol, 0.0)
        prior_low = cached_low if self._last_low_min.get(bar.symbol) != bar_min else 0.0
        day_open = self._day_open_prices.setdefault(bar.symbol, float(bar.open))
        post_low = min(cached_low, bar.low) if cached_low > 0.0 else float(bar.low)
        self._day_low_prices[bar.symbol], self._last_low_min[bar.symbol] = post_low, bar_min
        if self._condition_symbols is not None and bar.symbol not in self._condition_symbols:
            return None

        surge_pct = (bar.high - bar.open) / bar.open
        prev_close = self._day_prev_closes.get(bar.symbol, 0.0)
        snap = list(self._qualified_symbols)
        if self._validate_and_finalize(bar.symbol, bar.dt, day_open, prev_close, prior_low, post_low, surge_pct, False, bar.cum_turnover):
            if bar.symbol not in snap:
                snap.append(bar.symbol)
            return snap
        return None

    def evaluate_candidate(
        self,
        cand: ConditionCandidate,
        snapshot: Optional[IntradaySnapshot] = None,
        recent_bars: Optional[List[MinuteBar]] = None,
    ) -> bool:
        sym = cand.symbol
        eval_dt = cand.as_of or cand.bar_dt or (recent_bars[-1].dt if recent_bars else (snapshot.as_of if snapshot else None))
        if eval_dt is None:
            return False

        reg_bars = [
            b for b in (recent_bars or [])
            if not getattr(b, "is_premarket", False) and b.time >= self.market_open_time and b.open > 0.0
        ]
        day_open = (
            self._day_open_prices.get(sym, 0.0)
            or (snapshot.day_open if snapshot and snapshot.day_open > 0.0 else 0.0)
            or (cand.day_open if cand.day_open > 0.0 else 0.0)
            or (reg_bars[0].open if reg_bars else 0.0)
        )
        prev_close = (
            (cand.prev_close if cand.prev_close > 0.0 else 0.0)
            or (snapshot.prev_close if snapshot and snapshot.prev_close > 0.0 else 0.0)
            or self._day_prev_closes.get(sym, 0.0)
        )
        if prev_close > 0.0:
            self._day_prev_closes[sym] = prev_close
        if day_open > 0.0:
            self._day_open_prices.setdefault(sym, float(day_open))

        curr_min = eval_dt.replace(second=0, microsecond=0)
        cached_low = self._day_low_prices.get(sym, 0.0)
        prior_bar_lows = [b.low for b in reg_bars if b.low > 0.0 and b.dt.replace(second=0, microsecond=0) < curr_min]
        prior_cands = [x for x in (cand.prior_krx_day_low, *prior_bar_lows) if x and x > 0.0]
        if not prior_cands and cached_low > 0.0 and self._last_low_min.get(sym) != curr_min:
            prior_cands.append(cached_low)
        prior_low = min(prior_cands) if prior_cands else 0.0

        post_cands = [x for x in (cand.krx_day_low, snapshot.day_low if snapshot else 0.0, cached_low, *(b.low for b in reg_bars)) if x and x > 0.0]
        post_low = min(post_cands) if post_cands else 0.0
        if post_low > 0.0:
            self._day_low_prices[sym], self._last_low_min[sym] = post_low, curr_min

        surge_pct, surge_flag, cum_to = self._extract_surge_and_turnover(cand, snapshot, reg_bars, curr_min)
        return self._validate_and_finalize(sym, eval_dt, day_open, prev_close, prior_low, post_low, surge_pct, surge_flag, cum_to)

    def _extract_surge_and_turnover(
        self,
        cand: ConditionCandidate,
        snapshot: Optional[IntradaySnapshot],
        reg_bars: List[MinuteBar],
        curr_min: datetime,
    ) -> tuple[Optional[float], bool, float]:
        snap_to = snapshot.cum_turnover if snapshot else 0.0
        window_bars = [b for b in reg_bars if self.start_time <= b.time <= self.end_time]
        if window_bars:
            eff_to = self.effective_min_turnover(cand.symbol)
            last_surge, last_to = 0.0, 0.0
            for b in window_bars:
                b_min = b.dt.replace(second=0, microsecond=0)
                b_high = max(b.high, snapshot.current_price) if (snapshot and b_min == curr_min and snapshot.current_price > 0.0) else b.high
                b_to = max(b.cum_turnover, snap_to, cand.krx_cum_turnover) if b_min == curr_min else b.cum_turnover
                last_surge, last_to = (b_high - b.open) / b.open, b_to
                if last_surge >= self.min_body_pct and b_to >= eff_to:
                    return last_surge, True, b_to
            return last_surge, False, last_to
        eff_cum = max(cand.krx_cum_turnover, snap_to)
        if cand.bar_open > 0.0 and cand.bar_high > 0.0:
            return (cand.bar_high - cand.bar_open) / cand.bar_open, False, eff_cum
        surge_ok = (cand.curr_bar_surge_ok is True) or (cand.seq == "0" and cand.curr_bar_surge_ok is not False)
        return None, surge_ok, eff_cum
