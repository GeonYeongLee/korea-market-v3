"""
Daily Single-File Parquet Packer for Korea Market v4 Backtesting.
Consolidates thousands of per-symbol `.parquet` files into one presorted
`daily_packed/YYYYMMDD.parquet` per trading day without touching raw `daily/`.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import Optional

import pyarrow as pa
import pyarrow.parquet as pq

from v4.adapters.backtest._dates import list_trading_dates, resolve_data_dir
from v4.adapters.backtest.parquet_loader import _read_day_table

logger = logging.getLogger("korea_market.v4.adapters.backtest.packer")

PACK_COLS = ["symbol", "timestamp", "open", "high", "low", "close", "volume", "turnover", "vwap", "is_premarket"]


def packed_day_path(data_dir: str, trade_date: date) -> str:
    """Returns `.../daily_packed/YYYYMMDD.parquet` path alongside `data_dir`."""
    norm = os.path.normpath(data_dir)
    packed_dir = os.path.join(os.path.dirname(norm), f"{os.path.basename(norm)}_packed")
    return os.path.join(packed_dir, f"{trade_date:%Y%m%d}.parquet")


def pack_trading_days(
    data_dir: Optional[str] = None,
    start_date: Optional[date | str] = None,
    end_date: Optional[date | str] = None,
    overwrite: bool = False,
) -> tuple[int, int]:
    """Packs per-symbol daily parquet files into presorted single-file caches. Returns `(packed, skipped)`."""
    base_dir = resolve_data_dir(data_dir)
    dates = list_trading_dates(base_dir, start_date, end_date)
    packed = 0
    skipped = 0

    for d in dates:
        out_path = packed_day_path(base_dir, d)
        if not overwrite and os.path.isfile(out_path):
            skipped += 1
            continue
        day_dir = os.path.join(base_dir, d.strftime("%Y%m%d"))
        table = _read_day_table(day_dir, d, PACK_COLS, use_packed=False)
        if table is None or table.num_rows == 0:
            skipped += 1
            continue

        avail = set(table.schema.names)
        sort_keys = [("timestamp", "ascending")]
        if "turnover" in avail:
            sort_keys.append(("turnover", "descending"))
        sort_keys.append(("symbol", "ascending"))
        table = table.sort_by(sort_keys)

        meta = dict(table.schema.metadata or {})
        meta[b"v4_sorted"] = b"1"
        table = table.replace_schema_metadata(meta)

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        tmp_path = f"{out_path}.tmp"
        pq.write_table(table, tmp_path, compression="zstd")
        os.replace(tmp_path, out_path)
        packed += 1

    return packed, skipped
