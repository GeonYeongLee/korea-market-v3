"""
Korea Exchange (KRX) Statutory Tick Size and Price Rounding Utilities.
Conforms to statutory KRX equity market price increment regulations.
"""
from __future__ import annotations

import math
from typing import Optional


def get_krx_tick_size(price: float) -> int:
    """
    Returns statutory KRX tick size (호가단위) for a given stock price.
    
    KRX Equity Tick Table:
      - Price < 2,000 KRW            : 1 KRW
      - 2,000 <= Price < 5,000 KRW   : 5 KRW
      - 5,000 <= Price < 20,000 KRW  : 10 KRW
      - 20,000 <= Price < 50,000 KRW : 50 KRW
      - 50,000 <= Price < 200,000 KRW: 100 KRW
      - 200,000 <= Price < 500,000 KRW: 500 KRW
      - Price >= 500,000 KRW         : 1,000 KRW
    """
    p = abs(price)
    if p < 2_000:
        return 1
    elif p < 5_000:
        return 5
    elif p < 20_000:
        return 10
    elif p < 50_000:
        return 50
    elif p < 200_000:
        return 100
    elif p < 500_000:
        return 500
    else:
        return 1_000


def round_to_krx_tick(
    price: float,
    side: str = "BUY",
    round_mode: Optional[str] = None,
) -> int:
    """
    Rounds a floating-point target price to the nearest valid KRX tick increment.
    
    Args:
        price: Raw target price (e.g. 52350.7).
        side: Order side ('BUY' or 'SELL').
        round_mode: 'floor', 'ceil', or 'round'.
                    If None:
                      - BUY defaults to 'floor' (never exceed limit buying price).
                      - SELL defaults to 'ceil' (never sell below limit selling price).
    
    Returns:
        Exact integer price compliant with KRX tick tables.
    """
    if price <= 0:
        return 0

    tick = get_krx_tick_size(price)

    if round_mode is None:
        round_mode = "floor" if side.upper() == "BUY" else "ceil"

    round_mode = round_mode.lower()
    if round_mode == "floor":
        return int(math.floor(price / tick) * tick)
    elif round_mode == "ceil":
        return int(math.ceil(price / tick) * tick)
    elif round_mode == "round":
        return int(round(price / tick) * tick)
    else:
        raise ValueError(f"Invalid round_mode: {round_mode}. Must be 'floor', 'ceil', or 'round'.")


def add_krx_ticks(price: float, ticks: int, side: str = "BUY") -> int:
    """Adds or subtracts a specific number of KRX ticks from a given price."""
    curr = round_to_krx_tick(price, side=side)
    for _ in range(abs(ticks)):
        step = get_krx_tick_size(curr)
        if ticks > 0:
            curr += step
        else:
            curr = max(1, curr - step)
    return curr

