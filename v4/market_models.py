"""
Market & Event Data Models for Korea Market v4.
Slotted dataclasses for market regime, causal snapshots, bars, condition candidates, and feed events.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal, Optional

ExecutionMode = Literal["pessimistic", "optimistic"]
MarketDivision = Literal["UN", "J", "NX"]
WsTrId = Literal["H0UNCNT0", "H0STCNT0"]


def shift_from_0900(base_t: time, market_open_time: time) -> time:
    """Shifts a 09:00-relative session schedule time to match actual market_open_time."""
    delta_sec = (market_open_time.hour - 9) * 3600 + market_open_time.minute * 60 + market_open_time.second
    base_sec = base_t.hour * 3600 + base_t.minute * 60 + base_t.second
    total = max(0, min(23 * 3600 + 59 * 60 + 59, base_sec + delta_sec))
    return time(total // 3600, (total % 3600) // 60, total % 60)


@dataclass(frozen=True, slots=True)
class MarketIndexRegime:
    """Point-in-time market regime, opening index gaps, and trading session schedule."""
    trade_date: date
    ks_gap_pct: float
    kq_gap_pct: float
    ks_trend: float = 0.0
    kq_trend: float = 0.0
    market_gap_std20: float = 0.85
    is_bear_market: bool = False
    dynamic_max_gap: float = 0.85
    market_open_time: time = time(9, 0)
    market_close_time: time = time(15, 30)
    source: str = "parquet"


@dataclass(frozen=True, slots=True)
class SymbolFeatures:
    """Strictly causal (t-1 settled) stock features & NXT eligibility."""
    symbol: str
    prior_atr20_pct: float
    prior_adto20: float
    prev_close: float = 0.0
    is_nxt_eligible: Optional[bool] = None


@dataclass(slots=True)
class MinuteBar:
    """
    Standardized 1-minute OHLCV bar; premarket rows (< market_open_time) are flagged.
    Canonical `cum_turnover` follows integrated KRX+NXT (`"UN"`) semantics matching
    v3 `HistoricalDataFeed` & KIS `acml_tr_pbmn` (inclusive of 08:00~08:50 premarket).
    """
    symbol: str
    dt: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    turnover: float = 0.0
    vwap: float = 0.0
    cum_turnover: float = 0.0
    is_premarket: bool = False
    source: str = ""

    @property
    def time(self) -> time: return self.dt.time()
    @property
    def date(self) -> date: return self.dt.date()


@dataclass(slots=True)
class ConditionCandidate:
    """
    HTS (`HHKST03900400`) or causal emulator candidate. `krx_day_low` / `prior_krx_day_low`
    are strictly >= `market_open_time` (`prior_krx_day_low` < minute `M` for `optimistic` MAE).
    Fast-Pass is only permitted when `seq == "0"` or `curr_bar_surge_ok is True`.
    """
    symbol: str
    name: str = ""
    day_open: float = 0.0
    krx_day_low: float = 0.0
    prior_krx_day_low: float = 0.0
    krx_cum_turnover: float = 0.0
    current_price: float = 0.0
    prev_close: float = 0.0
    bar_open: float = 0.0
    bar_high: float = 0.0
    bar_low: float = 0.0
    bar_close: float = 0.0
    bar_volume: int = 0
    bar_turnover: float = 0.0
    bar_dt: Optional[datetime] = None
    curr_bar_surge_ok: Optional[bool] = None
    prev_bar_surge_ok: Optional[bool] = None
    is_nxt_eligible: Optional[bool] = None
    seq: str = ""
    as_of: Optional[datetime] = None
    chgrate: float = 0.0
    cttr: float = 0.0


@dataclass(slots=True)
class IntradaySnapshot:
    """
    Causal snapshot at `as_of` (`UN` -> `J` fallback).
    For NXT-eligible stocks in live: `cum_turnover`/`cum_volume` come from `"UN"`, while
    `day_open`/`day_low`/`day_high` MUST come from `"J"` or regular (>= market_open_time) bars.
    """
    symbol: str
    as_of: datetime
    day_open: float
    day_low: float
    day_high: float
    current_price: float
    cum_turnover: float
    cum_volume: int
    is_nxt_eligible: bool = False
    source: str = "J"
    prev_close: float = 0.0
    premarket_low: float = 0.0
    premarket_turnover: float = 0.0


@dataclass(slots=True)
class MinutePulseEvent:
    """
    Session pulse driving HTS polling (`is_minute_boundary=False` on sub-second live polls,
    `True` on minute `M` rollovers and in backtest) and `15:21` timeout checks up to `market_close_time`.
    """
    dt: datetime
    is_minute_boundary: bool = True
    is_session_end: bool = False

    @property
    def time(self) -> time: return self.dt.time()
    @property
    def date(self) -> date: return self.dt.date()


@dataclass(slots=True)
class TickEvent:
    """
    Real-time WS execution tick (`H0UNCNT0` for NXT, `H0STCNT0` for non-NXT).
    `day_low` never falls back to `raw_day_low` for `H0UNCNT0` (which carries 08:00~08:50 premarket lows).
    """
    symbol: str
    dt: datetime
    price: float
    volume: int
    turnover: float = 0.0
    cum_turnover: float = 0.0
    cum_volume: int = 0
    bid: float = 0.0
    ask: float = 0.0
    day_open: float = 0.0
    krx_day_open: float = 0.0
    raw_day_open: float = 0.0
    krx_day_low: float = 0.0
    raw_day_low: float = 0.0
    vwap: float = 0.0
    volume_power: float = 0.0
    tr_id: str = ""

    def __post_init__(self) -> None:
        if self.day_open <= 0.0:
            if self.krx_day_open > 0.0:
                self.day_open = self.krx_day_open
            elif self.tr_id == "H0STCNT0" and self.raw_day_open > 0.0:
                self.day_open = self.raw_day_open

    @property
    def day_low(self) -> float:
        if self.krx_day_low > 0.0:
            return self.krx_day_low
        if self.tr_id == "H0STCNT0":
            return self.raw_day_low
        return 0.0

    @property
    def time(self) -> time: return self.dt.time()
    @property
    def date(self) -> date: return self.dt.date()


@dataclass(slots=True)
class BarCloseEvent:
    """
    Completed 1-minute bar event (WS aggregator rollover / wall-clock flush or backtest bar).
    `dt` is strictly the bar start minute `HH:MM:00` (matching `bar.dt`).
    """
    symbol: str
    dt: datetime
    bar: MinuteBar

    @property
    def time(self) -> time: return self.dt.time()
    @property
    def date(self) -> date: return self.dt.date()
