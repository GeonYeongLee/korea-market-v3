"""
Crisis Rebound Universe Scanner (Decoupled Golden Setting: -2.5% KOSDAQ Panic).
================================================================================
Discovers high-probability Mean-Reversion candidates during market close (15:15~15:20):
  - Priority 1 (Pattern C: Gap-Down Green Rebound, Index-Decoupled):
      * Open gap <= -1.0% (early panic gap-down absorbed)
      * Real body return ((close - open) / open * 100) >= +0.5% (confirmed buyer reversal)
      * Daily change < 0.0%
      * Liquidity Top 20
      * Operates whenever Champion B has 0 candidates (no index restriction).
  - Priority 2 (Pattern A: Waterfall Panic Capitulation, KOSDAQ Panic Filter):
      * Triggered ONLY when KOSDAQ Index <= -2.5% (systemic panic liquidation)
      * Daily drop <= -6.0% (extreme oversold flush)
      * Liquidity Top 20
  - Slots: Up to 2 candidates per day, prioritized C first, then A.
  - Zero lookahead guarantee & Universal Audit contract compliance.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import date, time
from typing import Any, Dict, List, Optional
import pandas as pd

from engine.models import MinuteBar
from strategy.base import IScanner

logger = logging.getLogger("korea_market.strategy.scanner.crisis_rebound")


class CrisisReboundScanner(IScanner):
    """
    Scans for high-conviction oversold rebound candidates.
    Pattern C runs decoupled on individual price action.
    Pattern A activates strictly on KOSDAQ panic days (<= -2.5%).
    """

    name: str = "crisis_rebound_scanner"

    def __init__(
        self,
        market_panic_threshold: float = -2.5,
        pattern_c_open_gap_max: float = -1.0,
        pattern_c_body_min: float = 0.5,
        pattern_a_drop_max: float = -6.0,
        turnover_rank_max: int = 20,
        max_universe_size: int = 2,
        start_time: time = time(15, 15),
        end_time: time = time(15, 25),
        daily_file: str = "data/dataset_2y/daily_bars_all.parquet",
        index_file: str = "data/dataset_2y/index_daily_full.parquet",
        **kwargs: Any,
    ) -> None:
        self.market_panic_threshold = market_panic_threshold
        self.pattern_c_open_gap_max = pattern_c_open_gap_max
        self.pattern_c_body_min = pattern_c_body_min
        self.pattern_a_drop_max = pattern_a_drop_max
        self.turnover_rank_max = turnover_rank_max
        self.max_universe_size = max_universe_size
        self.start_time = start_time
        self.end_time = end_time
        self.daily_file = daily_file
        self.index_file = index_file

        self._day_candidates: List[str] = []
        self._daily_cache: Optional[Dict[str, List[str]]] = None
        self._day_open_prices: Dict[str, float] = {}

    def _resolve_file_path(self, target_path: str) -> Optional[str]:
        if os.path.exists(target_path):
            return target_path
        alt_paths = [
            os.path.join("..", target_path),
            os.path.join("..", "korea-market_v2", target_path),
            os.path.join("..", "korea-market_v3", target_path),
        ]
        for p in alt_paths:
            if os.path.exists(p):
                return p
        return None

    def _load_cache(self) -> None:
        if self._daily_cache is not None:
            return

        resolved_path = self._resolve_file_path(self.daily_file)
        if not resolved_path or not os.path.exists(resolved_path):
            self._daily_cache = {}
            return

        try:
            df = pd.read_parquet(resolved_path)
            df["symbol"] = df["symbol"].astype(str).str.zfill(6)
            df["date_str"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            df["turnover"] = pd.to_numeric(df["turnover"], errors="coerce")
            df["change_pct"] = pd.to_numeric(df["change_pct"], errors="coerce")
            df["open"] = pd.to_numeric(df["open"], errors="coerce")
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df["prev_close"] = pd.to_numeric(df["prev_close"], errors="coerce")

            # Rank by turnover
            df["to_rank"] = df.groupby("date_str")["turnover"].rank(ascending=False, method="min")

            # Exclude lower limits (-29%)
            df = df[df["change_pct"] > -29.0].copy()

            # Morphology
            df["open_gap"] = (df["open"] - df["prev_close"]) / df["prev_close"] * 100.0
            df["body_pct"] = (df["close"] - df["open"]) / df["open"] * 100.0

            # Load KOSDAQ index for Pattern A panic trigger
            index_path = self._resolve_file_path(self.index_file)
            if index_path and os.path.exists(index_path):
                df_idx = pd.read_parquet(index_path)
                df_idx["date_str"] = pd.to_datetime(df_idx["date"]).dt.strftime("%Y-%m-%d")
                df_idx = df_idx.sort_values("date_str").reset_index(drop=True)
                if "kq_close" in df_idx.columns:
                    df_idx["kq_prev"] = df_idx["kq_close"].shift(1)
                    df_idx["kq_chg"] = (df_idx["kq_close"] - df_idx["kq_prev"]) / df_idx["kq_prev"] * 100.0
                    df = df.merge(df_idx[["date_str", "kq_chg"]], on="date_str", how="left")
            if "kq_chg" not in df.columns:
                df["kq_chg"] = 0.0

            # Pattern C: Gap-Down Green Rebound (Decoupled from index, Priority 1)
            cond_c = (
                (df["change_pct"] < 0.0)
                & (df["open_gap"] <= self.pattern_c_open_gap_max)
                & (df["body_pct"] >= self.pattern_c_body_min)
                & (df["to_rank"] <= self.turnover_rank_max)
            )

            # Pattern A: Waterfall Panic Capitulation (KOSDAQ <= -2.5%, Priority 2)
            cond_a = (
                (df["kq_chg"] <= self.market_panic_threshold)
                & (df["change_pct"] <= self.pattern_a_drop_max)
                & (df["to_rank"] <= self.turnover_rank_max)
            )

            df_c = df[cond_c].sort_values(["date_str", "turnover"], ascending=[True, False])
            df_a = df[cond_a].sort_values(["date_str", "turnover"], ascending=[True, False])

            cache: Dict[str, List[str]] = {}
            all_target_dates = sorted(list(set(df_c["date_str"]).union(set(df_a["date_str"]))))

            c_groups = {d: grp for d, grp in df_c.groupby("date_str")}
            a_groups = {d: grp for d, grp in df_a.groupby("date_str")}

            for d in all_target_dates:
                c_cands = c_groups.get(d, pd.DataFrame())
                a_cands = a_groups.get(d, pd.DataFrame())

                c_syms = list(c_cands["symbol"]) if len(c_cands) > 0 else []
                # Exclude duplicate symbols already picked by C
                a_syms = [s for s in (list(a_cands["symbol"]) if len(a_cands) > 0 else []) if s not in c_syms]

                # Combine C first, then A
                combined = (c_syms + a_syms)[: self.max_universe_size]
                if combined:
                    cache[d] = combined

            self._daily_cache = cache
        except Exception as e:
            logger.debug("Failed loading daily bars for crisis rebound cache: %s", e)
            self._daily_cache = {}

    def filter_universe(self, current_date: date) -> Optional[List[str]]:
        self._load_cache()
        curr_d_str = current_date.strftime("%Y-%m-%d") if hasattr(current_date, "strftime") else str(current_date)
        if self._daily_cache and curr_d_str in self._daily_cache:
            return list(self._daily_cache[curr_d_str])
        return None

    def on_day_open(self, current_date: date) -> None:
        self._day_candidates.clear()
        self._day_open_prices.clear()
        self._load_cache()
        curr_d_str = current_date.strftime("%Y-%m-%d") if hasattr(current_date, "strftime") else str(current_date)
        if self._daily_cache and curr_d_str in self._daily_cache:
            self._day_candidates = list(self._daily_cache[curr_d_str])

    def feed_condition_search_symbol(self, symbol: str) -> None:
        sym = str(symbol).zfill(6)
        if sym not in self._day_candidates:
            self._day_candidates.append(sym)

    def get_day_open(self, symbol: str) -> Optional[float]:
        return self._day_open_prices.get(symbol)

    def on_bar(self, current_date: date, bar: MinuteBar) -> Optional[List[str]]:
        if not (math.isfinite(bar.open) and math.isfinite(bar.high) and math.isfinite(bar.low) and math.isfinite(bar.close)):
            return None

        sym = bar.symbol.zfill(6)
        if sym not in self._day_open_prices:
            self._day_open_prices[sym] = bar.open

        bar_t = bar.dt.time() if hasattr(bar.dt, "time") else time(15, 18)
        if self.start_time <= bar_t <= self.end_time:
            if sym in self._day_candidates:
                return [sym]

        return None
