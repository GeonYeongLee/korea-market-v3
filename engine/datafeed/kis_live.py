"""
Real-time DataFeed Adapter for KIS WebSocket.
Streams real-time ticks and finalized v3 MinuteBars via KISWebSocketClient and TickToBarAggregator.
Strictly implements Korea Market v3 IDataFeed.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from datetime import datetime
from typing import List, Optional, Iterator, Union, Any, Dict, Callable

from engine.models import MinuteBar, TickData
from engine.datafeed.base import IDataFeed
from engine.broker.base import IBroker
from core.ws_client import KISWebSocketClient
from engine.datafeed.tick_aggregator import TickToBarAggregator

logger = logging.getLogger("korea_market.datafeed.kis_live")


class KisRealtimeFeed(IDataFeed):
    """Real-time data feed wrapping KIS WebSocket and Tick-to-Bar aggregator."""

    def __init__(
        self,
        ws_client: Optional[KISWebSocketClient] = None,
        aggregator: Optional[TickToBarAggregator] = None,
        broker: Optional[IBroker] = None,
    ):
        self.ws_client = ws_client
        self.aggregator = aggregator or TickToBarAggregator()
        self.broker = broker
        self.active_symbols: List[str] = []
        self._subscription_lock = threading.RLock()

        self._event_queue: asyncio.Queue[Union[MinuteBar, TickData]] = asyncio.Queue()
        self._sync_queue: queue.Queue[Optional[Union[MinuteBar, TickData]]] = queue.Queue()
        self._stop_event = threading.Event()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_thread: Optional[threading.Thread] = None
        self.execution_callback: Optional[Callable[[Dict[str, Any]], Any]] = None
        self.total_ticks_received: int = 0
        self.last_tick_time: float = 0.0

        # Wire aggregator callbacks
        self.aggregator.register_bar_callback(self._on_aggregator_bar)
        self.aggregator.register_tick_callback(self._on_aggregator_tick)

        if self.ws_client and hasattr(self.ws_client, "register_callback"):
            self.ws_client.register_callback(self._on_ws_message)

    def set_broker(self, broker: IBroker) -> None:
        """Sets or updates the broker instance for position-aware eviction and execution dispatch."""
        self.broker = broker

    async def _on_ws_message(self, event_type: str, data: Dict[str, Any]) -> None:
        """Dispatches WebSocket events to tick aggregator or execution notice handlers."""
        if event_type == "TICK":
            await self.aggregator.handle_tick(event_type, data)
        elif event_type == "EXECUTION":
            if self.execution_callback:
                try:
                    res = self.execution_callback(data)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as e:
                    logger.error(f"Error in execution callback: {e}", exc_info=True)
            elif self.broker and hasattr(self.broker, "handle_execution_notice"):
                try:
                    self.broker.handle_execution_notice(data)
                except Exception as e:
                    logger.error(f"Error in broker execution notice handler: {e}", exc_info=True)

    def start(self) -> None:
        """Starts background thread running asyncio loop for WebSocket connection."""
        self._stop_event.clear()
        if self._ws_thread is not None and self._ws_thread.is_alive():
            return

        def _run_loop():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            if self.ws_client and hasattr(self.ws_client, "connect"):
                coro = self.ws_client.connect()
                if asyncio.iscoroutine(coro):
                    self._loop.create_task(coro)
            try:
                self._loop.run_forever()
            finally:
                self._loop.close()

        self._ws_thread = threading.Thread(target=_run_loop, daemon=True, name="KisWsThread")
        self._ws_thread.start()

    def _dispatch_ws_subscribe(self, symbol: str) -> None:
        if not self.ws_client or not hasattr(self.ws_client, "subscribe_symbol"):
            return
        fn = self.ws_client.subscribe_symbol
        if self._loop and self._loop.is_running():
            res = fn(symbol)
            if asyncio.iscoroutine(res):
                asyncio.run_coroutine_threadsafe(res, self._loop)
        else:
            try:
                loop = asyncio.get_running_loop()
                res = fn(symbol)
                if asyncio.iscoroutine(res):
                    loop.create_task(res)
            except RuntimeError:
                res = fn(symbol)
                if asyncio.iscoroutine(res):
                    res.close()

    def _dispatch_ws_unsubscribe(self, symbol: str) -> None:
        if not self.ws_client or not hasattr(self.ws_client, "unsubscribe_symbol"):
            return
        fn = self.ws_client.unsubscribe_symbol
        if self._loop and self._loop.is_running():
            res = fn(symbol)
            if asyncio.iscoroutine(res):
                asyncio.run_coroutine_threadsafe(res, self._loop)
        else:
            try:
                loop = asyncio.get_running_loop()
                res = fn(symbol)
                if asyncio.iscoroutine(res):
                    loop.create_task(res)
            except RuntimeError:
                res = fn(symbol)
                if asyncio.iscoroutine(res):
                    res.close()

    def subscribe_symbol(self, symbol: str) -> None:
        """
        Dynamically registers a symbol to the live feed.
        Enforces KIS 40-symbol cap, protecting open positions from eviction.
        """
        with self._subscription_lock:
            if symbol not in self.active_symbols:
                if len(self.active_symbols) >= 40:
                    held = set()
                    if self.broker:
                        held = set(self.broker.get_positions().keys())
                    evicted = None
                    for s in self.active_symbols:
                        if s not in held:
                            evicted = s
                            break
                    if evicted:
                        self.active_symbols.remove(evicted)
                        self._dispatch_ws_unsubscribe(evicted)
                        logger.warning(
                            f"Reached KIS WebSocket 40-symbol limit. Evicted unheld symbol {evicted} for {symbol}."
                        )
                    else:
                        logger.warning(
                            f"Reached KIS WebSocket 40-symbol limit but all {len(self.active_symbols)} are held. Cannot evict. Rejecting subscription for {symbol}."
                        )
                        return False
                self.active_symbols.append(symbol)
        self._dispatch_ws_subscribe(symbol)
        return True

    def unsubscribe_symbol(self, symbol: str) -> None:
        """Dynamically unregisters a symbol from the live WebSocket feed."""
        with self._subscription_lock:
            if symbol in self.active_symbols:
                self.active_symbols.remove(symbol)
        self._dispatch_ws_unsubscribe(symbol)
        logger.info(f"Unsubscribed symbol {symbol} from WebSocket feed. Remaining: {len(self.active_symbols)}")

    def set_symbols(self, symbols: List[str]) -> None:
        """Sets active symbols, subscribing newly added symbols."""
        with self._subscription_lock:
            new_symbols = [s for s in symbols if s not in self.active_symbols]
            self.active_symbols = list(symbols)
        for sym in new_symbols:
            self._dispatch_ws_subscribe(sym)

    async def _on_aggregator_bar(self, *args, **kwargs) -> None:
        """Handles finalized 1-minute bars from TickToBarAggregator."""
        if len(args) == 2:
            symbol, bar = args
        elif len(args) == 1:
            bar = args[0]
        else:
            bar = kwargs.get("bar") or kwargs.get("kbar")

        if bar is None:
            return

        if self._event_queue.qsize() < 100:
            try:
                self._event_queue.put_nowait(bar)
            except Exception:
                pass
        self._sync_queue.put(bar)

    async def _on_aggregator_tick(self, symbol: str, price_data: Any, dt: datetime) -> None:
        """Handles real-time tick events from aggregator."""
        self.total_ticks_received += 1
        self.last_tick_time = time.time()
        if isinstance(price_data, dict):
            px = float(price_data.get("price", 0.0))
            vol = int(price_data.get("volume", 1))
            cum_to = float(price_data.get("cum_turnover", 0.0))
            to_val = float(price_data.get("turnover", 0.0)) or (px * vol)
            bid = float(price_data.get("bid", 0.0))
            ask = float(price_data.get("ask", 0.0))
            day_o = float(price_data.get("open", 0.0))
            day_l = float(price_data.get("low", 0.0))
        elif isinstance(price_data, TickData):
            px = price_data.price
            vol = price_data.volume
            cum_to = price_data.cum_turnover
            to_val = price_data.turnover
            bid = price_data.bid
            ask = price_data.ask
            day_o = price_data.day_open
            day_l = price_data.day_low
        else:
            px = float(price_data)
            vol = 1
            cum_to = 0.0
            to_val = 0.0
            bid = 0.0
            ask = 0.0
            day_o = 0.0
            day_l = 0.0

        tick = TickData(
            symbol=symbol,
            dt=dt,
            price=px,
            volume=vol,
            turnover=to_val,
            cum_turnover=cum_to,
            bid=bid,
            ask=ask,
            day_open=day_o,
            day_low=day_l,
        )
        if self._event_queue.qsize() < 100:
            try:
                self._event_queue.put_nowait(tick)
            except Exception:
                pass
        self._sync_queue.put(tick)

    async def get_next_event(self) -> Union[MinuteBar, TickData]:
        """Asynchronous event retrieval."""
        return await self._event_queue.get()

    def stream_events(self) -> Iterator[Union[MinuteBar, TickData]]:
        """Synchronous event generator for TradingEngine.run() compatibility."""
        self.start()
        while not self._stop_event.is_set():
            try:
                event = self._sync_queue.get(timeout=0.5)
                if event is None:
                    break
                yield event
            except queue.Empty:
                continue

    def stop(self) -> None:
        """Signals the feed to stop streaming and disconnects WebSocket."""
        self._stop_event.set()
        self._sync_queue.put(None)
        if self.ws_client and hasattr(self.ws_client, "disconnect") and self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.ws_client.disconnect(), self._loop)
            except Exception:
                pass
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def hard_reconnect(self) -> None:
        """Triggers an immediate hard reconnect on the underlying WebSocket client."""
        self.reboot()

    def reboot(self) -> None:
        """
        Hard re-initialization of WebSocket feed.
        Invalidates approval key, triggers fresh connection with newly acquired approval key,
        and resubscribes all active symbols.
        """
        logger.warning("🚨 [KisRealtimeFeed] Hard reboot triggered! Re-initializing WebSocket feed...")
        self._stop_event.clear()
        if self.ws_client:
            if hasattr(self.ws_client, "token_mgr") and self.ws_client.token_mgr:
                self.ws_client.token_mgr.invalidate_approval_key()
            if hasattr(self.ws_client, "hard_reconnect") and self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self.ws_client.hard_reconnect(), self._loop)
            elif hasattr(self.ws_client, "connection_state"):
                self.ws_client.connection_state = "DISCONNECTED"

        if self._ws_thread is None or not self._ws_thread.is_alive():
            logger.info("[KisRealtimeFeed] WS thread dead during reboot; restarting thread...")
            self.start()
