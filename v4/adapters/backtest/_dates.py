"""Date and data-directory resolution helpers for Korea Market v4 Backtesting."""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import List, Optional


def resolve_data_dir(custom_path: Optional[str] = None) -> str:
    """Resolves the available 1-minute partitioned dataset directory."""
    candidates = [
        custom_path,
        "data/dataset_2y/daily",
        os.path.join("..", "korea-market_v2", "data", "dataset_2y", "daily"),
    ]
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return custom_path or "data/dataset_2y/daily"


def parse_date_arg(val: Optional[date | str]) -> Optional[date]:
    """Parses `date`, `YYYY-MM-DD`, or `YYYYMMDD` into `datetime.date`."""
    if val is None or isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    s = str(val).strip().replace("-", "")
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    raise ValueError(f"Unsupported date format: {val!r}")


def list_trading_dates(
    data_dir: str, start_date: Optional[date | str] = None, end_date: Optional[date | str] = None
) -> List[date]:
    """Returns sorted trading dates available in `data_dir` within `[start_date, end_date]`."""
    if not os.path.isdir(data_dir):
        return []
    s_dt, e_dt = parse_date_arg(start_date), parse_date_arg(end_date)
    dates: List[date] = []
    for entry in sorted(os.listdir(data_dir)):
        if len(entry) != 8 or not entry.isdigit() or not os.path.isdir(os.path.join(data_dir, entry)):
            continue
        d_val = date(int(entry[:4]), int(entry[4:6]), int(entry[6:8]))
        if (s_dt is not None and d_val < s_dt) or (e_dt is not None and d_val > e_dt):
            continue
        dates.append(d_val)
    return dates
