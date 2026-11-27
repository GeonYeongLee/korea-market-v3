"""
KIS Live WebSocket Realtime Feed Adapter for Korea Market v4.
Implements `IRealtimeFeedPort` with strict `H0UNCNT0` (NXT) / `H0STCNT0` (non-NXT) routing,
40-slot shared cap with position + pending-order eviction protection, `cnt > 1` frame parsing,
30s silence watchdog, `PINGPONG`/`AUTH_FAILED` handling, and `M+1:00.1` quiet bar rollover.
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import date, time
from typing import Any, AsyncIterator, List, Optional, Set

import websockets

from config.settings import Settings, settings
from core.auth import TokenManager
from v4.adapters.kis.bar_accumulator import BarAccumulator
from v4.adapters.kis.clock import KisLiveClock
from v4.adapters.kis.feed_parser import build_ws_sub_payload
from v4.adapters.kis.regime_data import load_nxt_symbol_set
from v4.adapters.kis.venue import KisVenueResolver
from v4.adapters.kis.ws_session import (
    SILENCE_WATCHDOG_SEC,
    handle_ws_raw_message,
    run_pulse_and_watchdog_loop,
    run_ws_loop,
)
from v4.interfaces import IBrokerPort, IClock, IRealtimeFeedPort
from v4.models import MarketEvent

logger = logging.getLogger("korea_market.v4.adapters.kis.feed")

MAX_WS_SLOTS = 40


class KisRealtimeFeed(IRealtimeFeedPort):
    """Unified async KIS WebSocket + wall-clock pulse feed implementing `IRealtimeFeedPort`."""

    def __init__(
        self,
        cfg: Optional[Settings] = None,
        token_mgr: Optional[TokenManager] = None,
        broker: Optional[IBrokerPort] = None,
        clock: Optional[IClock] = None,
        nxt_symbols: Optional[Set[str]] = None,
        nxt_file: Optional[str] = "data/metadata/nxt_symbols.json",
        market_close_time: time = time(15, 30),
        poll_interval_sec: float = 0.25,
        venue: Optional[KisVenueResolver] = None,
    ) -> None:
        self.cfg = cfg or settings
        self.token_mgr = token_mgr or TokenManager(self.cfg)
        self.broker = broker
        self.clock = clock or KisLiveClock()
        if venue is not None:
            self.venue = venue
            self.nxt_symbols: Optional[Set[str]] = set(venue.nxt_symbols)
        else:
            self.nxt_symbols = (
                nxt_symbols if nxt_symbols is not None else load_nxt_symbol_set(nxt_file)
            )
            self.venue = KisVenueResolver(self.nxt_symbols)
        self.market_close_time = market_close_time
        self.poll_interval_sec = float(poll_interval_sec)

        self._subscriptions: OrderedDict[str, str] = OrderedDict()  # symbol -> H0UNCNT0 | H0STCNT0
        self._accumulator = BarAccumulator()
        self._queue: asyncio.Queue[Optional[MarketEvent]] = asyncio.Queue()
        self._ws: Any = None
        self._running = False
        self._tasks: List[asyncio.Task[Any]] = []
        self._aes_key: Optional[str] = None
        self._aes_iv: Optional[str] = None
        self.connection_state: str = "DISCONNECTED"
        self.last_msg_mono: float = 0.0

    def set_broker(self, broker: IBrokerPort) -> None:
        self.broker = broker

    @property
    def ws_url(self) -> str:
        return f"{self.cfg.endpoints.ws_url}/tryitout/H0UNCNT0"

    @property
    def is_connected(self) -> bool:
        if self._ws is None:
            return False
        if hasattr(self._ws, "open"):
            return bool(self._ws.open)
        if hasattr(self._ws, "state"):
            return self._ws.state == 1 or getattr(self._ws.state, "name", "") == "OPEN"
        return True

    def active_symbols(self) -> List[str]:
        return list(self._subscriptions.keys())

    def _protected_symbols(self) -> Set[str]:
        protected: Set[str] = set()
        if self.broker is None:
            return protected
        for sym, pos in self.broker.get_positions().items():
            if pos and pos.quantity > 0:
                protected.add(sym)
        for pend in self.broker.get_pending_orders():
            if pend and pend.symbol:
                protected.add(pend.symbol)
        return protected

    def _resolve_tr_id(self, symbol: str, is_nxt: Optional[bool]) -> str:
        if is_nxt is not None:
            return "H0UNCNT0" if is_nxt else "H0STCNT0"
        return self.venue.ws_tr_id(symbol)

    async def _send_ws_sub(self, symbol: str, tr_id: str, tr_type: str, approval_key: str) -> None:
        if self.is_connected and self._ws is not None:
            await self._ws.send(build_ws_sub_payload(symbol, tr_id, tr_type, approval_key))

    async def subscribe(self, symbol: str, is_nxt: Optional[bool] = None) -> bool:
        """Idempotently subscribes `symbol` (`H0UNCNT0` for NXT, `H0STCNT0` for non-NXT) within 40-slot cap."""
        if symbol in self._subscriptions:
            return True

        if len(self._subscriptions) >= MAX_WS_SLOTS:
            protected = self._protected_symbols()
            evict_sym = next((s for s in self._subscriptions if s not in protected), None)
            if evict_sym is None:
                logger.warning("WS 40-slot cap reached and all symbols are protected; rejecting %s", symbol)
                return False
            await self.unsubscribe(evict_sym)
            logger.info("Evicted unprotected WS symbol %s to make room for %s", evict_sym, symbol)

        tr_id = self._resolve_tr_id(symbol, is_nxt)
        self._subscriptions[symbol] = tr_id
        if self.is_connected:
            approval_key = await asyncio.to_thread(self.token_mgr.get_approval_key)
            await self._send_ws_sub(symbol, tr_id, "1", approval_key)
        return True

    async def unsubscribe(self, symbol: str) -> None:
        tr_id = self._subscriptions.pop(symbol, None)
        self._accumulator.discard(symbol)
        if tr_id and self.is_connected:
            approval_key = await asyncio.to_thread(self.token_mgr.get_approval_key)
            await self._send_ws_sub(symbol, tr_id, "2", approval_key)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._ws_loop(), name="KisWsLoop"),
            asyncio.create_task(self._pulse_and_watchdog_loop(), name="KisPulseWatchdog"),
        ]

    async def _close_ws(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def stop(self) -> None:
        self._running = False
        self.connection_state = "DISCONNECTED"
        await self._close_ws()
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        await self._queue.put(None)

    async def hard_reconnect(self) -> None:
        logger.warning("Initiating hard WS reconnect (silence watchdog / auth reset)...")
        await asyncio.to_thread(self.token_mgr.invalidate_approval_key)
        self.connection_state = "DISCONNECTED"
        await self._close_ws()

    async def _ws_loop(self) -> None:
        await run_ws_loop(self, websockets)

    async def _handle_raw_message(self, raw: str) -> None:
        await handle_ws_raw_message(self, raw)

    async def _pulse_and_watchdog_loop(self) -> None:
        await run_pulse_and_watchdog_loop(self)

    async def stream_events(self, start_date: Optional[date] = None, end_date: Optional[date] = None) -> AsyncIterator[MarketEvent]:
        await self.start()
        while self._running or not self._queue.empty():
            ev = await self._queue.get()
            if ev is None:
                break
            yield ev
