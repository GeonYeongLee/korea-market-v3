"""
KIS REAL-Live Broker Adapter for Korea Market v4.
Implements `IBrokerPort` with `TTTC*` REST orders, Phase-2 `1s` ccld recovery,
>= 3 consecutive BUY rejection lockout, WS execution notice handling with `net_pnl`,
balance sync (`TTTC8434R`), and `sync_open_orders()`.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from config.settings import Settings, settings
from core.rest_client import KISRestClient
from v4.adapters._broker_common import emit_order_event, make_reject_event, update_position_mtm
from v4.adapters.kis.broker_orders import (
    extract_ccld_fill,
    parse_ccld_pending_events,
    resolve_order_price,
    submit_rest_order,
)
from v4.adapters.kis.broker_state import apply_fill_state, build_fill_event, reconcile_balance_state
from v4.adapters.kis.clock import KisLiveClock
from v4.adapters.kis.venue import KisVenueResolver
from v4.interfaces import ExecutionCallback, IBrokerPort, IClock
from v4.models import (
    AccountBalance,
    ExecutionMode,
    MinuteBar,
    OrderEvent,
    OrderRequest,
    OrderSide,
    OrderStatus,
    Position,
    TickEvent,
    TradeRecord,
)

logger = logging.getLogger("korea_market.v4.adapters.kis.broker")
MAX_CONSECUTIVE_BUY_REJECTIONS = 3


class KisLiveBrokerAdapter(IBrokerPort):
    """Canonical `IBrokerPort` implementation for KIS REAL-only accounts."""

    def __init__(
        self,
        cfg: Optional[Settings] = None,
        client: Optional[KISRestClient] = None,
        clock: Optional[IClock] = None,
        initial_capital: float = 10_000_000.0,
        commission_rate: float = 0.00015,
        tax_rate: float = 0.0018,
        venue: Optional[KisVenueResolver] = None,
    ) -> None:
        self.cfg = cfg or settings.switch_env("real")
        self.client = client or KISRestClient(self.cfg)
        self.clock = clock or KisLiveClock()
        self.venue = venue or KisVenueResolver()
        self.initial_capital = float(initial_capital)
        self.commission_rate = float(commission_rate)
        self.tax_rate = float(tax_rate)

        self._lock = threading.RLock()
        self._callbacks: List[ExecutionCallback] = []
        self._positions: Dict[str, Position] = {}
        self._pending_orders: Dict[str, OrderEvent] = {}
        self._order_cum_filled: Dict[str, int] = {}
        self._order_req_qty: Dict[str, int] = {}
        self._order_symbols: Dict[str, str] = {}
        self._order_cum_pnl: Dict[str, float] = {}
        self._order_cum_comm: Dict[str, float] = {}
        self._order_cum_tax: Dict[str, float] = {}
        self._completed_trades: List[TradeRecord] = []
        self._latest_balance: Optional[AccountBalance] = None
        self._consecutive_buy_rejections: int = 0
        self._order_seq: int = 1
        self._trade_seq: int = 1

    def on_execution_event(self, cb: ExecutionCallback) -> None: self._callbacks.append(cb)
    def set_execution_mode(self, mode: ExecutionMode) -> None: return None

    def _emit_event(self, ev: OrderEvent) -> None:
        emit_order_event(self._callbacks, ev, logger)

    def get_balance(self) -> AccountBalance:
        with self._lock:
            if self._latest_balance is not None: return self._latest_balance
            pos_val = sum(p.current_price * p.quantity for p in self._positions.values())
            return AccountBalance(
                total_equity=self.initial_capital + pos_val, cash=self.initial_capital,
                positions=dict(self._positions), initial_capital=self.initial_capital,
            )

    def get_positions(self) -> Dict[str, Position]:
        with self._lock: return dict(self._positions)

    def get_position(self, symbol: str) -> Optional[Position]:
        with self._lock: return self._positions.get(symbol)

    def get_pending_orders(self) -> List[OrderEvent]:
        with self._lock: return list(self._pending_orders.values())

    def get_completed_trades(self) -> List[TradeRecord]:
        with self._lock: return list(self._completed_trades)

    def _update_mtm(self, symbol: str, cur_p: float, hi_p: float, lo_p: float) -> None:
        with self._lock:
            update_position_mtm(self._positions, symbol, cur_p, high=hi_p, low=lo_p)

    def on_bar(self, bar: MinuteBar) -> None: self._update_mtm(bar.symbol, bar.close, bar.high, bar.low)
    def on_tick(self, tick: TickEvent) -> None: self._update_mtm(tick.symbol, tick.price, tick.price, tick.price)

    def _record_fill_delta(
        self, order_id: str, symbol: str, delta_qty: int, price: float, side: OrderSide, dt: datetime
    ) -> tuple[int, float, float, float]:
        """Applies `delta_qty` fill under `self._lock`; returns `(cum_qty, cum_comm, cum_tax, cum_pnl)`."""
        self._trade_seq, self._latest_balance, comm, tax, net_pnl = apply_fill_state(
            self._positions, self._completed_trades, self._trade_seq,
            self._latest_balance, self.initial_capital, self.commission_rate,
            self.tax_rate, symbol, delta_qty, price, side, dt,
        )
        cum_qty = self._order_cum_filled.get(order_id, 0) + delta_qty
        self._order_cum_filled[order_id] = cum_qty
        self._order_cum_comm[order_id] = self._order_cum_comm.get(order_id, 0.0) + comm
        self._order_cum_tax[order_id] = self._order_cum_tax.get(order_id, 0.0) + tax
        self._order_cum_pnl[order_id] = self._order_cum_pnl.get(order_id, 0.0) + net_pnl
        return cum_qty, self._order_cum_comm[order_id], self._order_cum_tax[order_id], self._order_cum_pnl[order_id]

    def _make_reject(self, oid: str, req: OrderRequest, px: float, dt: datetime, msg: str) -> OrderEvent:
        ev = make_reject_event(req, dt, msg, order_id=oid, requested_price=px)
        self._emit_event(ev)
        return ev

    async def submit_order(self, req: OrderRequest) -> OrderEvent:
        now_dt = req.created_at or self.clock.now()
        oid = req.client_order_id or f"kis_{self._order_seq}"
        self._order_seq += 1

        if req.side == OrderSide.BUY and self._consecutive_buy_rejections >= MAX_CONSECUTIVE_BUY_REJECTIONS:
            return self._make_reject(oid, req, float(req.price or 0.0), now_dt, "BUY_LOCKOUT_CONSECUTIVE_REJECTIONS")

        rounded_price = resolve_order_price(req)
        exchange = req.exchange or self.venue.order_exchange(req.symbol)
        resp = await submit_rest_order(self.client, req, rounded_price, exchange=exchange)

        if str(resp.get("rt_cd", "")) != "0":
            if req.side == OrderSide.BUY:
                self._consecutive_buy_rejections += 1
            return self._make_reject(
                oid, req, float(rounded_price or req.price or 0.0), now_dt, str(resp.get("msg1") or "ORDER_REJECTED")
            )

        if req.side == OrderSide.BUY:
            self._consecutive_buy_rejections = 0

        odno = str((resp.get("output") or {}).get("ODNO") or oid).strip()
        with self._lock:
            self._order_req_qty[odno] = req.quantity
            self._order_symbols[odno] = req.symbol

        p2_qty = int(resp.get("_phase2_filled_qty") or 0)
        p2_px = float(resp.get("_phase2_filled_price") or 0.0)
        if p2_qty > 0 and p2_px > 0.0:
            req_px = float(rounded_price or req.price or 0.0)
            with self._lock:
                prev_cum = self._order_cum_filled.get(odno, 0)
                delta = max(0, p2_qty - prev_cum)
                if delta <= 0:
                    return self._pending_orders.get(odno) or build_fill_event(
                        odno, req.symbol, req.side, OrderStatus.FILLED, p2_px, 0, 0, prev_cum, req_px, req.quantity,
                        self._order_cum_comm.get(odno, 0.0), self._order_cum_tax.get(odno, 0.0), self._order_cum_pnl.get(odno, 0.0),
                        now_dt, "PHASE2_ALREADY_FILLED", req.client_order_id, req.tag,
                    )
                cum_q, cum_c, cum_t, cum_pnl = self._record_fill_delta(odno, req.symbol, delta, p2_px, req.side, now_dt)
                status = OrderStatus.FILLED if cum_q >= req.quantity else OrderStatus.PARTIALLY_FILLED
                ev = build_fill_event(
                    odno, req.symbol, req.side, status, p2_px, delta, max(0, req.quantity - cum_q), cum_q, req_px,
                    req.quantity, cum_c, cum_t, cum_pnl, now_dt, "PHASE2_VERIFIED_FILL", req.client_order_id, req.tag,
                )
                if status == OrderStatus.PARTIALLY_FILLED: self._pending_orders[odno] = ev
            self._emit_event(ev)
            return ev

        ev = OrderEvent(
            order_id=odno, symbol=req.symbol, side=req.side, status=OrderStatus.PENDING,
            unfilled_quantity=req.quantity, requested_price=float(rounded_price or req.price or 0.0),
            requested_quantity=req.quantity, dt=now_dt, submitted_at=now_dt,
            client_order_id=req.client_order_id, message=str(resp.get("msg1") or "PENDING"), tag=req.tag,
        )
        with self._lock: self._pending_orders[odno] = ev
        self._emit_event(ev)
        return ev

    async def cancel_order(self, order_id: str, symbol: str = "") -> bool:
        with self._lock:
            pend = self._pending_orders.get(order_id)
            sym = symbol or (pend.symbol if pend else self._order_symbols.get(order_id, ""))
            side = pend.side if pend else OrderSide.BUY
            req_qty = self._order_req_qty.get(order_id, 0)
            if req_qty > 0 and self._order_cum_filled.get(order_id, 0) >= req_qty:
                self._pending_orders.pop(order_id, None)
                return False
        resp = await asyncio.to_thread(self.client.order_cancel, order_id, "00000", 0, True, symbol=sym)
        ok = str(resp.get("rt_cd", "")) == "0"
        with self._lock: self._pending_orders.pop(order_id, None)
        if not ok and sym:
            records = await asyncio.to_thread(self.client.inquire_daily_ccld, order_id, sym, "00")
            fill = extract_ccld_fill(records, order_id)
            if fill is not None:
                fq, fp = fill
                now_dt = self.clock.now()
                with self._lock:
                    delta = max(0, fq - self._order_cum_filled.get(order_id, 0))
                    if delta <= 0: return False
                    cum_q, cum_c, cum_t, cum_pnl = self._record_fill_delta(order_id, sym, delta, fp, side, now_dt)
                    req_q = self._order_req_qty.get(order_id, cum_q)
                self._emit_event(build_fill_event(
                    order_id, sym, side, OrderStatus.FILLED, fp, delta, 0, cum_q,
                    fp, req_q, cum_c, cum_t, cum_pnl, now_dt, "FILLED_ON_CANCEL_REJECT",
                ))
                return False
        self._emit_event(OrderEvent(
            order_id=order_id, symbol=sym, side=side, status=OrderStatus.CANCELLED,
            dt=self.clock.now(), message="CANCELLED" if ok else "CANCEL_REJECTED",
        ))
        return ok

    async def cancel_pending(self, symbol: Optional[str] = None) -> List[str]:
        with self._lock:
            targets = [(oid, o.symbol) for oid, o in self._pending_orders.items() if symbol is None or o.symbol == symbol]
        cancelled: List[str] = []
        for oid, sym in targets:
            if await self.cancel_order(oid, symbol=sym or self._order_symbols.get(oid, "")):
                cancelled.append(oid)
        return cancelled

    def handle_execution_notice(self, notice: Dict[str, Any]) -> None:
        """Processes WS `H0STCNI0`/`H0STCNI9` execution notice and emits canonical `OrderEvent`."""
        odno = str(notice.get("order_no", "")).strip()
        sym = str(notice.get("symbol", "")).strip()
        side = OrderSide.BUY if str(notice.get("side", "BUY")).upper() == "BUY" else OrderSide.SELL
        qty = int(notice.get("qty", 0))
        px = float(notice.get("price", 0.0))
        if not sym or qty <= 0 or px <= 0.0:
            return

        now_dt = self.clock.now()
        with self._lock:
            if odno:
                self._order_symbols.setdefault(odno, sym)
            req_qty = self._order_req_qty.get(odno, 0)
            prev_cum = self._order_cum_filled.get(odno, 0)
            delta = min(qty, max(0, req_qty - prev_cum)) if req_qty > 0 else qty
            if delta <= 0:
                return
            pend = self._pending_orders.get(odno)
            rem = max(0, (pend.unfilled_quantity if pend is not None else req_qty) - delta) if (pend is not None or req_qty > 0) else 0
            if pend is not None:
                pend.unfilled_quantity = rem
                pend.cum_filled_quantity = prev_cum + delta
                if rem == 0:
                    self._pending_orders.pop(odno, None)
            status = OrderStatus.PARTIALLY_FILLED if rem > 0 else OrderStatus.FILLED
            cum_q, cum_c, cum_t, cum_pnl = self._record_fill_delta(odno, sym, delta, px, side, now_dt)

        self._emit_event(build_fill_event(
            odno, sym, side, status, px, delta, rem, cum_q,
            px, req_qty or cum_q, cum_c, cum_t, cum_pnl, now_dt, f"WS_EXECUTION_{status.value}",
        ))

    async def sync_open_orders(self) -> List[OrderEvent]:
        """Reconciles open orders & holdings against KIS `inquire_daily_ccld` and `get_balance`."""
        bal = await asyncio.to_thread(self.client.get_balance)
        now_dt = self.clock.now()
        with self._lock:
            self._latest_balance = reconcile_balance_state(self._positions, bal, self.initial_capital, now_dt)
        records = await asyncio.to_thread(self.client.inquire_daily_ccld, None, "", "02")
        events = parse_ccld_pending_events(records, now_dt)
        with self._lock:
            self._pending_orders = {ev.order_id: ev for ev in events}
            for ev in events:
                self._order_symbols[ev.order_id] = ev.symbol
                self._order_req_qty.setdefault(ev.order_id, ev.requested_quantity)
        return events
