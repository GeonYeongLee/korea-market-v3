"""
Shared defensive scalar parsers for KIS REST/WebSocket payloads.
"""
from __future__ import annotations

from typing import Any, Optional


def safe_float(val: Any, default: float = 0.0) -> float:
    """Parses `val` as float, returning `default` on None, empty string, or parse error."""
    try:
        if val is None:
            return default
        s = str(val).strip()
        return float(s) if s else default
    except (ValueError, TypeError):
        return default


def safe_int(val: Any, default: int = 0) -> int:
    """Parses `val` as int (via float), returning `default` on None, empty string, or parse error."""
    try:
        if val is None:
            return default
        s = str(val).strip()
        return int(float(s)) if s else default
    except (ValueError, TypeError):
        return default


def parse_optional_float(raw: Any) -> Optional[float]:
    """Parses `raw` as float, returning `None` on None, empty string, or parse error."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None
