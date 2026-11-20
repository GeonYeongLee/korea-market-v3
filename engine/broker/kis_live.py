"""
Korea Investment & Securities (KIS) Live & Mock Trading Broker Adapter.
=======================================================================
Strictly implements Korea Market v3 IBroker and integrates with slotted dataclass models.
Handles real-money and mock order lifecycle, KRX call auction routing, orderable cash
precedence, partial fills, real-time WebSocket execution notices, and account balance syncing.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Callable, Any

from engine.models import (
    Order, OrderEvent, OrderSide, OrderType, OrderStatus,
    Position, AccountBalance, MinuteBar, TickData
)
from engine.broker.base import IBroker
from core.rest_client import KISRestClient
from core.krx_ticks import round_to_krx_tick
from core.telemetry import telemetry
from config.settings import Settings, settings

logger = logging.getLogger("korea_market.broker.kis_live")


class KisLiveBroker(IBroker):
    """Production broker adapter communicating directly with KIS Open API."""

    def __init__(
        self,
        mode: str = "vts",
        cfg: Optional[Settings] = None,
        client: Optional[KISRestClient] = None,
    ):
        self.mode = mode.lower()
        self.cfg = cfg or settings.switch_env("real" if self.mode == "real" else "vts")
        self.client = client or KISRestClient(self.cfg)

        self._lock = threading.RLock()
        self._event_listeners: List[Callable[[OrderEvent], None]] = []
        self._positions: Dict[str, Position] = {}
        self._pending_orders: Dict[str, Order] = {}
        self._latest_balance: Optional[AccountBalance] = None
        self._initial_capital: float = 10_000_000.0

    # -------------------------------------------------------------------------
    # Event Listeners
    # -------------------------------------------------------------------------
    def register_event_listener(self, listener: Callable[[OrderEvent], None]) -> None:
        """Registers a callback listener for OrderEvent broadcasts."""
        with self._lock:
            self._event_listeners.append(listener)

    def _emit_event(self, event: OrderEvent) -> None:
        """Emits an order event to all registered listeners outside the lock."""
        with self._lock:
            listeners = list(self._event_listeners)

        for listener in listeners:
            try:
                listener(event)
            except Exception as e:
                logger.error(f"[{self.mode.upper()}] Error in live broker event listener: {e}", exc_info=True)

    # -------------------------------------------------------------------------
    # Order Submission & Cancellation
    # -------------------------------------------------------------------------
    def submit_order(self, order: Order) -> str:
        """
        Sends order to KIS Open API.
        Enforces KRX legal tick rounding and routes liquidation/SL orders as Market Order ("01").
        Forces KRX exchange routing during call auctions (15:15~15:30 closing auction, 08:50 opening auction).
        """
        try:
            # Emergency tags route as Market Order ("01") on KRX
            # Nextrade pre-market (08:00~08:50) strictly requires Limit Order ("00"), so exempt NXT
            is_nxt_limit = "NXT" in (order.tag or "").upper() and order.price is not None and order.price > 0
            is_market_tag = any(
                k in (order.tag or "").upper()
                for k in ["SL", "EOD", "AUCTION", "STOP", "TP_CLOSE", "EXIT", "LIQUIDATION", "TIMEOUT"]
            ) and not is_nxt_limit

            is_market = (
                order.order_type == OrderType.MARKET
                or order.price is None
                or order.price <= 0
                or (order.side == OrderSide.SELL and is_market_tag)
            )

            if order.side == OrderSide.BUY and "CLOSE" in (order.tag or "").upper() and not is_market and order.price:
                # In KRX 15:20~15:30 closing auction, submit at collar cap (+0.5%) to guarantee fill at single clearing price
                collar_raw = order.price * 1.005
                order_p = round_to_krx_tick(collar_raw, side="BUY", round_mode="floor")
            else:
                order_p = 0 if is_market else round_to_krx_tick(order.price or 0.0, side=order.side.name)

            order_type_str = "01" if is_market else ("11" if "IOC" in (order.tag or "").upper() else "00")

            now_t = datetime.now().time()
            force_krx = (dtime(15, 15) <= now_t <= dtime(15, 30)) or any(
                k in (order.tag or "").upper() for k in ["AUCTION", "CLOSE"]
            )
            target_exchange = "KRX" if force_krx else None

            kwargs = {}
            if target_exchange is not None:
                kwargs["exchange"] = target_exchange

            if order.side == OrderSide.BUY:
                resp = self.client.order_cash_buy(
                    symbol=order.symbol,
                    qty=int(order.quantity),
                    price=order_p,
                    order_type=order_type_str,
                    **kwargs,
                )
            else:
                resp = self.client.order_cash_sell(
                    symbol=order.symbol,
                    qty=int(order.quantity),
                    price=order_p,
                    order_type=order_type_str,
                    **kwargs,
                )

            rt_cd = resp.get("rt_cd", "")
            if rt_cd == "0":
                output = resp.get("output", {})
                odno = str(output.get("ODNO", ""))
                order.order_id = odno or order.order_id
                order.status = OrderStatus.PENDING

                with self._lock:
                    self._pending_orders[order.order_id] = order

                logger.info(
                    f"[{self.mode.upper()}] Order submitted successfully: {order.side.value} {order.quantity} {order.symbol} "
                    f"(ODNO: {odno}, Price: {order_p}, Type: {order_type_str})"
                )
                telemetry.record_execution(
                    event_type="ORDER_SUBMITTED",
                    symbol=order.symbol,
                    side=order.side.value,
                    quantity=int(order.quantity),
                    price=float(order_p),
                    order_no=odno or order.order_id,
                    tag=order.tag or "",
                )
                self._emit_event(OrderEvent(
                    order_id=order.order_id,
                    symbol=order.symbol,
                    side=order.side,
                    status=OrderStatus.PENDING,
                    filled_price=0.0,
                    filled_quantity=0,
                    dt=datetime.now(),
                    message=f"Order submitted (ODNO: {odno})",
                ))
                return order.order_id
            else:
                msg = resp.get("msg1", "Order failed")
                logger.error(f"[{self.mode.upper()}] Order rejected: {msg}")
                order.status = OrderStatus.REJECTED
                telemetry.record_execution(
                    event_type="ORDER_REJECTED",
                    symbol=order.symbol,
                    side=order.side.value,
                    quantity=int(order.quantity),
                    price=float(order_p),
                    order_no=order.order_id,
                    tag=msg,
                )
                self._emit_event(OrderEvent(
                    order_id=order.order_id,
                    symbol=order.symbol,
                    side=order.side,
                    status=OrderStatus.REJECTED,
                    filled_price=0.0,
                    filled_quantity=0,
                    dt=datetime.now(),
                    message=msg,
                ))
                return ""
        except Exception as e:
            logger.error(f"[{self.mode.upper()}] Failed to submit order to KIS: {e}", exc_info=True)
            order.status = OrderStatus.REJECTED
            self._emit_event(OrderEvent(
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                status=OrderStatus.REJECTED,
                filled_price=0.0,
                filled_quantity=0,
                dt=datetime.now(),
                message=str(e),
            ))
            return ""

    def cancel_order(self, order_id: str) -> bool:
        """Cancels open order via KIS Open API."""
        try:
            with self._lock:
                ord_ = self._pending_orders.get(order_id)
                symbol = ord_.symbol if ord_ else ""
                side = ord_.side if ord_ else OrderSide.BUY

            resp = self.client.order_cancel(order_no=order_id, qty=0, cancel_all=True)
            is_success = resp.get("rt_cd") == "0"

            with self._lock:
                self._pending_orders.pop(order_id, None)

            if is_success:
                self.sync_account_state()

            telemetry.record_execution(
                event_type="ORDER_CANCELLED",
                symbol=symbol,
                side=side.value,
                quantity=0,
                order_no=order_id,
                tag="CANCELLED" if is_success else "CANCEL_REJECTED",
            )

            self._emit_event(OrderEvent(
                order_id=order_id,
                symbol=symbol,
                side=side,
                status=OrderStatus.CANCELLED,
                filled_price=0.0,
                filled_quantity=0,
                dt=datetime.now(),
                message="CANCELLED" if is_success else "CANCEL_REJECTED",
            ))
            return is_success
        except Exception as e:
            logger.warning(f"[{self.mode.upper()}] Could not cancel order {order_id}: {e}")
            with self._lock:
                ord_ = self._pending_orders.pop(order_id, None)
                symbol = ord_.symbol if ord_ else ""
                side = ord_.side if ord_ else OrderSide.BUY

            self._emit_event(OrderEvent(
                order_id=order_id,
                symbol=symbol,
                side=side,
                status=OrderStatus.CANCELLED,
                filled_price=0.0,
                filled_quantity=0,
                dt=datetime.now(),
                message=f"CANCEL_EXCEPTION: {e}",
            ))
            return False

    def cancel_pending_orders(self, symbol: Optional[str] = None) -> List[str]:
        """Cancels all active pending orders, optionally filtered by symbol."""
        with self._lock:
            pending_list = list(self._pending_orders.items())

        cancelled_ids: List[str] = []
        for pend_id, pend_ord in pending_list:
            if symbol is None or pend_ord.symbol == symbol:
                logger.info(
                    f"[{self.mode.upper()}] Cancelling pending order {pend_id} "
                    f"({pend_ord.side.value if hasattr(pend_ord.side, 'value') else pend_ord.side} "
                    f"{pend_ord.quantity} {pend_ord.symbol})"
                )
                if self.cancel_order(pend_id):
                    cancelled_ids.append(pend_id)
        return cancelled_ids

    # -------------------------------------------------------------------------
    # Position & Balance Queries
    # -------------------------------------------------------------------------
    def get_position(self, symbol: str) -> Optional[Position]:
        """Returns active Position or None."""
        with self._lock:
            return self._positions.get(symbol)

    def get_positions(self) -> Dict[str, Position]:
        """Returns shallow copy of current positions dictionary."""
        with self._lock:
            return dict(self._positions)

    @property
    def positions(self) -> Dict[str, Position]:
        """Property returning shallow copy of current positions dictionary."""
        return self.get_positions()

    def get_balance(self) -> AccountBalance:
        """Returns current AccountBalance snapshot."""
        with self._lock:
            if self._latest_balance is not None:
                return self._latest_balance
            return AccountBalance(
                total_equity=self._initial_capital,
                cash=self._initial_capital,
                positions=dict(self._positions),
            )

    # -------------------------------------------------------------------------
    # Real-time Price Feed Updates
    # -------------------------------------------------------------------------
    def on_bar(self, bar: MinuteBar) -> None:
        """Updates position current_price, high_water_mark, and low_water_mark based on MinuteBar."""
        with self._lock:
            pos = self._positions.get(bar.symbol)
            if pos is not None:
                pos.current_price = bar.close
                if bar.high > pos.high_water_mark:
                    pos.high_water_mark = bar.high
                if pos.low_water_mark == 0.0 or bar.low < pos.low_water_mark:
                    pos.low_water_mark = bar.low

    def on_tick(self, tick: TickData) -> None:
        """Updates position current_price, high_water_mark, and low_water_mark based on TickData."""
        with self._lock:
            pos = self._positions.get(tick.symbol)
            if pos is not None:
                pos.current_price = tick.price
                if tick.price > pos.high_water_mark:
                    pos.high_water_mark = tick.price
                if pos.low_water_mark == 0.0 or tick.price < pos.low_water_mark:
                    pos.low_water_mark = tick.price

    # -------------------------------------------------------------------------
    # Real-time WebSocket Execution Notice Handling
    # -------------------------------------------------------------------------
    def handle_execution_notice(self, notice: Dict[str, Any]) -> None:
        """
        Processes real-time execution notice (H0STCNI0 / H0STCNI9) pushed by KIS WebSocket.
        Updates internal _positions, decrements/increments cash, and emits OrderEvent.
        Strictly ignores non-fill updates (cntg_yn != '2' or qty <= 0).
        """
        order_no = str(notice.get("order_no") or notice.get("ord_no") or notice.get("ODNO") or "")
        symbol = str(notice.get("symbol") or notice.get("pdno") or notice.get("code") or "")
        side_str = str(
            notice.get("side") or ("BUY" if str(notice.get("sll_buy_dvsn_cd") or "").strip() in ("02", "2") else "SELL")
        ).upper()
        side = OrderSide.BUY if side_str == "BUY" else OrderSide.SELL
        filled_qty = int(notice.get("qty") or notice.get("cntg_qty") or 0)
        filled_p = float(notice.get("price") or notice.get("cntg_unpr") or 0.0)

        cntg_yn = str(notice.get("cntg_yn", "2")).strip()
        if cntg_yn != "2" or filled_qty <= 0:
            logger.info(
                f"[{self.mode.upper()}] Non-fill order notice ignored: order_no={order_no}, "
                f"cntg_yn={cntg_yn}, qty={filled_qty}"
            )
            return

        with self._lock:
            pend_order = self._pending_orders.get(order_no)
            is_partial = False
            remaining_qty = 0

            if pend_order is not None:
                if getattr(pend_order, "unfilled_quantity", None) is None:
                    pend_order.unfilled_quantity = pend_order.quantity
                pend_order.unfilled_quantity = max(0, pend_order.unfilled_quantity - filled_qty)
                remaining_qty = pend_order.unfilled_quantity
                if remaining_qty > 0:
                    is_partial = True
                    pend_order.status = OrderStatus.PARTIALLY_FILLED
                else:
                    self._pending_orders.pop(order_no, None)
                    pend_order.status = OrderStatus.FILLED
            else:
                self._pending_orders.pop(order_no, None)

            # Update position holdings
            if side == OrderSide.BUY:
                if symbol not in self._positions:
                    self._positions[symbol] = Position(
                        symbol=symbol,
                        quantity=filled_qty,
                        avg_price=filled_p,
                        current_price=filled_p,
                        high_water_mark=filled_p,
                        low_water_mark=filled_p,
                        entry_dt=datetime.now(),
                    )
                else:
                    pos = self._positions[symbol]
                    total_cost = (pos.quantity * pos.avg_price) + (filled_qty * filled_p)
                    pos.quantity += filled_qty
                    pos.avg_price = total_cost / pos.quantity if pos.quantity > 0 else filled_p
                    pos.current_price = filled_p
                    if filled_p > pos.high_water_mark:
                        pos.high_water_mark = filled_p
                    if pos.low_water_mark > 0.0:
                        pos.low_water_mark = min(pos.low_water_mark, filled_p)
                    else:
                        pos.low_water_mark = filled_p
            elif side == OrderSide.SELL and symbol in self._positions:
                pos = self._positions[symbol]
                pos.quantity = max(0, pos.quantity - filled_qty)
                if pos.quantity == 0:
                    self._positions.pop(symbol, None)

            # Update cash balance with statutory fees (0.014% comm, 0.18% sell tax)
            current_cash = self._latest_balance.cash if self._latest_balance is not None else self._initial_capital
            total_equity = self._latest_balance.total_equity if self._latest_balance is not None else self._initial_capital
            trade_amt = filled_p * filled_qty
            comm = trade_amt * 0.00014
            tax = trade_amt * 0.0018 if side == OrderSide.SELL else 0.0

            if side == OrderSide.BUY:
                new_cash = max(0.0, current_cash - (trade_amt + comm))
            else:
                new_cash = current_cash + (trade_amt - comm - tax)

            self._latest_balance = AccountBalance(
                total_equity=total_equity,
                cash=new_cash,
                positions=dict(self._positions),
            )

        status = OrderStatus.PARTIALLY_FILLED if is_partial else OrderStatus.FILLED
        logger.info(
            f"[{self.mode.upper()}] Execution notice processed ({status.value}): {side.value} {symbol} "
            f"{filled_qty} shares @ {filled_p:,.0f} KRW (ODNO: {order_no}, Remaining: {remaining_qty})"
        )

        target_p = pend_order.price if (pend_order and pend_order.price) else filled_p
        slip_krw = 0.0
        slip_bp = 0.0
        if target_p > 0:
            slip_krw = (filled_p - target_p) if side == OrderSide.BUY else (target_p - filled_p)
            slip_bp = (slip_krw / target_p) * 10000.0

        telemetry.record_execution(
            event_type=f"ORDER_{status.value}",
            symbol=symbol,
            side=side.value,
            quantity=filled_qty,
            price=target_p,
            order_no=order_no,
            filled_price=filled_p,
            filled_quantity=filled_qty,
            slippage_krw=slip_krw,
            slippage_bp=slip_bp,
            tag=status.value,
        )

        self._emit_event(OrderEvent(
            order_id=order_no,
            symbol=symbol,
            side=side,
            status=status,
            filled_price=filled_p,
            filled_quantity=filled_qty,
            dt=datetime.now(),
            message=f"Executed {side.value} {symbol} {filled_qty} @ {filled_p:,.0f} ({status.value})",
        ))

    # -------------------------------------------------------------------------
    # Account State Reconciliation (REST sync)
    # -------------------------------------------------------------------------
    def sync_account_state(self) -> None:
        """
        Queries client.get_balance(), merges holdings into v3 Position instances, and updates balance.
        Strictly prioritizes orderable cash (ord_psbl_cash_amt / ord_psbl_cash) over D+0 settlement deposit.
        Preserves strategy metadata and stop-loss levels on existing positions.
        """
        try:
            resp = self.client.get_balance()
            if not resp:
                logger.warning(f"[{self.mode.upper()}] Could not fetch KIS balance: empty response")
                return

            out1 = resp.get("holdings", [])
            out2 = resp.get("summary", {})

            # Prefer immediately orderable cash over D+0 settlement deposit
            raw_cash = out2.get("ord_psbl_cash_amt")
            if raw_cash is None or str(raw_cash).strip() == "":
                raw_cash = out2.get("ord_psbl_cash")
            if raw_cash is None or str(raw_cash).strip() == "":
                raw_cash = out2.get("dnca_tot_amt", 0.0)

            cash = float(raw_cash)
            tot_equity = float(out2.get("tot_evlu_amt", cash))

            with self._lock:
                current_symbols = set()
                for item in out1:
                    qty = int(item.get("hldg_qty") or item.get("hld_qty") or 0)
                    if qty > 0:
                        code = str(item.get("pdno", ""))
                        if not code:
                            continue
                        current_symbols.add(code)
                        avg_p = float(item.get("pchs_avg_pric", 0.0))
                        curr_p = float(item.get("prpr", avg_p))

                        if code in self._positions:
                            pos = self._positions[code]
                            pos.quantity = qty
                            pos.avg_price = avg_p
                            pos.current_price = curr_p
                            pos.high_water_mark = max(pos.high_water_mark, curr_p)
                            if pos.low_water_mark > 0.0:
                                pos.low_water_mark = min(pos.low_water_mark, curr_p)
                            else:
                                pos.low_water_mark = curr_p
                        else:
                            pos = Position(
                                symbol=code,
                                quantity=qty,
                                avg_price=avg_p,
                                current_price=curr_p,
                                high_water_mark=curr_p,
                                low_water_mark=curr_p,
                                entry_dt=datetime.now(),
                            )
                            self._positions[code] = pos

                # Remove symbols that are no longer in remote holdings
                for code in list(self._positions.keys()):
                    if code not in current_symbols:
                        del self._positions[code]

                curr_capital = tot_equity if tot_equity > 0 else cash
                self._initial_capital = curr_capital
                self._latest_balance = AccountBalance(
                    total_equity=tot_equity,
                    cash=cash,
                    positions=dict(self._positions),
                )
        except Exception as e:
            logger.error(f"[{self.mode.upper()}] Error syncing account state: {e}", exc_info=True)
