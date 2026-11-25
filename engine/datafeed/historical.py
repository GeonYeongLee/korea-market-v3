"""
Historical DataFeed for deterministic backtesting.
Streams 1-minute bars chronologically from the local 2-year partitioned dataset.
"""
from __future__ import annotations

import os
import glob
import logging
from datetime import datetime, date
from typing import Iterator, List, Optional, Union, Callable, Set, Tuple, Dict
import pandas as pd

from engine.models import MinuteBar, TickData
from engine.datafeed.base import IDataFeed

logger = logging.getLogger("korea_market.datafeed.historical")


class HistoricalDataFeed(IDataFeed):
    def __init__(
        self,
        data_dir: str = "data/dataset_2y/daily",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        symbols: Optional[List[str]] = None,
    ):
        self.data_dir = data_dir
        self.start_date = start_date
        self.end_date = end_date
        self.symbols = set(symbols) if symbols else None
        self.filter_provider: Optional[Callable[[datetime], Tuple[bool, Optional[Set[str]]]]] = None

    def register_filter(
        self,
        provider: Callable[[datetime], Tuple[bool, Optional[Set[str]]]],
    ) -> None:
        """Registers a coordinated event stream filter callback."""
        self.filter_provider = provider

    def stream_events(self) -> Iterator[Union[MinuteBar, TickData]]:
        if not os.path.exists(self.data_dir):
            logger.warning(f"Data directory does not exist: {self.data_dir}")
            return

        date_dirs = sorted(os.listdir(self.data_dir))
        for d_str in date_dirs:
            if not d_str.isdigit() or len(d_str) != 8:
                continue

            if self.start_date and d_str < self.start_date.replace("-", ""):
                continue
            if self.end_date and d_str > self.end_date.replace("-", ""):
                continue

            day_path = os.path.join(self.data_dir, d_str)
            if not os.path.isdir(day_path):
                continue

            yield from self._stream_day(day_path, d_str)

    def _yield_bars_from_table(
        self,
        table: Any,
        available_cols: List[str],
        cum_turnover_tracker: Dict[str, float],
        state: Dict[str, Any],
    ) -> Iterator[MinuteBar]:
        """Yields MinuteBar instances sequentially from a sorted PyArrow table."""
        if table.num_rows == 0:
            return

        import pyarrow as pa

        sort_keys = [("timestamp", "ascending")]
        if "turnover" in available_cols:
            sort_keys.append(("turnover", "descending"))
        sort_keys.append(("symbol", "ascending"))

        sorted_indices = pa.compute.sort_indices(table, sort_keys=sort_keys)
        table = table.take(sorted_indices)

        dts = table["timestamp"].to_pylist()
        syms = table["symbol"].to_pylist()
        opens = table["open"].to_pylist()
        highs = table["high"].to_pylist()
        lows = table["low"].to_pylist()
        closes = table["close"].to_pylist()
        vols = table["volume"].to_pylist()
        tos = table["turnover"].to_pylist() if "turnover" in available_cols else [0.0] * table.num_rows
        vwaps = table["vwap"].to_pylist() if "vwap" in available_cols else [0.0] * table.num_rows

        has_filter = self.filter_provider is not None

        for sym, dt_val, o, h, l, c, v, to, vw in zip(
            syms, dts, opens, highs, lows, closes, vols, tos, vwaps
        ):
            if has_filter and dt_val != state.get("last_dt"):
                state["last_dt"] = dt_val
                is_day_done, active_symbols = self.filter_provider(dt_val)
                state["is_day_done"] = is_day_done
                state["active_symbols"] = active_symbols
                if is_day_done:
                    return

            active_syms = state.get("active_symbols")
            if active_syms is not None and sym not in active_syms:
                continue

            to_val = float(to) if to is not None else 0.0
            cum_to = cum_turnover_tracker.get(sym, 0.0) + to_val
            cum_turnover_tracker[sym] = cum_to

            yield MinuteBar(
                symbol=sym,
                dt=dt_val,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=int(v),
                turnover=to_val,
                vwap=float(vw) if vw is not None else 0.0,
                cum_turnover=cum_to,
            )

    def _stream_day(self, day_path: str, date_str: str) -> Iterator[MinuteBar]:
        try:
            import pyarrow as pa
            import pyarrow.dataset as ds

            ds_obj = ds.dataset(day_path, format="parquet")
            cols = ["symbol", "timestamp", "open", "high", "low", "close", "volume", "turnover", "vwap"]
            available_cols = [c for c in cols if c in ds_obj.schema.names]

            # Fast market open time detection (e.g. 10:00 AM on CSAT/Jan 2)
            y, m, d = int(date_str[:4]), int(date_str[4:6]), int(date_str[6:])
            cutoff_h, cutoff_m = 9, 30
            try:
                files = [f for f in os.listdir(day_path) if f.endswith(".parquet")]
                if files:
                    import pyarrow.parquet as pq
                    meta = pq.read_metadata(os.path.join(day_path, files[0]))
                    for i in range(meta.row_group(0).num_columns):
                        c = meta.row_group(0).column(i)
                        if "timestamp" in c.path_in_schema:
                            if c.statistics.min.hour >= 10:
                                cutoff_h, cutoff_m = 10, 30
                            break
            except Exception:
                pass

            cutoff_dt = datetime(y, m, d, cutoff_h, cutoff_m, 0)
            ts_type = ds_obj.schema.field("timestamp").type
            cutoff_scalar = pa.scalar(cutoff_dt, type=ts_type)

            filter_morning = ds.field("timestamp") <= cutoff_scalar
            if self.symbols:
                filter_morning = filter_morning & ds.field("symbol").isin(list(self.symbols))

            table_am = ds_obj.to_table(columns=available_cols, filter=filter_morning)
            late_open_full_day = False
            if table_am.num_rows == 0:
                # Late-open fallback: load entire day once (respecting self.symbols) and skip redundant pm pass
                late_filter = ds.field("symbol").isin(list(self.symbols)) if self.symbols else None
                table_am = ds_obj.to_table(columns=available_cols, filter=late_filter)
                if table_am.num_rows == 0:
                    return
                late_open_full_day = True

            cum_turnover_tracker: Dict[str, float] = {}
            state: Dict[str, Any] = {
                "last_dt": None,
                "is_day_done": False,
                "active_symbols": None,
            }

            yield from self._yield_bars_from_table(table_am, available_cols, cum_turnover_tracker, state)

            if state["is_day_done"] or late_open_full_day:
                return

            # Coordinated afternoon pushdown: load only active held symbols past cutoff
            if self.filter_provider is not None:
                last_dt = state.get("last_dt") or cutoff_dt
                is_done, active_syms = self.filter_provider(last_dt)
                if is_done or (active_syms is not None and len(active_syms) == 0):
                    return
                if active_syms is not None:
                    filter_pm = (ds.field("timestamp") > cutoff_scalar) & ds.field("symbol").isin(list(active_syms))
                else:
                    filter_pm = ds.field("timestamp") > cutoff_scalar
                    if self.symbols:
                        filter_pm = filter_pm & ds.field("symbol").isin(list(self.symbols))
            else:
                filter_pm = ds.field("timestamp") > cutoff_scalar
                if self.symbols:
                    filter_pm = filter_pm & ds.field("symbol").isin(list(self.symbols))

            table_pm = ds_obj.to_table(columns=available_cols, filter=filter_pm)
            if table_pm.num_rows > 0:
                yield from self._yield_bars_from_table(table_pm, available_cols, cum_turnover_tracker, state)
            return

        except Exception as e:
            logger.debug(f"PyArrow stream error ({e}), falling back to pandas reader.")

        # Fallback pandas pipeline
        parquet_files = glob.glob(os.path.join(day_path, "*.parquet"))
        if not parquet_files:
            return

        dfs = []
        for p_file in parquet_files:
            sym = os.path.splitext(os.path.basename(p_file))[0]
            if self.symbols and sym not in self.symbols:
                continue
            try:
                df = pd.read_parquet(p_file)
                if not df.empty:
                    dfs.append(df)
            except Exception as e:
                logger.error(f"Error reading {p_file}: {e}")

        if not dfs:
            return

        combined = pd.concat(dfs, ignore_index=True)
        if "timestamp" in combined.columns:
            combined["dt"] = pd.to_datetime(combined["timestamp"])
        if "turnover" in combined.columns:
            sort_cols = ["dt", "turnover", "symbol"]
            ascending = [True, False, True]
        else:
            sort_cols = ["dt", "symbol"]
            ascending = [True, True]
        combined.sort_values(by=sort_cols, ascending=ascending, inplace=True)

        cols_order = ["dt", "symbol", "open", "high", "low", "close", "volume", "turnover", "vwap"]
        actual_cols = [c for c in cols_order if c in combined.columns]
        col_indices = {name: i for i, name in enumerate(actual_cols)}
        dt_idx = col_indices["dt"]
        sym_idx = col_indices["symbol"]
        open_idx = col_indices["open"]
        high_idx = col_indices["high"]
        low_idx = col_indices["low"]
        close_idx = col_indices["close"]
        vol_idx = col_indices["volume"]
        to_idx = col_indices.get("turnover")
        vwap_idx = col_indices.get("vwap")

        cum_turnover_tracker = {}
        last_dt = None
        has_filter = self.filter_provider is not None
        active_symbols: Optional[Set[str]] = None

        for row in combined[actual_cols].itertuples(index=False, name=None):
            dt_val = row[dt_idx]

            if has_filter and dt_val != last_dt:
                last_dt = dt_val
                dt_pydatetime = dt_val.to_pydatetime() if hasattr(dt_val, "to_pydatetime") else dt_val
                is_day_done, active_symbols = self.filter_provider(dt_pydatetime)
                if is_day_done:
                    break

            sym = row[sym_idx]
            if active_symbols is not None and sym not in active_symbols:
                continue

            to = float(row[to_idx]) if to_idx is not None else 0.0
            cum_to = cum_turnover_tracker.get(sym, 0.0) + to
            cum_turnover_tracker[sym] = cum_to

            dt_obj = dt_val.to_pydatetime() if hasattr(dt_val, "to_pydatetime") else dt_val
            bar = MinuteBar(
                symbol=sym,
                dt=dt_obj,
                open=float(row[open_idx]),
                high=float(row[high_idx]),
                low=float(row[low_idx]),
                close=float(row[close_idx]),
                volume=int(row[vol_idx]),
                turnover=to,
                vwap=float(row[vwap_idx]) if vwap_idx is not None else 0.0,
                cum_turnover=cum_to,
            )
            yield bar

