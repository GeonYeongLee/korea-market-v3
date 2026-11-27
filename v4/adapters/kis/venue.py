"""
Single Source of Truth (SSOT) for KIS Market Venue & TR-ID Routing (KRX vs NXT/SOR).
"""
from __future__ import annotations

from typing import Optional, Set, Tuple


class KisVenueResolver:
    """Centralizes KRX vs Nextrade (NXT) venue, WS TR-ID, order exchange, and bar routing."""

    def __init__(self, nxt_symbols: Optional[Set[str]] = None) -> None:
        self.has_nxt_metadata: bool = nxt_symbols is not None
        self.nxt_symbols: Set[str] = {
            str(s).strip() for s in (nxt_symbols or set()) if str(s).strip()
        }

    def is_nxt(self, symbol: str) -> bool:
        """Returns True if `symbol` is eligible for Nextrade (NXT) integrated routing."""
        return str(symbol).strip() in self.nxt_symbols

    def is_nxt_or_none(self, symbol: str) -> Optional[bool]:
        """Returns `is_nxt(symbol)` when metadata is available, else `None`."""
        return self.is_nxt(symbol) if self.has_nxt_metadata else None

    def ws_tr_id(self, symbol: str) -> str:
        """Returns `H0UNCNT0` for NXT-eligible symbols, else `H0STCNT0`."""
        return "H0UNCNT0" if self.is_nxt(symbol) else "H0STCNT0"

    def order_exchange(self, symbol: str) -> str:
        """Returns `SOR` for NXT-eligible symbols, else `KRX`."""
        return "SOR" if self.is_nxt(symbol) else "KRX"

    @staticmethod
    def minute_bar_divs() -> Tuple[str, ...]:
        """Returns ordered minute-bar market division codes (`'J'` first, `'UN'` fallback)."""
        return ("J", "UN")
