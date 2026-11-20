"""
Korea Investment & Securities (KIS) WebSocket Client.
=====================================================
Handles real-time tick prices (H0UNCNT0, H0STCNT0) and execution notices (H0STCNI0, H0STCNI9).
Provides robust reconnection, clean teardown, PINGPONG keepalive echo, and AES notice decryption.
"""
from __future__ import annotations

import base64
import json
import logging
import asyncio
import threading
from typing import Dict, Any, Callable, Awaitable, Set, List, Optional
import websockets

from config.settings import Settings, settings
from core.auth import TokenManager
from core.telemetry import telemetry

try:
    from Cryptodome.Cipher import AES
    from Cryptodome.Util.Padding import unpad
except ImportError:
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
    except ImportError:
        AES = None
        unpad = None

logger = logging.getLogger("kis.ws")

TickCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]


def _safe_float(v: Any, default: float = 0.0) -> float:
    """Safely converts string or numeric input to float."""
    try:
        if v is None:
            return default
        s = str(v).strip()
        return float(s) if s else default
    except (ValueError, TypeError):
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    """Safely converts string or numeric input to integer."""
    try:
        if v is None:
            return default
        s = str(v).strip().split(".")[0]
        return int(s) if s else default
    except (ValueError, TypeError):
        return default


class KISWebSocketClient:
    """Asynchronous WebSocket streamer for KIS Open API."""

    def __init__(
        self,
        cfg: Optional[Settings] = None,
        token_mgr: Optional[TokenManager] = None,
    ):
        self.cfg = cfg or settings
        self.token_mgr = token_mgr or TokenManager(self.cfg)
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._lock = threading.Lock()
        self._subscribed_symbols: Set[str] = set()
        self._callbacks: List[TickCallback] = []
        self._reconnect_delay = 3.0
        self._aes_key: Optional[str] = None
        self._aes_iv: Optional[str] = None

    @property
    def ws_url(self) -> str:
        """KIS WebSocket endpoint (H0UNCNT0: 통합 실시간 체결가)."""
        return f"{self.cfg.endpoints.ws_url}/tryitout/H0UNCNT0"

    def register_callback(self, cb: TickCallback) -> None:
        """Register async callback function for incoming market ticks and executions."""
        with self._lock:
            self._callbacks.append(cb)

    @property
    def is_connected(self) -> bool:
        """Checks whether the underlying WebSocket connection is open."""
        ws = self._ws
        if ws is None:
            return False
        if hasattr(ws, "closed") and ws.closed:
            return False
        if hasattr(ws, "open"):
            return bool(ws.open)
        if hasattr(ws, "state"):
            try:
                return ws.state == 1 or getattr(ws.state, "name", "") == "OPEN"
            except Exception:
                return ws.state == 1
        return True

    async def connect(self) -> None:
        """Establishes WebSocket connection and starts listener loop with auto-reconnect."""
        self._running = True

        while self._running:
            try:
                approval_key = self.token_mgr.get_approval_key()
                logger.info(f"[KIS_WS] Connecting to KIS WebSocket: {self.ws_url}")
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    logger.info("[KIS_WS] WebSocket connected successfully.")

                    # Resubscribe to previous symbols if reconnecting
                    with self._lock:
                        current_subs = list(self._subscribed_symbols)

                    for sym in current_subs:
                        await self._send_subscription(sym, tr_type="1", approval_key=approval_key)

                    # Also subscribe to user execution notice if HTS_ID provided
                    if self.cfg.hts_id:
                        await self._subscribe_execution_notice(approval_key)

                    # Listen for incoming frames
                    async for message in ws:
                        if not self._running:
                            break
                        await self._handle_message(message)

            except (websockets.ConnectionClosed, OSError) as e:
                if not self._running:
                    logger.info("[KIS_WS] WebSocket connection closed cleanly during shutdown.")
                    break
                logger.warning(f"[KIS_WS] WebSocket connection lost: {e}. Reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
            except asyncio.CancelledError:
                logger.info("[KIS_WS] WebSocket listener task cancelled.")
                break
            except Exception as e:
                if not self._running:
                    break
                logger.error(f"[KIS_WS] Unexpected WebSocket error: {e}", exc_info=True)
                await asyncio.sleep(self._reconnect_delay)
            finally:
                self._ws = None

    async def disconnect(self) -> None:
        """Gracefully disconnect WebSocket and terminate listener loop."""
        self._running = False
        ws = self._ws
        self._ws = None
        if ws:
            try:
                await ws.close()
            except Exception as e:
                logger.debug(f"[KIS_WS] Error closing WebSocket: {e}")

    async def subscribe_symbol(self, symbol: str) -> None:
        """Subscribe to real-time tick prices for a symbol (H0UNCNT0: 통합 실시간 체결가)."""
        with self._lock:
            if symbol in self._subscribed_symbols:
                return
            self._subscribed_symbols.add(symbol)

        if self.is_connected:
            approval_key = self.token_mgr.get_approval_key()
            await self._send_subscription(symbol, tr_type="1", approval_key=approval_key)

    async def unsubscribe_symbol(self, symbol: str) -> None:
        """Unsubscribe from real-time tick prices for a symbol."""
        with self._lock:
            if symbol not in self._subscribed_symbols:
                return
            self._subscribed_symbols.discard(symbol)

        if self.is_connected:
            approval_key = self.token_mgr.get_approval_key()
            await self._send_subscription(symbol, tr_type="2", approval_key=approval_key)

    async def _send_subscription(self, symbol: str, tr_type: str, approval_key: str) -> None:
        """Sends JSON subscription control frame to KIS WebSocket."""
        ws = self._ws
        if not ws:
            return

        payload = {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": tr_type,
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": "H0UNCNT0",
                    "tr_key": symbol,
                }
            },
        }
        try:
            await ws.send(json.dumps(payload))
            logger.debug(f"[KIS_WS] WS subscription request sent: {symbol} (tr_type={tr_type})")
        except Exception as e:
            logger.warning(f"[KIS_WS] Failed to send WS subscription for {symbol}: {e}")

    async def _subscribe_execution_notice(self, approval_key: str) -> None:
        """Subscribe to real-time account execution notifications (H0STCNI0/H0STCNI9)."""
        ws = self._ws
        if not ws:
            return

        tr_id = "H0STCNI0" if self.cfg.env == "real" else "H0STCNI9"
        payload = {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": tr_id,
                    "tr_key": self.cfg.hts_id,
                }
            },
        }
        try:
            await ws.send(json.dumps(payload))
            logger.info(f"[KIS_WS] Subscribed to execution notifications ({tr_id}) for HTS_ID: {self.cfg.hts_id}")
        except Exception as e:
            logger.warning(f"[KIS_WS] Failed to subscribe to execution notifications: {e}")

    async def _handle_message(self, raw: str) -> None:
        """Dispatches incoming raw WebSocket text frame to handler."""
        # 1. PINGPONG keepalive frame: echo back immediately to maintain session
        if "PINGPONG" in raw:
            if self._ws:
                await self._ws.send(raw)
            return

        # 2. JSON control frame (pingpong, system messages, subscription ack)
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
                tr_id = data.get("header", {}).get("tr_id")
                if tr_id == "PINGPONG":
                    if self._ws:
                        await self._ws.send(raw)
                    return
                msg_cd = data.get("body", {}).get("msg_cd")
                output = data.get("body", {}).get("output", {})
                if tr_id in ("H0STCNI0", "H0STCNI9") and output:
                    self._aes_iv = output.get("iv")
                    self._aes_key = output.get("key")
                    logger.info(f"[KIS_WS] Execution notice AES encryption key acquired for {tr_id}")
                logger.debug(f"[KIS_WS] Control frame: tr_id={tr_id}, msg={msg_cd}")
                telemetry.record_ws_frame(frame_type="CONTROL", tr_id=tr_id or "UNKNOWN", data={"msg_cd": msg_cd})
            except Exception as e:
                logger.debug(f"[KIS_WS] Error parsing control frame: {e}")
            return

        # 3. Pipe-delimited real-time tick stream
        parts = raw.split("|")
        if len(parts) < 4:
            return

        enc_flag, tr_id, cnt, data_str = parts[0], parts[1], parts[2], parts[3]

        if tr_id in ("H0STCNT0", "H0UNCNT0"):
            # Real-time tick data (KRX alone or KRX+NXT consolidated)
            fields = data_str.split("^")
            if len(fields) < 18:
                return

            vol = _safe_int(fields[12]) if len(fields) > 12 and fields[12] else (_safe_int(fields[11]) if len(fields) > 11 else 0)
            cum_vol = _safe_int(fields[13]) if len(fields) > 13 and fields[13] else (_safe_int(fields[12]) if len(fields) > 12 else 0)
            cum_to = _safe_float(fields[14]) if len(fields) > 14 and fields[14] else (_safe_float(fields[13]) if len(fields) > 13 else 0.0)

            # Volume Power (체결강도 %)
            vol_power = 0.0
            if len(fields) > 19:
                try:
                    sel_qty = _safe_float(fields[18])
                    buy_qty = _safe_float(fields[19])
                    if sel_qty > 0:
                        vol_power = round((buy_qty / sel_qty) * 100.0, 2)
                except (ValueError, TypeError):
                    pass

            if vol_power <= 0 and len(fields) > 17:
                try:
                    fallback_val = _safe_float(fields[17])
                    if fallback_val > 0:
                        vol_power = fallback_val
                except (ValueError, TypeError):
                    pass

            if vol_power <= 0:
                vol_power = 120.0  # safe fallback

            # Field offsets: H0UNCNT0 (통합) vs H0STCNT0 (단독)
            if tr_id == "H0UNCNT0":
                vwap_val = _safe_float(fields[7]) if len(fields) > 7 else 0.0
                open_val = _safe_float(fields[8]) if len(fields) > 8 else 0.0
                high_val = _safe_float(fields[9]) if len(fields) > 9 else 0.0
                low_val = _safe_float(fields[10]) if len(fields) > 10 else 0.0
            else:
                vwap_val = _safe_float(fields[6]) if len(fields) > 6 else 0.0
                open_val = _safe_float(fields[7]) if len(fields) > 7 else 0.0
                high_val = _safe_float(fields[8]) if len(fields) > 8 else 0.0
                low_val = _safe_float(fields[9]) if len(fields) > 9 else 0.0

            tick = {
                "symbol": fields[0],
                "time": fields[1],                    # HHMMSS
                "price": _safe_float(fields[2]),      # 현재가
                "sign": fields[3],                    # 대비부호
                "change": _safe_float(fields[4]),     # 전일대비
                "change_rate": _safe_float(fields[5]),# 등락률 (%)
                "vwap": vwap_val,                     # 당일가중평균가
                "open": open_val,                     # 시가
                "high": high_val,                     # 고가
                "low": low_val,                       # 저가
                "volume": vol,
                "cum_volume": cum_vol,
                "cum_turnover": cum_to,
                "volume_power": vol_power,
            }
            if tick["price"] <= 0:
                return

            telemetry.record_ws_frame(
                frame_type="TICK",
                tr_id=tr_id,
                symbol=tick["symbol"],
                data={
                    "price": tick["price"],
                    "volume": tick["volume"],
                    "cum_turnover": tick["cum_turnover"],
                    "vwap": tick["vwap"],
                },
            )

            with self._lock:
                cbs = list(self._callbacks)

            for cb in cbs:
                try:
                    await cb("TICK", tick)
                except Exception as e:
                    logger.error(f"[KIS_WS] Callback error processing tick: {e}", exc_info=True)

        elif tr_id in ("H0STCNI0", "H0STCNI9"):
            # Execution notice frame
            if enc_flag == "1":
                if self._aes_key and self._aes_iv:
                    if AES is None or unpad is None:
                        logger.error("[KIS_WS] Cryptodome or Crypto package required to decrypt execution notice.")
                        return
                    try:
                        cipher = AES.new(self._aes_key.encode("utf-8"), AES.MODE_CBC, self._aes_iv.encode("utf-8"))
                        decrypted_bytes = unpad(cipher.decrypt(base64.b64decode(data_str)), AES.block_size)
                        data_str = decrypted_bytes.decode("utf-8")
                    except Exception as e:
                        logger.error(f"[KIS_WS] Failed to decrypt execution notice: {e}")
                        return
                else:
                    logger.error("[KIS_WS] Received encrypted execution notice but AES key/IV not available")
                    return

            fields = data_str.split("^")
            if len(fields) >= 14:
                cntg_yn = fields[13]
                # 1 = Order accepted / modified / cancelled, 2 = Execution fill
                if cntg_yn != "2":
                    logger.info(f"[KIS_WS] Order receipt/update notice: order_no={fields[2]}, status={cntg_yn}")
                    with self._lock:
                        cbs = list(self._callbacks)
                    for cb in cbs:
                        try:
                            await cb("ORDER_NOTICE", {"order_no": fields[2], "status": cntg_yn})
                        except Exception:
                            pass
                    return

                raw_sym = (
                    fields[8] if len(fields[8]) in (6, 7)
                    else (fields[7] if len(fields) > 7 and len(fields[7]) in (6, 7) else fields[3])
                )
                sym = raw_sym.lstrip("A") if len(raw_sym) == 7 and raw_sym.startswith("A") else raw_sym
                exec_notice = {
                    "account_no": fields[1],
                    "order_no": fields[2],
                    "symbol": sym,
                    "side": "BUY" if fields[4] == "02" else "SELL",
                    "qty": _safe_int(fields[9]),
                    "price": _safe_float(fields[10]),
                }
            else:
                raw_sym = fields[3] if len(fields) > 3 else ""
                sym = raw_sym.lstrip("A") if len(raw_sym) == 7 and raw_sym.startswith("A") else raw_sym
                exec_notice = {
                    "account_no": fields[0] if len(fields) > 0 else "",
                    "order_no": fields[1] if len(fields) > 1 else "",
                    "symbol": sym,
                    "side": "BUY" if len(fields) > 4 and fields[4] == "02" else "SELL",
                    "qty": _safe_int(fields[5]) if len(fields) > 5 else 0,
                    "price": _safe_float(fields[6]) if len(fields) > 6 else 0.0,
                }

            telemetry.record_ws_frame(
                frame_type="EXECUTION_NOTICE",
                tr_id=tr_id,
                symbol=exec_notice.get("symbol", ""),
                data=exec_notice,
            )

            with self._lock:
                cbs = list(self._callbacks)

            for cb in cbs:
                try:
                    await cb("EXECUTION", exec_notice)
                except Exception as e:
                    logger.error(f"[KIS_WS] Callback error processing execution notice: {e}", exc_info=True)
