"""
Morning Leader Scanner.
Identifies surging morning market leaders between 09:00 and 09:03 based on:
  1. Real-time KIS HTS Condition Search (TR: HHKST03900400, seq="0") in live trading, or all symbols in backtest
  2. Pre-breakout MAE guard (low >= -1.5% from day open; permanently disqualified if breached)
  3. Cumulative turnover >= 1,000,000,000 KRW (10억 원)
  4. Bar high surge >= +3.5% ((high - open) / open >= 0.035) with 0% lookahead
"""
from __future__ import annotations

import math
import os
from datetime import date, time
from typing import Optional, List, Dict, Set
import numpy as np

from core.dynamic_features import DynamicFeatureProvider, get_default_dynamic_feature_provider, SymbolDynamicFeatures
from core.telemetry import telemetry
from engine.models import MinuteBar
from strategy.base import IScanner


class MorningLeaderScanner(IScanner):
    """
    Screens for top morning momentum leaders between 09:01:00 and 09:03:00.
    Supports KIS HTS Condition Search API (TR: HHKST03900400) for real-time live execution.
    """

    name: str = "morning_leader_scanner"

    def __init__(
        self,
        min_cum_turnover: float = 1_000_000_000.0,  # 누적 10억 원 이상
        min_body_pct: float = 0.035,                # +3.5% 이상 고가 급등폭 ((high - open) / open >= 0.035)
        max_mae_pct: Optional[float] = -0.015,      # 시초가 대비 최대 눌림폭 -1.5% (초과 시 영구 탈락)
        min_gap_pct: Optional[float] = 0.000,       # 0.0% 이상 시초 갭상승 (마이너스 갭다운 차단)
        max_gap_pct: Optional[float] = 0.100,       # +10.0% 이하 시초 갭상승 (10% 이상 과열 갭업 차단)
        daily_file: str = "data/dataset_2y/daily_bars_2y.parquet",
        start_time: time = time(9, 1),
        end_time: time = time(9, 3),
        condition_name: str = "모닝_돌파주도주",
        condition_seq: str = "0",
        use_dynamic_turnover: bool = True,
        feature_provider: Optional[DynamicFeatureProvider] = None,
    ):
        self.min_cum_turnover = min_cum_turnover
        self.min_body_pct = min_body_pct
        self.max_mae_pct = -abs(max_mae_pct) if max_mae_pct is not None else None
        self.min_gap_pct = min_gap_pct
        self.max_gap_pct = max_gap_pct
        self.daily_file = daily_file
        self.start_time = start_time
        self.end_time = end_time
        self.condition_name = condition_name
        self.condition_seq = condition_seq
        self.condition_tr: str = "HHKST03900400"
        self.use_dynamic_turnover = use_dynamic_turnover
        self.feature_provider = feature_provider

        self._day_open_prices: Dict[str, float] = {}
        self._day_low_prices: Dict[str, float] = {}
        self._disqualified_symbols: Set[str] = set()
        self._qualified_symbols: Set[str] = set()
        self._condition_symbols: Optional[Set[str]] = None
        self._prev_close_lookup: Dict[str, Dict[str, float]] = {}
        self._day_prev_closes: Dict[str, float] = {}
        self._day_features: Dict[str, SymbolDynamicFeatures] = {}

    def is_disqualified(self, symbol: str) -> bool:
        """Returns True if the symbol has been permanently disqualified today."""
        return symbol in self._disqualified_symbols

    def _load_prev_close_cache(self) -> None:
        if self._prev_close_lookup:
            return
        if os.path.exists(self.daily_file):
            try:
                import pandas as pd
                df = pd.read_parquet(self.daily_file, columns=["date", "symbol", "prev_close"])
                df["date_str"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
                for d_str, g in df.groupby("date_str"):
                    self._prev_close_lookup[d_str] = dict(zip(g["symbol"], g["prev_close"]))
            except Exception:
                pass

    def feed_condition_search_symbol(self, symbol: str) -> None:
        """
        External hook for KIS HTS Condition Search real-time alerts (TR: HHKST03900400).
        Dynamically adds the symbol to the real-time condition universe.
        """
        if self._condition_symbols is None:
            self._condition_symbols = set()
        self._condition_symbols.add(symbol)

    def set_condition_universe(self, symbols: List[str]) -> None:
        """
        Initializes or replaces candidate universe from KIS HTS condition search results.
        """
        self._condition_symbols = set(symbols)

    def filter_universe(self, current_date: date) -> Optional[List[str]]:
        if self._condition_symbols is not None:
            return list(self._condition_symbols)
        return None

    def on_day_open(self, current_date: date) -> None:
        self._day_open_prices.clear()
        self._day_low_prices.clear()
        self._disqualified_symbols.clear()
        self._qualified_symbols.clear()
        if self._condition_symbols is not None:
            self._condition_symbols.clear()
        if self.use_dynamic_turnover:
            provider = self.feature_provider or get_default_dynamic_feature_provider()
            self._day_features = provider.get_day_stock_features(current_date)
        else:
            self._day_features.clear()

        if (self.min_gap_pct is not None or self.max_gap_pct is not None) and not self._prev_close_lookup:
            self._load_prev_close_cache()
        curr_d_str = current_date.strftime("%Y-%m-%d") if hasattr(current_date, "strftime") else str(current_date)
        self._day_prev_closes = self._prev_close_lookup.get(curr_d_str, {})

    def get_day_open(self, symbol: str) -> Optional[float]:
        """Returns registered day open price for symbol."""
        return self._day_open_prices.get(symbol)

    def on_bar(self, current_date: date, bar: MinuteBar) -> Optional[List[str]]:
        # 1. Extreme numerical anomaly defense
        if not (math.isfinite(bar.open) and math.isfinite(bar.high) and math.isfinite(bar.low) and math.isfinite(bar.close)):
            return None
        if bar.open <= 0.0 or bar.close <= 0.0 or bar.high <= 0.0 or bar.low <= 0.0:
            return None
        if bar.high < bar.low:
            return None
        if bar.turnover < 0.0 or bar.cum_turnover < 0.0 or bar.volume < 0:
            return None

        # 2. Track day open & day low (always from regular market open >= 09:00:00)
        # CRITICAL: Recorded BEFORE condition universe check and time window checks.
        # This prevents mid-morning condition search discovery (e.g. at 09:02) from
        # overriding the true 09:00:00 market open price.
        if bar.time >= time(9, 0):
            if bar.symbol not in self._day_open_prices:
                self._day_open_prices[bar.symbol] = bar.open
                self._day_low_prices[bar.symbol] = bar.low
            else:
                self._day_low_prices[bar.symbol] = min(self._day_low_prices[bar.symbol], bar.low)

        # 3. Early return for out-of-window bars
        # Eliminates redundant loop executions in backtests and live afternoon trading.
        if bar.time < self.start_time or bar.time > self.end_time:
            return None

        # 4. Condition search filter (live trading mode)
        if self._condition_symbols is not None and bar.symbol not in self._condition_symbols:
            return None

        # 5. Permanently disqualified check
        if bar.symbol in self._disqualified_symbols:
            return None

        # 6. Gap guard & Pre-breakout MAE Guard
        day_open = self._day_open_prices.get(bar.symbol, 0.0)
        vwap = round(bar.turnover / bar.volume, 2) if bar.volume > 0 else bar.close
        gap = 0.0
        mae = 0.0

        if day_open > 0.0:
            # Morning Opening Gap Guard (+0.0% ~ +10.0%)
            if (self.min_gap_pct is not None or self.max_gap_pct is not None) and self._day_prev_closes:
                pc = self._day_prev_closes.get(bar.symbol)
                if pc and pc > 0.0:
                    gap = (day_open - pc) / pc
                    if math.isnan(gap) or not math.isfinite(gap):
                        return None
                    if (self.min_gap_pct is not None and gap < self.min_gap_pct) or (self.max_gap_pct is not None and gap > self.max_gap_pct):
                        self._disqualified_symbols.add(bar.symbol)
                        self._qualified_symbols.discard(bar.symbol)
                        telemetry.record_strategy_eval(
                            "SCANNER", bar.symbol, "REJECT", f"FAIL_GAP_LIMIT ({gap:+.2%})",
                            {"day_open": day_open, "prev_close": pc, "gap_pct": round(gap * 100, 2), "vwap": vwap},
                            dt=bar.dt
                        )
                        return None

            # Pre-breakout MAE Guard: drop > max_mae permanently disqualifies the stock for the day
            if self.max_mae_pct is not None:
                mae = (self._day_low_prices[bar.symbol] - day_open) / day_open
                if math.isnan(mae) or not math.isfinite(mae) or mae < self.max_mae_pct:
                    self._disqualified_symbols.add(bar.symbol)
                    self._qualified_symbols.discard(bar.symbol)
                    telemetry.record_strategy_eval(
                        "SCANNER", bar.symbol, "REJECT", f"FAIL_MAE_BREACH ({mae:+.2%})",
                        {"day_open": day_open, "day_low": self._day_low_prices[bar.symbol], "mae_pct": round(mae * 100, 2), "vwap": vwap},
                        dt=bar.dt
                    )
                    return None

        # 7. High surge percent check ((high - open) / open >= +3.5%) with NaN/Inf defense (Lookahead 0%)
        surge_pct = (bar.high - bar.open) / bar.open
        if math.isnan(surge_pct) or not math.isfinite(surge_pct) or surge_pct < self.min_body_pct:
            if telemetry.audit_enabled and surge_pct >= 0.02:
                telemetry.record_strategy_eval(
                    "SCANNER", bar.symbol, "REJECT", f"FAIL_SURGE_PCT ({surge_pct:+.2%})",
                    {"high": bar.high, "open": bar.open, "surge_pct": round(surge_pct * 100, 2), "vwap": vwap},
                    dt=bar.dt
                )
            return None

        # 8. Turnover criteria (Dynamic: clip(0.08 * adto20, 500M, 2.5B) or fixed fallback)
        eff_min_turnover = self.min_cum_turnover
        if self.use_dynamic_turnover and bar.symbol in self._day_features:
            adto = self._day_features[bar.symbol].prior_adto20
            if adto > 0.0:
                eff_min_turnover = float(np.clip(0.08 * adto, 500_000_000.0, 2_500_000_000.0))

        if bar.cum_turnover < eff_min_turnover:
            telemetry.record_strategy_eval(
                "SCANNER", bar.symbol, "REJECT", f"FAIL_TURNOVER_THRESHOLD ({bar.cum_turnover/1e8:.1f}억 < {eff_min_turnover/1e8:.1f}억)",
                {"cum_turnover": bar.cum_turnover, "eff_min_turnover": eff_min_turnover, "surge_pct": round(surge_pct * 100, 2), "vwap": vwap},
                dt=bar.dt
            )
            return None

        telemetry.record_strategy_eval(
            "SCANNER", bar.symbol, "PASS", "PASS_SCANNER_CONDITIONS",
            {
                "day_open": day_open, "high": bar.high, "surge_pct": round(surge_pct * 100, 2),
                "cum_turnover": bar.cum_turnover, "eff_min_turnover": eff_min_turnover, "vwap": vwap,
            },
            dt=bar.dt
        )
        self._qualified_symbols.add(bar.symbol)
        return list(self._qualified_symbols)
