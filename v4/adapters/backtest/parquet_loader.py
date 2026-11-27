"""
Fast Parquet Day Loader & Pre-Indexer for Korea Market v4 Backtesting.
Reads `YYYYMMDD/*.parquet`, accumulates canonical integrated `cum_turnover`
starting from 08:00:00, separates regular-session bars (>= market_open_time),
and sorts per minute by `(dt ASC, turnover DESC, symbol ASC)`.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds

from v4.adapters.backtest._dates import list_trading_dates, parse_date_arg, resolve_data_dir
from v4.models import MinuteBar

logger = logging.getLogger("korea_market.v4.adapters.backtest.parquet_loader")


def _resolve_session_bounds(
    times: List[time], raw_pms: Optional[List[Optional[bool]]]
) -> Tuple[time, time]:
    """Determines `(market_open_time, market_close_time)` from regular-session timestamps."""
    min_reg: Optional[time] = None
    max_reg: Optional[time] = None
    t_0900, t_1400, t_1530 = time(9, 0), time(14, 0), time(15, 30)
    n = len(times)
    for i in range(n):
        t = times[i]
        pm = raw_pms[i] if raw_pms is not None else None
        is_pm = bool(pm) if pm is not None else (t < t_0900)
        if is_pm:
            continue
        if min_reg is None or t < min_reg:
            min_reg = t
        if t <= t_1530 and (max_reg is None or t > max_reg):
            max_reg = t

    if min_reg is None:
        return t_0900, t_1530
    m_open = time(10, 0) if min_reg.hour >= 10 else t_0900
    m_close = max_reg if (max_reg is not None and t_1400 <= max_reg <= t_1530) else t_1530
    return m_open, m_close


@dataclass(slots=True)
class DayBarIndex:
    """Pre-sorted, pre-indexed intraday bar store for a single trading date."""

    trade_date: date
    market_open_time: time = time(9, 0)
    market_close_time: time = time(15, 30)
    sorted_minutes: List[datetime] = field(default_factory=list)
    minute_slices: Dict[datetime, Tuple[int, int]] = field(default_factory=dict)
    syms: List[str] = field(default_factory=list)
    dts: List[datetime] = field(default_factory=list)
    opens: List[float] = field(default_factory=list)
    highs: List[float] = field(default_factory=list)
    lows: List[float] = field(default_factory=list)
    closes: List[float] = field(default_factory=list)
    vols: List[int] = field(default_factory=list)
    tos: List[float] = field(default_factory=list)
    vwaps: List[float] = field(default_factory=list)
    cum_tos: List[float] = field(default_factory=list)
    is_pms: List[bool] = field(default_factory=list)
    _bar_cache: Dict[int, MinuteBar] = field(default_factory=dict, repr=False)

    def bar(self, idx: int) -> MinuteBar:
        """Lazily builds and caches a `MinuteBar` for sorted row index `idx`."""
        b = self._bar_cache.get(idx)
        if b is None:
            b = MinuteBar(
                symbol=self.syms[idx], dt=self.dts[idx], open=self.opens[idx], high=self.highs[idx],
                low=self.lows[idx], close=self.closes[idx], volume=self.vols[idx], turnover=self.tos[idx],
                vwap=self.vwaps[idx], cum_turnover=self.cum_tos[idx], is_premarket=self.is_pms[idx], source="parquet",
            )
            self._bar_cache[idx] = b
        return b

    def get_minute_range(self, m_dt: datetime) -> Tuple[int, int]:
        """O(1) slice range `(start, end)` in sorted column arrays for minute `m_dt`."""
        return self.minute_slices.get(m_dt, (0, 0))


def _col_floats(table: pa.Table, col: str, avail: Set[str], n: int) -> List[float]:
    if col not in avail:
        return [0.0] * n
    arr = table[col].combine_chunks().to_numpy(zero_copy_only=False).astype(np.float64, copy=False)
    return np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0).tolist()


def _extract_minute_dts(ts_col: pa.ChunkedArray) -> Tuple[List[datetime], List[time]]:
    """Extracts interned minute-floored `datetime` and `time` lists via PyArrow."""
    floored = pc.floor_temporal(ts_col, unit="minute").combine_chunks()
    uniq_pa = pc.unique(floored)
    uniq_dts = [
        (v.to_pydatetime() if hasattr(v, "to_pydatetime") else v).replace(second=0, microsecond=0)
        for v in uniq_pa.to_pylist()
    ]
    uniq_times = [dt.time() for dt in uniq_dts]
    idx_arr = pc.index_in(floored, value_set=uniq_pa).to_numpy(zero_copy_only=False)
    return [uniq_dts[i] for i in idx_arr], [uniq_times[i] for i in idx_arr]


def _has_parquet_magic(path: str) -> bool:
    try:
        if os.path.getsize(path) < 12:
            return False
        with open(path, "rb") as f:
            data = f.read(4)
            f.seek(-4, os.SEEK_END)
            return data == b"PAR1" and f.read(4) == b"PAR1"
    except OSError:
        return False


def _query_dataset(
    files: List[str], trade_date: date, columns: List[str], symbols: Optional[Set[str]]
) -> Optional[pa.Table]:
    ds_obj = ds.dataset(files, format="parquet")
    avail = [c for c in columns if c in ds_obj.schema.names]
    if "timestamp" not in avail or "symbol" not in avail:
        return None
    ts_type = ds_obj.schema.field("timestamp").type
    max_dt = datetime(trade_date.year, trade_date.month, trade_date.day, 15, 30, 0)
    filt = ds.field("timestamp") <= pa.scalar(max_dt, type=ts_type)
    if symbols is not None:
        filt = filt & ds.field("symbol").isin(list(symbols))
    return ds_obj.to_table(columns=avail, filter=filt)


def _read_day_table(
    day_dir: str, trade_date: date, columns: List[str], symbols: Optional[Set[str]] = None
) -> Optional[pa.Table]:
    if not os.path.isdir(day_dir):
        return None
    files = [os.path.join(day_dir, f) for f in os.listdir(day_dir) if f.endswith(".parquet")]
    if symbols is not None:
        files = [p for p in files if os.path.splitext(os.path.basename(p))[0] in symbols]
    if not files:
        return None
    try:
        return _query_dataset(files, trade_date, columns, symbols)
    except (pa.ArrowInvalid, OSError) as exc:
        logger.warning(f"Parquet batch read failed in {day_dir} ({exc}); filtering corrupted files.")
        valid = [p for p in files if _has_parquet_magic(p)]
        if not valid:
            return None
        try:
            return _query_dataset(valid, trade_date, columns, symbols)
        except (pa.ArrowInvalid, OSError):
            tables = []
            for p in valid:
                try:
                    tbl = _query_dataset([p], trade_date, columns, symbols)
                    if tbl is not None and tbl.num_rows > 0:
                        tables.append(tbl)
                except (pa.ArrowInvalid, OSError) as sub_exc:
                    logger.warning(f"Skipping corrupted parquet file {p}: {sub_exc}")
            return pa.concat_tables(tables, promote_options="permissive") if tables else None


def detect_session_times(day_dir: str, trade_date: date) -> Tuple[time, time]:
    """Detects `(market_open_time, market_close_time)` from day parquet files."""
    table = _read_day_table(day_dir, trade_date, ["symbol", "timestamp", "is_premarket"])
    if table is None or table.num_rows == 0:
        return time(9, 0), time(15, 30)
    _, times = _extract_minute_dts(table["timestamp"])
    raw_pms = table["is_premarket"].to_pylist() if "is_premarket" in table.schema.names else None
    return _resolve_session_bounds(times, raw_pms)


def load_day_bar_index(
    day_dir: str,
    trade_date: date,
    symbols: Optional[Set[str]] = None,
) -> DayBarIndex:
    """Loads, sorts, accumulates `cum_turnover`, and indexes a single trading day."""
    req_cols = ["symbol", "timestamp", "open", "high", "low", "close", "volume", "turnover", "vwap", "is_premarket"]
    table = _read_day_table(day_dir, trade_date, req_cols, symbols=symbols)
    if table is None or table.num_rows == 0:
        return DayBarIndex(trade_date=trade_date)

    avail = set(table.schema.names)
    sort_keys = [("timestamp", "ascending")]
    if "turnover" in avail:
        sort_keys.append(("turnover", "descending"))
    sort_keys.append(("symbol", "ascending"))
    table = table.sort_by(sort_keys)

    n = table.num_rows
    dts, times = _extract_minute_dts(table["timestamp"])
    raw_pms = table["is_premarket"].to_pylist() if "is_premarket" in avail else None
    m_open, m_close = _resolve_session_bounds(times, raw_pms)

    syms = table["symbol"].to_pylist()
    if syms and not isinstance(syms[0], str):
        syms = [str(s) for s in syms]
    opens, highs, lows, closes, tos, vwaps = (
        _col_floats(table, c, avail, n) for c in ("open", "high", "low", "close", "turnover", "vwap")
    )
    if "volume" in avail:
        v_arr = np.nan_to_num(table["volume"].combine_chunks().to_numpy(zero_copy_only=False), nan=0.0)
        vols = v_arr.astype(np.int64, copy=False).tolist()
    else:
        vols = [0] * n

    cum_tos, is_pms = [0.0] * n, [False] * n
    cum_tracker: Dict[str, float] = {}
    sorted_minutes: List[datetime] = []
    minute_slices: Dict[datetime, Tuple[int, int]] = {}

    cur_min: Optional[datetime] = None
    cur_start = 0
    for i in range(n):
        dt, sym = dts[i], syms[i]
        ct = cum_tracker.get(sym, 0.0) + tos[i]
        cum_tracker[sym] = cum_tos[i] = ct
        pm = raw_pms[i] if raw_pms is not None else None
        is_pms[i] = (bool(pm) if pm is not None else False) or (times[i] < m_open)
        if dt != cur_min:
            if cur_min is not None:
                minute_slices[cur_min] = (cur_start, i)
            cur_min, cur_start = dt, i
            sorted_minutes.append(dt)
    if cur_min is not None:
        minute_slices[cur_min] = (cur_start, n)

    return DayBarIndex(
        trade_date=trade_date, market_open_time=m_open, market_close_time=m_close,
        sorted_minutes=sorted_minutes, minute_slices=minute_slices,
        syms=syms, dts=dts, opens=opens, highs=highs, lows=lows, closes=closes,
        vols=vols, tos=tos, vwaps=vwaps, cum_tos=cum_tos, is_pms=is_pms,
    )


class CurrentDayStore:
    """Single-slot store holding the active trading date's `DayBarIndex`."""

    def __init__(self) -> None:
        self._date: Optional[date] = None
        self._symbols: Optional[FrozenSet[str]] = None
        self._index: Optional[DayBarIndex] = None

    def get(self, d: date) -> Optional[DayBarIndex]:
        if self._date == d and self._symbols is None:
            return self._index
        return None

    def get_or_load(
        self, day_dir: str, d: date, symbols: Optional[Set[str]] = None
    ) -> DayBarIndex:
        sym_key = frozenset(symbols) if symbols is not None else None
        if self._date == d and self._index is not None and self._symbols == sym_key:
            return self._index
        idx = load_day_bar_index(day_dir, d, symbols=symbols)
        self._date = d
        self._symbols = sym_key
        self._index = idx
        return idx

    def clear(self) -> None:
        self._date = None
        self._symbols = None
        self._index = None
