"""
Korea Investment & Securities (KIS) WebSocket Client
Handles real-time tick prices (H0UNCNT0, H0STCNT0), and execution notices (H0STCNI0, H0STCNI9).
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
    try:
        if v is None:
            return default
        s = str(v).strip()
        return float(s) if s else default
    except (ValueError, TypeError):
        return default


def _safe_int(v: Any, default: int = 0) -> int:
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
        self.connection_state: str = "DISCONNECTED"
        self.last_msg_time: float = 0.0
        self.total_messages_received: int = 0

    @property
    def ws_url(self) -> str:
        # KIS WebSocket endpoint (H0UNCNT0: 통합 실시간 체결가)
        return f"{self.cfg.endpoints.ws_url}/tryitout/H0UNCNT0"

    def register_callback(self, cb: TickCallback) -> None:
        """Register async callback function for incoming market ticks and executions."""
        with self._lock:
            self._callbacks.append(cb)

    @property
    def is_healthy(self) -> bool:
        """Checks whether WebSocket connection is healthy and active."""
        return self.connection_state == "CONNECTED" and self.is_connected

    async def hard_reconnect(self) -> None:
        """Forces WebSocket reconnect with newly acquired credentials."""
        logger.info("Initiating hard WebSocket reconnect...")
        self.token_mgr.invalidate_approval_key()
        self.connection_state = "DISCONNECTED"
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def connect(self) -> None:
        """Establishes WebSocket connection and starts listener loop."""
        self._running = True

        while self._running:
            if self.connection_state == "AUTH_FAILED":
                # Do not blindly loop when auth has failed; wait for watchdog/reboot
                await asyncio.sleep(self._reconnect_delay)
                continue

            self.connection_state = "CONNECTING"
            try:
                approval_key = self.token_mgr.get_approval_key()
                logger.info(f"Connecting to KIS WebSocket: {self.ws_url}")
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    self.connection_state = "CONNECTED"
                    logger.info("WebSocket connected successfully.")

                    # Resubscribe to previous symbols if reconnecting
                    with self._lock:
                        current_subs = list(self._subscribed_symbols)
                    for sym in current_subs:
                        await self._send_subscription(sym, tr_type="1", approval_key=approval_key)

                    # Also subscribe to user execution notice if HTS_ID provided
                    if self.cfg.hts_id:
                        await self._subscribe_execution_notice(approval_key)

                    # Listen for messages
                    async for message in ws:
                        if not self._running:
                            break
                        self.last_msg_time = asyncio.get_event_loop().time()
                        self.total_messages_received += 1
                        await self._handle_message(message)
                        if self.connection_state == "AUTH_FAILED":
                            break

            except (websockets.ConnectionClosed, OSError) as e:
                if self.connection_state != "AUTH_FAILED":
                    self.connection_state = "DISCONNECTED"
                if not self._running:
                    logger.info("WebSocket connection closed cleanly during shutdown.")
                    break
                if self.connection_state != "AUTH_FAILED":
                    logger.warning(f"WebSocket connection lost: {e}. Reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
            except asyncio.CancelledError:
                logger.info("WebSocket listener task cancelled.")
                break
            except Exception as e:
                err_str = str(e).lower()
                if "401" in err_str or "403" in err_str or "approval" in err_str or "auth" in err_str:
                    logger.error(f"🚨 [WS Auth Failed] WebSocket connection failed with authentication error: {e}")
                    self.connection_state = "AUTH_FAILED"
                    self.token_mgr.invalidate_approval_key()
                else:
                    if self.connection_state != "AUTH_FAILED":
                        self.connection_state = "DISCONNECTED"
                    if not self._running:
                        break
                    logger.error(f"Unexpected WebSocket error: {e}", exc_info=True)
                if not self._running:
                    break
                await asyncio.sleep(self._reconnect_delay)
            finally:
                self._ws = None
                if self.connection_state != "AUTH_FAILED" and not self._running:
                    self.connection_state = "DISCONNECTED"

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

    async def disconnect(self) -> None:
        """Gracefully disconnect WebSocket."""
        self._running = False
        self.connection_state = "DISCONNECTED"
        ws = self._ws
        self._ws = None
        if ws:
            try:
                await ws.close()
            except Exception as e:
                logger.debug(f"Error closing WebSocket: {e}")

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
            logger.debug(f"WS subscription request sent: {symbol} (tr_type={tr_type})")
        except Exception as e:
            logger.warning(f"Failed to send WS subscription for {symbol}: {e}")

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
            logger.info(f"Subscribed to execution notifications ({tr_id}) for HTS_ID: {self.cfg.hts_id}")
        except Exception as e:
            logger.warning(f"Failed to subscribe to execution notifications: {e}")

    async def _handle_message(self, raw: str) -> None:
        # Check PINGPONG frame and echo back immediately to maintain session
        if "PINGPONG" in raw:
            ws = self._ws
            if ws:
                try:
                    await ws.send(raw)
                except Exception as e:
                    logger.debug(f"Failed to echo PINGPONG frame: {e}")
            return

        # Case 1: JSON control frame (pingpong, system messages, subscription ack)
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
                tr_id = data.get("header", {}).get("tr_id")
                if tr_id == "PINGPONG":
                    if self._ws:
                        await self._ws.send(raw)
                    return
                body = data.get("body", {})
                msg_cd = body.get("msg_cd")
                msg1 = body.get("msg1", "")

                # Detect authentication failure codes (e.g. OPSP0011, OPSP0012, or invalid approval message)
                if (
                    msg_cd in ("OPSP0011", "OPSP0012", "OPSP0013")
                    or "invalid approval" in msg1.lower()
                    or "not found" in msg1.lower()
                ):
                    logger.error(
                        f"🚨 [WS Auth Failed] KIS auth failure frame received: msg_cd={msg_cd}, msg1={msg1}. "
                        "Marking connection_state='AUTH_FAILED' and invalidating approval key."
                    )
                    self.connection_state = "AUTH_FAILED"
                    self.token_mgr.invalidate_approval_key()
                    telemetry.record_ws_frame(
                        frame_type="CONTROL",
                        tr_id=tr_id or "AUTH_FAILED",
                        data={"msg_cd": msg_cd, "msg1": msg1, "error": "AUTH_FAILED"},
                    )
                    if self._ws:
                        await self._ws.close()
                    return

                output = body.get("output", {})
                if tr_id in ("H0STCNI0", "H0STCNI9") and output:
                    self._aes_iv = output.get("iv")
                    self._aes_key = output.get("key")
                    logger.info(f"Execution notice AES encryption key acquired for {tr_id}")
                logger.debug(f"WS Control: tr_id={tr_id}, msg={msg_cd}")
                telemetry.record_ws_frame(frame_type="CONTROL", tr_id=tr_id or "UNKNOWN", data={"msg_cd": msg_cd, "msg1": msg1})
            except Exception:
                pass
            return

        # Case 2: Pipe-delimited real-time tick stream
        parts = raw.split("|")
        if len(parts) < 4:
            return

        enc_flag, tr_id, cnt, data_str = parts[0], parts[1], parts[2], parts[3]

        if tr_id in ("H0STCNT0", "H0UNCNT0"):
            # Real-time tick data (KRX 단독 또는 KRX+NXT 통합 실시간 체결가)
            fields = data_str.split("^")
            if len(fields) < 18:
                return

            vol = _safe_int(fields[12]) if len(fields) > 12 else 0
            cum_vol = _safe_int(fields[13]) if len(fields) > 13 else 0
            cum_to = _safe_float(fields[14]) if len(fields) > 14 else 0.0

            # Volume Power (체결강도 %):
            # KIS H0STCNT0/H0UNCNT0:
            #   18: CTTR (체결강도), 19: SELN_CNTG_SMTN (총 매도수량), 20: SHNU_CNTG_SMTN (총 매수수량)
            vol_power = 0.0
            if len(fields) > 20:
                sel_qty = _safe_float(fields[19])
                buy_qty = _safe_float(fields[20])
                if sel_qty > 0 and buy_qty > 0:
                    vol_power = round((buy_qty / sel_qty) * 100.0, 2)

            if vol_power <= 0 and len(fields) > 19:
                sel_or_cttr = _safe_float(fields[18])
                buy_or_seln = _safe_float(fields[19])
                if sel_or_cttr > 0 and buy_or_seln > 0 and len(fields) <= 20:
                    vol_power = round((buy_or_seln / sel_or_cttr) * 100.0, 2)
                elif sel_or_cttr > 0:
                    vol_power = round(sel_or_cttr, 2)

            if vol_power <= 0:
                vol_power = 120.0  # safe fallback

            tick = {
                "symbol": fields[0],
                "time": fields[1],                    # HHMMSS
                "price": _safe_float(fields[2]),      # 현재가
                "sign": fields[3],                    # 대비부호
                "change": _safe_float(fields[4]),     # 전일대비
                "change_rate": _safe_float(fields[5]),# 등락률 (%)
                "vwap": _safe_float(fields[6]),       # 당일가중평균가
                "open": _safe_float(fields[7]),       # 시가
                "high": _safe_float(fields[8]),       # 고가
                "low": _safe_float(fields[9]),        # 저가
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
                tick_callbacks = list(self._callbacks)
            for cb in tick_callbacks:
                try:
                    await cb("TICK", tick)
                except Exception as e:
                    logger.error(f"Callback error processing tick: {e}", exc_info=True)

        elif tr_id in ("H0STCNI0", "H0STCNI9"):
            # Execution notice
            if enc_flag == "1":
                if self._aes_key and self._aes_iv:
                    if AES is None or unpad is None:
                        logger.error("Cryptodome or Crypto package is required to decrypt execution notice.")
                        return
                    try:
                        cipher = AES.new(self._aes_key.encode("utf-8"), AES.MODE_CBC, self._aes_iv.encode("utf-8"))
                        decrypted_bytes = unpad(cipher.decrypt(base64.b64decode(data_str)), AES.block_size)
                        data_str = decrypted_bytes.decode("utf-8")
                    except Exception as e:
                        logger.error(f"Failed to decrypt execution notice: {e}")
                        return
                else:
                    logger.error("Received encrypted execution notice but AES key/IV not available")
                    return

            fields = data_str.split("^")
            sym = ""
            for idx in (7, 8, 3):
                if idx < len(fields):
                    c = fields[idx].strip().lstrip("A")
                    if len(c) == 6 and c.isdigit():
                        sym = c
                        break
            if not sym:
                for c_raw in fields:
                    c = c_raw.strip().lstrip("A")
                    if len(c) == 6 and c.isdigit():
                        sym = c
                        break

            if len(fields) >= 14:
                cntg_yn = fields[13]
                # 1 = 주문/접수/취소 통보, 2 = 체결 통보
                if cntg_yn != "2":
                    logger.info(f"Order receipt/update notice: order_no={fields[2]}, status={cntg_yn}")
                    return
                exec_notice = {
                    "account_no": fields[1],
                    "order_no": fields[2],
                    "symbol": sym,
                    "side": "BUY" if fields[4] == "02" else "SELL",
                    "qty": _safe_int(fields[9]),
                    "price": _safe_float(fields[10]),
                }
            else:
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
                exec_callbacks = list(self._callbacks)
            for cb in exec_callbacks:
                try:
                    await cb("EXECUTION", exec_notice)
                except Exception as e:
                    logger.error(f"Callback error processing execution notice: {e}")
