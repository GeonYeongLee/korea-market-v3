"""
KIS REST Order Submission, Phase-2 Recovery & Net PnL Helpers for Korea Market v4.
No `/uapi/hashkey` calls; stops retrying immediately on `rt_cd == "1"`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

import requests

from core.krx_ticks import round_to_krx_tick
from core.rest_client import KISRestClient
from v4.adapters._broker_common import compute_sell_net_pnl  # noqa: F401 (re-export)
from v4.models import OrderRequest, OrderSide, OrderType

logger = logging.getLogger("korea_market.v4.adapters.kis.broker_orders")


def to_kis_ord_dvsn(order_type: OrderType) -> str:
    if order_type == OrderType.LIMIT:
        return "00"
    if order_type == OrderType.BEST_LIMIT:
        return "03"
    return "01"


def resolve_order_price(req: OrderRequest) -> int:
    """Rounds LIMIT prices via `round_to_krx_tick`; returns 0 for MARKET/BEST_LIMIT."""
    if req.order_type != OrderType.LIMIT or not req.price or req.price <= 0.0:
        return 0
    return int(round_to_krx_tick(float(req.price), side=req.side.value))


def _format_http_error(resp: Any, err: Exception) -> str:
    if resp is not None and hasattr(resp, "json"):
        try:
            body = resp.json()
            if isinstance(body, dict) and any(k in body for k in ("rt_cd", "msg_cd", "msg1")):
                return f"rt_cd={body.get('rt_cd')} msg_cd={body.get('msg_cd')} msg1={body.get('msg1')}"
        except Exception:
            pass
    return str(err)


def _find_ccld_match(
    records: List[Dict[str, Any]], symbol: str, qty: int, side: OrderSide
) -> Optional[Dict[str, Any]]:
    expected_code = "02" if side == OrderSide.BUY else "01"
    for rec in records or []:
        r_sym = str(rec.get("pdno") or rec.get("PDNO") or "").strip()
        r_qty = int(float(rec.get("ord_qty") or rec.get("ORD_QTY") or 0))
        r_side = str(rec.get("sll_buy_dvsn_cd") or rec.get("SLL_BUY_DVSN_CD") or "").strip()
        if r_sym == symbol and r_qty == qty and r_side in (expected_code, side.value, ""):
            return rec
    return None


async def submit_rest_order(
    client: KISRestClient,
    req: OrderRequest,
    rounded_price: int,
    exchange: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Submits BUY/SELL order via `KISRestClient` (no hashkey).
    - Stops immediately on `rt_cd == "1"` or non-retryable 4xx/500 HTTP rejection.
    - On network timeout / 502-504, performs 1.0s Phase-2 `inquire_daily_ccld` verification.
    """
    ord_dvsn = to_kis_ord_dvsn(req.order_type)
    fn = client.order_cash_buy if req.side == OrderSide.BUY else client.order_cash_sell
    eff_exchange = exchange if exchange is not None else req.exchange

    try:
        resp = await asyncio.to_thread(
            fn, req.symbol, req.quantity, rounded_price, ord_dvsn, eff_exchange
        )
        if (
            req.exchange is None
            and eff_exchange == "SOR"
            and str(resp.get("rt_cd", "")) != "0"
            and "SOR" in str(resp.get("msg1", "")).upper()
        ):
            logger.warning(
                "%s order SOR rejected for %s (%s); retrying on KRX...",
                req.side.value, req.symbol, resp.get("msg1", ""),
            )
            resp = await asyncio.to_thread(
                fn, req.symbol, req.quantity, rounded_price, ord_dvsn, "KRX"
            )
        if str(resp.get("rt_cd", "")) == "1":
            logger.error(
                "Business rejection rt_cd=1 for %s %s: %s",
                req.side.value, req.symbol, resp.get("msg1", ""),
            )
        return resp
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.RequestException) as net_err:
        if isinstance(net_err, requests.exceptions.HTTPError):
            resp_obj = getattr(net_err, "response", None)
            status = getattr(resp_obj, "status_code", 0)
            if status not in (502, 503, 504):
                detail = _format_http_error(resp_obj, net_err)
                logger.error("Order HTTP %s rejected for %s: %s", status, req.symbol, detail)
                return {"rt_cd": "1", "msg1": detail}

        logger.warning("Network timeout/error on %s %s (%s); running 1s Phase-2 ccld check", req.side.value, req.symbol, net_err)
        await asyncio.sleep(1.0)
        try:
            records = await asyncio.to_thread(client.inquire_daily_ccld, None, req.symbol, "00")
        except Exception as ccld_err:
            logger.error("Phase-2 inquire_daily_ccld failed: %s", ccld_err)
            records = []

        matched = _find_ccld_match(records, req.symbol, req.quantity, req.side)
        if matched:
            odno = str(matched.get("odno") or matched.get("ODNO") or "").strip()
            filled_qty = int(float(matched.get("tot_ccld_qty") or matched.get("ccld_qty") or matched.get("CCLD_QTY") or 0))
            filled_p = float(matched.get("avg_prvs") or matched.get("ccld_unpr") or matched.get("CCLD_UNPR") or 0.0)
            return {
                "rt_cd": "0",
                "msg1": "PHASE2_VERIFIED_ON_EXCHANGE",
                "output": {"ODNO": odno},
                "_phase2_filled_qty": filled_qty,
                "_phase2_filled_price": filled_p,
            }
        return {"rt_cd": "1", "msg1": f"TIMEOUT_REJECTED: {net_err}"}


# compute_sell_net_pnl is imported and re-exported from v4.adapters._broker_common above.


def parse_ccld_pending_events(
    records: List[Dict[str, Any]], now_dt: Any
) -> List[Any]:
    """Parses `inquire_daily_ccld(ccld_dvsn='02')` rows into `OrderEvent`s."""
    from v4.models import OrderEvent, OrderSide, OrderStatus

    events: List[OrderEvent] = []
    for rec in records or []:
        odno = str(rec.get("odno") or rec.get("ODNO") or "").strip()
        sym = str(rec.get("pdno") or rec.get("PDNO") or "").strip()
        rem = int(float(rec.get("rmn_qty") or rec.get("psbl_qty") or 0))
        if not odno or not sym or rem <= 0:
            continue
        side = OrderSide.BUY if str(rec.get("sll_buy_dvsn_cd") or "02") == "02" else OrderSide.SELL
        events.append(
            OrderEvent(
                order_id=odno,
                symbol=sym,
                side=side,
                status=OrderStatus.PENDING,
                unfilled_quantity=rem,
                requested_quantity=int(float(rec.get("ord_qty") or rem)),
                requested_price=float(rec.get("ord_unpr") or 0.0),
                dt=now_dt,
                message="REST_SYNC",
            )
        )
    return events


def extract_ccld_fill(records: List[Dict[str, Any]], order_id: str) -> Optional[Tuple[int, float]]:
    """Extracts `(filled_qty, filled_price)` for `order_id` from `inquire_daily_ccld` rows."""
    for rec in records or []:
        if str(rec.get("odno") or rec.get("ODNO") or "").strip() == order_id:
            fq = int(float(rec.get("tot_ccld_qty") or rec.get("ccld_qty") or 0))
            fp = float(rec.get("avg_prvs") or rec.get("ccld_unpr") or 0.0)
            if fq > 0 and fp > 0.0:
                return fq, fp
    return None
