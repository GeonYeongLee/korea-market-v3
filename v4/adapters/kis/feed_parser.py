"""
KIS WebSocket Frame Parser & Quiet-Bar Wall-Clock Accumulator for Korea Market v4.
Handles multi-record (`cnt > 1`) `H0UNCNT0`/`H0STCNT0` tick frames, AES-encrypted
`H0STCNI0`/`H0STCNI9` execution notices, and `M+1:00.1` quiet-symbol bar rollover.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from v4.adapters.kis._parsers import safe_float as _sf
from v4.adapters.kis._parsers import safe_int as _si
from v4.adapters.kis.bar_accumulator import BarAccumulator, _FormingBarState  # noqa: F401
from v4.models import TickEvent

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

logger = logging.getLogger("korea_market.v4.adapters.kis.feed_parser")


def _parse_hhmmss(hhmmss: str, base_dt: datetime) -> datetime:
    s = hhmmss.strip().zfill(6)
    if len(s) >= 6 and s[:6].isdigit():
        try:
            return base_dt.replace(
                hour=int(s[:2]), minute=int(s[2:4]), second=int(s[4:6]), microsecond=0
            )
        except ValueError:
            pass
    return base_dt


def parse_tick_records(
    tr_id: str, cnt: int, data_str: str, base_dt: datetime
) -> List[TickEvent]:
    """Parses single or multi-record (`cnt > 1`) KIS WS tick payloads into `TickEvent`s."""
    fields = data_str.split("^")
    n_cnt = max(1, int(cnt))
    if n_cnt > 1:
        stride = (len(fields) // n_cnt) if (len(fields) >= n_cnt * 18 and len(fields) % n_cnt == 0) else 46
        records = [fields[i * stride : (i + 1) * stride] for i in range(n_cnt)]
    else:
        records = [fields]

    events: List[TickEvent] = []
    for rec in records:
        if len(rec) < 18:
            continue
        price = _sf(rec[2])
        if price <= 0.0:
            continue

        symbol = rec[0].strip()
        dt = _parse_hhmmss(rec[1], base_dt)
        vol = _si(rec[12]) if len(rec) > 12 else _si(rec[11])
        cum_vol = _si(rec[13]) if len(rec) > 13 else _si(rec[12])
        cum_to = _sf(rec[14]) if len(rec) > 14 else _sf(rec[13])
        vwap = _sf(rec[6])
        open_p = _sf(rec[7])
        low_p = _sf(rec[9])

        # KIS H0STCNT0/H0UNCNT0: 18=CTTR, 19=SELN_CNTG_SMTN, 20=SHNU_CNTG_SMTN (7adbfb2)
        vol_power = 0.0
        if len(rec) > 20:
            sel_q, buy_q = _sf(rec[19]), _sf(rec[20])
            if sel_q > 0.0 and buy_q > 0.0:
                vol_power = round((buy_q / sel_q) * 100.0, 2)
        if vol_power <= 0.0 and len(rec) > 19:
            sel_or_cttr, buy_or_seln = _sf(rec[18]), _sf(rec[19])
            if sel_or_cttr > 0.0 and buy_or_seln > 0.0 and len(rec) <= 20:
                vol_power = round((buy_or_seln / sel_or_cttr) * 100.0, 2)
            elif sel_or_cttr > 0.0:
                vol_power = round(sel_or_cttr, 2)
        if vol_power <= 0.0:
            vol_power = 120.0

        is_krx = tr_id == "H0STCNT0"
        events.append(
            TickEvent(
                symbol=symbol,
                dt=dt,
                price=price,
                volume=max(0, vol),
                turnover=float(max(0, vol)) * price,
                cum_turnover=cum_to,
                cum_volume=max(0, cum_vol),
                bid=_sf(rec[11]) if len(rec) > 11 else 0.0,
                ask=_sf(rec[10]) if len(rec) > 10 else 0.0,
                day_open=open_p if is_krx else 0.0,
                krx_day_open=open_p if is_krx else 0.0,
                raw_day_open=open_p,
                krx_day_low=low_p if is_krx else 0.0,
                raw_day_low=low_p,
                vwap=vwap,
                volume_power=vol_power,
                tr_id=tr_id,
            )
        )
    return events


def parse_execution_notice(
    enc_flag: str,
    data_str: str,
    aes_key: Optional[str],
    aes_iv: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Decrypts (if needed) and parses `H0STCNI0`/`H0STCNI9` execution notices."""
    payload = data_str
    if enc_flag == "1":
        if not aes_key or not aes_iv or AES is None or unpad is None:
            logger.error("Cannot decrypt execution notice: AES key/IV or Cryptodome missing.")
            return None
        try:
            cipher = AES.new(aes_key.encode("utf-8"), AES.MODE_CBC, aes_iv.encode("utf-8"))
            payload = unpad(cipher.decrypt(base64.b64decode(data_str)), AES.block_size).decode("utf-8")
        except Exception as exc:
            logger.error(f"Failed to decrypt execution notice: {exc}")
            return None

    fields = payload.split("^")
    sym = ""
    for idx in (7, 8, 3):
        if idx < len(fields):
            cand = fields[idx].strip().lstrip("A")
            if len(cand) == 6 and cand.isdigit():
                sym = cand
                break
    if not sym:
        for raw in fields:
            cand = raw.strip().lstrip("A")
            if len(cand) == 6 and cand.isdigit():
                sym = cand
                break

    if len(fields) >= 14:
        if fields[13].strip() != "2":  # "2" = 체결 통보
            return None
        return {
            "account_no": fields[1].strip(),
            "order_no": fields[2].strip(),
            "symbol": sym,
            "side": "BUY" if fields[4].strip() == "02" else "SELL",
            "qty": _si(fields[9]),
            "price": _sf(fields[10]),
        }
    return {
        "account_no": fields[0].strip() if len(fields) > 0 else "",
        "order_no": fields[1].strip() if len(fields) > 1 else "",
        "symbol": sym,
        "side": "BUY" if len(fields) > 4 and fields[4].strip() == "02" else "SELL",
        "qty": _si(fields[5]) if len(fields) > 5 else 0,
        "price": _sf(fields[6]) if len(fields) > 6 else 0.0,
    }


def build_ws_sub_payload(symbol: str, tr_id: str, tr_type: str, approval_key: str) -> str:
    """Builds JSON subscription payload for KIS WebSocket."""
    import json
    return json.dumps({
        "header": {
            "approval_key": approval_key,
            "custtype": "P",
            "tr_type": tr_type,
            "content-type": "utf-8",
        },
        "body": {"input": {"tr_id": tr_id, "tr_key": symbol}},
    })


def parse_ws_control_frame(raw: str) -> Dict[str, Any]:
    """Parses JSON control frame; flags `pingpong` or `auth_failed` and extracts AES key/IV."""
    import json
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    tr_id = data.get("header", {}).get("tr_id")
    if tr_id == "PINGPONG":
        return {"pingpong": True}
    body = data.get("body", {})
    msg_cd, msg1 = body.get("msg_cd"), str(body.get("msg1", ""))
    if msg_cd in ("OPSP0011", "OPSP0012", "OPSP0013") or "invalid approval" in msg1.lower() or "not found" in msg1.lower():
        return {"auth_failed": True, "msg_cd": msg_cd, "msg1": msg1}
    out = body.get("output", {})
    if tr_id in ("H0STCNI0", "H0STCNI9") and out:
        return {"aes_iv": out.get("iv"), "aes_key": out.get("key")}
    return {}
