"""
Korea Investment & Securities (KIS) WebSocket Client
Handles real-time tick prices (H0UNCNT0, H0STCNT0), and execution notices (H0STCNI0, H0STCNI9).
"""
from __future__ import annotations

import base64
import json
import logging
import asyncio
from typing import Dict, Any, Callable, Awaitable, Set, Optional
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
        self._subscribed_symbols: Set[str] = set()
        self._callbacks: list[TickCallback] = []
        self._reconnect_delay = 3.0
        self._aes_key: Optional[str] = None
        self._aes_iv: Optional[str] = None

    @property
    def ws_url(self) -> str:
        # KIS WebSocket endpoint (H0UNCNT0: 통합 실시간 체결가)
        return f"{self.cfg.endpoints.ws_url}/tryitout/H0UNCNT0"

    def register_callback(self, cb: TickCallback) -> None:
        """Register async callback function for incoming market ticks and executions."""
        self._callbacks.append(cb)

    async def connect(self) -> None:
        """Establishes WebSocket connection and starts listener loop."""
        self._running = True

        while self._running:
            try:
                approval_key = self.token_mgr.get_approval_key()
                logger.info(f"Connecting to KIS WebSocket: {self.ws_url}")
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    logger.info("WebSocket connected successfully.")

                    # Resubscribe to previous symbols if reconnecting
                    for sym in list(self._subscribed_symbols):
                        await self._send_subscription(sym, tr_type="1", approval_key=approval_key)

                    # Also subscribe to user execution notice if HTS_ID provided
                    if self.cfg.hts_id:
                        await self._subscribe_execution_notice(approval_key)

                    # Listen for messages
                    async for message in ws:
                        if not self._running:
                            break
                        await self._handle_message(message)

            except (websockets.ConnectionClosed, OSError) as e:
                logger.warning(f"WebSocket connection lost: {e}. Reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
            except Exception as e:
                logger.error(f"Unexpected WebSocket error: {e}", exc_info=True)
                await asyncio.sleep(self._reconnect_delay)

    @property
    def is_connected(self) -> bool:
        """Checks whether the underlying WebSocket connection is open."""
        if not self._ws:
            return False
        if hasattr(self._ws, "open"):
            return bool(self._ws.open)
        if hasattr(self._ws, "state"):
            try:
                return self._ws.state == 1 or getattr(self._ws.state, "name", "") == "OPEN"
            except Exception:
                return self._ws.state == 1
        return True

    async def disconnect(self) -> None:
        """Gracefully disconnect WebSocket."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def subscribe_symbol(self, symbol: str) -> None:
        """Subscribe to real-time tick prices for a symbol (H0UNCNT0: 통합 실시간 체결가)."""
        if symbol in self._subscribed_symbols:
            return
        self._subscribed_symbols.add(symbol)
        if self.is_connected:
            approval_key = self.token_mgr.get_approval_key()
            await self._send_subscription(symbol, tr_type="1", approval_key=approval_key)

    async def unsubscribe_symbol(self, symbol: str) -> None:
        """Unsubscribe from real-time tick prices for a symbol."""
        if symbol not in self._subscribed_symbols:
            return
        self._subscribed_symbols.remove(symbol)
        if self.is_connected:
            approval_key = self.token_mgr.get_approval_key()
            await self._send_subscription(symbol, tr_type="2", approval_key=approval_key)

    async def _send_subscription(self, symbol: str, tr_type: str, approval_key: str) -> None:
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
        await self._ws.send(json.dumps(payload))
        logger.debug(f"WS subscription request sent: {symbol} (tr_type={tr_type})")

    async def _subscribe_execution_notice(self, approval_key: str) -> None:
        """Subscribe to real-time account execution notifications (H0STCNI0/H0STCNI9)."""
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
        await self._ws.send(json.dumps(payload))
        logger.info(f"Subscribed to execution notifications ({tr_id}) for HTS_ID: {self.cfg.hts_id}")

    async def _handle_message(self, raw: str) -> None:
        # Check PINGPONG frame and echo back immediately to maintain session
        if "PINGPONG" in raw:
            if self._ws:
                await self._ws.send(raw)
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
                msg_cd = data.get("body", {}).get("msg_cd")
                output = data.get("body", {}).get("output", {})
                if tr_id in ("H0STCNI0", "H0STCNI9") and output:
                    self._aes_iv = output.get("iv")
                    self._aes_key = output.get("key")
                    logger.info(f"Execution notice AES encryption key acquired for {tr_id}")
                logger.debug(f"WS Control: tr_id={tr_id}, msg={msg_cd}")
                telemetry.record_ws_frame(frame_type="CONTROL", tr_id=tr_id or "UNKNOWN", data={"msg_cd": msg_cd})
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

            # Volume & turnover indexing:
            # 12: 체결수량 (CNTG_VOL)
            # 13: 누적체결량 (ACML_VOL)
            # 14: 누적거래대금 (ACML_TR_PBMN)
            vol = int(fields[12]) if len(fields) > 12 and fields[12].lstrip("-").isdigit() else (int(fields[11]) if len(fields) > 11 and fields[11].lstrip("-").isdigit() else 0)
            cum_vol = int(fields[13]) if len(fields) > 13 and fields[13].lstrip("-").isdigit() else (int(fields[12]) if len(fields) > 12 and fields[12].lstrip("-").isdigit() else 0)
            cum_to = float(fields[14]) if len(fields) > 14 and fields[14].replace(".", "", 1).lstrip("-").isdigit() else (float(fields[13]) if len(fields) > 13 and fields[13].replace(".", "", 1).lstrip("-").isdigit() else 0.0)

            # Volume Power (체결강도 %):
            # KIS H0STCNT0 fields:
            # 18: 총 매도수량 (TOTAL_SELN_QTY)
            # 19: 총 매수수량 (TOTAL_SHNU_QTY)
            # 체결강도 = (총 매수수량 / 총 매도수량) * 100
            vol_power = 0.0
            if len(fields) > 19:
                try:
                    sel_qty = float(fields[18])
                    buy_qty = float(fields[19])
                    if sel_qty > 0:
                        vol_power = round((buy_qty / sel_qty) * 100.0, 2)
                except (ValueError, TypeError):
                    pass

            if vol_power <= 0 and len(fields) > 17:
                try:
                    fallback_val = float(fields[17])
                    if fallback_val > 0:
                        vol_power = fallback_val
                except (ValueError, TypeError):
                    pass

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

            for cb in self._callbacks:
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
                    "qty": int(fields[9]) if fields[9].isdigit() else 0,
                    "price": float(fields[10]) if fields[10].replace(".", "", 1).isdigit() else 0.0,
                }
            else:
                exec_notice = {
                    "account_no": fields[0] if len(fields) > 0 else "",
                    "order_no": fields[1] if len(fields) > 1 else "",
                    "symbol": sym,
                    "side": "BUY" if len(fields) > 4 and fields[4] == "02" else "SELL",
                    "qty": int(fields[5]) if len(fields) > 5 and fields[5].isdigit() else 0,
                    "price": float(fields[6]) if len(fields) > 6 else 0.0,
                }

            telemetry.record_ws_frame(
                frame_type="EXECUTION_NOTICE",
                tr_id=tr_id,
                symbol=exec_notice.get("symbol", ""),
                data=exec_notice,
            )

            for cb in self._callbacks:
                try:
                    await cb("EXECUTION", exec_notice)
                except Exception as e:
                    logger.error(f"Callback error processing execution notice: {e}")
