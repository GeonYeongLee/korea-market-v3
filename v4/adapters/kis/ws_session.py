"""
WebSocket Session, Message Dispatch & Pulse/Watchdog Loop Helpers for `KisRealtimeFeed`.
"""
from __future__ import annotations

import asyncio
import logging
import time as pytime
from datetime import datetime, time
from typing import Any, Optional

from v4.adapters.kis.feed_parser import (
    parse_execution_notice,
    parse_tick_records,
    parse_ws_control_frame,
)
from v4.models import MinutePulseEvent

logger = logging.getLogger("korea_market.v4.adapters.kis.ws_session")

SILENCE_WATCHDOG_SEC = 30.0


async def handle_ws_raw_message(feed: Any, raw: str) -> None:
    """Dispatches a single raw KIS WebSocket frame (PINGPONG, JSON control, tick, or exec notice)."""
    if "PINGPONG" in raw:
        if feed._ws is not None:
            await feed._ws.send(raw)
        return

    if raw.startswith("{"):
        ctrl = parse_ws_control_frame(raw)
        if ctrl.get("pingpong") and feed._ws is not None:
            await feed._ws.send(raw)
        elif ctrl.get("auth_failed"):
            feed.connection_state = "AUTH_FAILED"
            await asyncio.to_thread(feed.token_mgr.invalidate_approval_key)
            logger.error("WS AUTH_FAILED frame: msg_cd=%s msg1=%s", ctrl.get("msg_cd"), ctrl.get("msg1"))
            if feed._ws is not None:
                await feed._ws.close()
        elif ctrl.get("aes_key"):
            feed._aes_iv = ctrl.get("aes_iv")
            feed._aes_key = ctrl.get("aes_key")
        return

    parts = raw.split("|")
    if len(parts) < 4:
        return
    enc_flag, tr_id, cnt_str, data_str = parts[0], parts[1], parts[2], parts[3]

    if tr_id in ("H0STCNT0", "H0UNCNT0"):
        cnt = int(cnt_str) if cnt_str.isdigit() else 1
        for tick in parse_tick_records(tr_id, cnt, data_str, feed.clock.now()):
            closed = feed._accumulator.on_tick(tick)
            if closed is not None:
                await feed._queue.put(closed)
            await feed._queue.put(tick)
    elif tr_id in ("H0STCNI0", "H0STCNI9"):
        notice = parse_execution_notice(enc_flag, data_str, feed._aes_key, feed._aes_iv)
        if notice and feed.broker is not None and hasattr(feed.broker, "handle_execution_notice"):
            feed.broker.handle_execution_notice(notice)


async def run_ws_loop(feed: Any, websockets_mod: Any) -> None:
    """Maintains the KIS WebSocket connection, subscriptions, and reconnect backoff."""
    while feed._running:
        if feed.connection_state == "AUTH_FAILED":
            await asyncio.sleep(3.0)
            continue
        feed.connection_state = "CONNECTING"
        try:
            approval_key = await asyncio.to_thread(feed.token_mgr.get_approval_key)
            async with websockets_mod.connect(feed.ws_url, ping_interval=30, ping_timeout=10) as ws:
                feed._ws = ws
                feed.connection_state = "CONNECTED"
                feed.last_msg_mono = pytime.monotonic()
                for sym, tr_id in list(feed._subscriptions.items()):
                    await feed._send_ws_sub(sym, tr_id, "1", approval_key)
                if feed.cfg.hts_id:
                    exec_tr = "H0STCNI0" if feed.cfg.env == "real" else "H0STCNI9"
                    await feed._send_ws_sub(feed.cfg.hts_id, exec_tr, "1", approval_key)

                async for raw in ws:
                    if not feed._running:
                        break
                    feed.last_msg_mono = pytime.monotonic()
                    await feed._handle_raw_message(raw)
                    if feed.connection_state == "AUTH_FAILED":
                        break
        except asyncio.CancelledError:
            break
        except Exception as exc:
            err = str(exc).lower()
            if any(k in err for k in ("401", "403", "approval", "auth")):
                feed.connection_state = "AUTH_FAILED"
                await asyncio.to_thread(feed.token_mgr.invalidate_approval_key)
                logger.error("WS auth failed: %s", exc)
            else:
                feed.connection_state = "DISCONNECTED"
                logger.warning("WS disconnected (%s); reconnecting in 3s", exc)
            await asyncio.sleep(3.0)


async def run_pulse_and_watchdog_loop(feed: Any) -> None:
    """Emits minute-boundary/poll pulses, flushes quiet bars, and triggers 30s silence reconnect."""
    last_min_key: Optional[datetime] = None
    last_poll_mono = 0.0
    while feed._running:
        try:
            now_dt = feed.clock.now()
            cur_min = now_dt.replace(second=0, microsecond=0)

            for closed in feed._accumulator.flush_expired(now_dt):
                await feed._queue.put(closed)

            if last_min_key is None or cur_min > last_min_key:
                last_min_key = cur_min
                is_end = now_dt.time() >= feed.market_close_time
                await feed._queue.put(MinutePulseEvent(dt=cur_min, is_minute_boundary=True, is_session_end=is_end))
                if is_end:
                    await feed._queue.put(None)
                    break
            elif feed.poll_interval_sec > 0.0 and (pytime.monotonic() - last_poll_mono) >= feed.poll_interval_sec:
                last_poll_mono = pytime.monotonic()
                await feed._queue.put(MinutePulseEvent(dt=now_dt, is_minute_boundary=False, is_session_end=False))

            if (feed.is_connected and feed.last_msg_mono > 0.0
                    and (pytime.monotonic() - feed.last_msg_mono) >= SILENCE_WATCHDOG_SEC
                    and time(9, 0) <= now_dt.time() <= feed.market_close_time):
                feed.last_msg_mono = pytime.monotonic()
                await feed.hard_reconnect()

            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Pulse/watchdog error: %s", exc, exc_info=True)
            await asyncio.sleep(0.5)
