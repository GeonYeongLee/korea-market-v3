"""
Real-Time Trading Audit & Diagnostic Telemetry Recorder
========================================================
Persists zero-gap audit trails for post-market backtest reconciliation:
  - logs/live_api_YYYYMMDD.jsonl           : Granular REST API and WebSocket message frames
  - logs/live_strategy_eval_YYYYMMDD.jsonl : Scanner, Entry, and Exit condition evaluations
  - logs/live_executions_YYYYMMDD.jsonl    : Order lifecycle, fill events, and tick slippage
  - logs/trades_audit_YYYYMMDD.jsonl       : Lifecycle trading events (backward compatible)
  - logs/scanner_audit_YYYYMMDD.jsonl      : Scanner funnel records (backward compatible)
  - logs/scanner_1min_YYYYMMDD.jsonl       : Minutely candidate snapshots (backward compatible)
  - logs/trading_YYYYMMDD.log              : Human-readable application stream log
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, Optional, List

from config.settings import settings
from core.telemetry_writer import async_writer, mask_sensitive_dict

logger = logging.getLogger("kis.telemetry")


class TelemetryManager:
    """Manages structured JSONL telemetry streams using non-blocking async disk writes."""

    _instance: Optional[TelemetryManager] = None

    def __init__(self, logs_dir: Optional[Path] = None):
        self.logs_dir = logs_dir or settings.logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.today_str = datetime.now().strftime("%Y%m%d")
        self._recompute_file_paths()
        self._logging_configured = False

        is_testing = bool(os.environ.get("PYTEST_CURRENT_TEST")) or os.environ.get("ENV") == "test"
        self.audit_enabled = not is_testing

    def _recompute_file_paths(self) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.app_log_file = self.logs_dir / f"trading_{self.today_str}.log"
        self.live_api_file = self.logs_dir / f"live_api_{self.today_str}.jsonl"
        self.live_strategy_eval_file = self.logs_dir / f"live_strategy_eval_{self.today_str}.jsonl"
        self.live_executions_file = self.logs_dir / f"live_executions_{self.today_str}.jsonl"
        self.scanner_audit_file = self.logs_dir / f"scanner_audit_{self.today_str}.jsonl"
        self.scanner_1min_file = self.logs_dir / f"scanner_1min_{self.today_str}.jsonl"
        self.trades_audit_file = self.logs_dir / f"trades_audit_{self.today_str}.jsonl"

    def set_logs_dir(self, new_logs_dir: Path) -> None:
        self.logs_dir = Path(new_logs_dir)
        self._recompute_file_paths()

    def set_audit_enabled(self, enabled: bool) -> None:
        self.audit_enabled = enabled

    @classmethod
    def get_instance(cls) -> TelemetryManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def setup_file_logging(self, level: int = logging.INFO) -> Path:
        if self._logging_configured:
            return self.app_log_file
        root_logger = logging.getLogger()
        root_logger.setLevel(level)
        if not any(isinstance(h, logging.FileHandler) for h in root_logger.handlers):
            fh = logging.FileHandler(self.app_log_file, encoding="utf-8")
            fh.setLevel(level)
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
            root_logger.addHandler(fh)
        self._logging_configured = True
        return self.app_log_file

    def record_api_call(
        self,
        endpoint: str,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        status_code: int = 200,
        rt_cd: str = "0",
        msg1: str = "",
        latency_ms: float = 0.0,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Records REST API request/response with masked secrets and latency."""
        if not self.audit_enabled:
            return
        rec = {
            "timestamp": datetime.now().isoformat(),
            "channel": "REST",
            "endpoint": endpoint,
            "method": method.upper(),
            "params": mask_sensitive_dict(params),
            "body": mask_sensitive_dict(body),
            "status_code": status_code,
            "rt_cd": str(rt_cd),
            "msg1": msg1,
            "latency_ms": round(latency_ms, 2),
        }
        if extra:
            rec.update(extra)
        async_writer.write(self.live_api_file, rec)

    def record_ws_frame(
        self,
        frame_type: str,
        tr_id: str = "",
        symbol: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Records WebSocket quote tick, execution notice, or control frame."""
        if not self.audit_enabled:
            return
        rec = {
            "timestamp": datetime.now().isoformat(),
            "channel": "WS",
            "frame_type": frame_type,
            "tr_id": tr_id,
            "symbol": symbol,
            "data": data or {},
        }
        async_writer.write(self.live_api_file, rec)

    def record_strategy_eval(
        self,
        eval_type: str,  # 'SCANNER', 'ENTRY', 'EXIT'
        symbol: str,
        decision: str,   # 'PASS', 'REJECT', 'HOLDING', 'TRIGGER'
        reason: str,
        metrics: Dict[str, Any],
        dt: Optional[datetime] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Records granular decision reasoning and numeric metrics for condition evaluations."""
        if not self.audit_enabled:
            return
        rec = {
            "timestamp": (dt or datetime.now()).isoformat(),
            "eval_type": eval_type.upper(),
            "symbol": symbol,
            "decision": decision,
            "reason": reason,
            "metrics": metrics,
        }
        if extra:
            rec.update(extra)
        async_writer.write(self.live_strategy_eval_file, rec)

    def record_execution(
        self,
        event_type: str,  # 'ORDER_SUBMITTED', 'ORDER_FILLED', 'ORDER_CANCELLED', 'ORDER_REJECTED'
        symbol: str,
        side: str,
        quantity: int,
        price: float = 0.0,
        order_no: str = "",
        filled_price: float = 0.0,
        filled_quantity: int = 0,
        slippage_krw: float = 0.0,
        slippage_bp: float = 0.0,
        tag: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Records order submissions, executions, cancellations, and slippage."""
        if not self.audit_enabled:
            return
        rec = {
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "symbol": symbol,
            "order_no": str(order_no),
            "side": side.upper(),
            "quantity": quantity,
            "price": price,
            "filled_price": filled_price,
            "filled_quantity": filled_quantity,
            "slippage_krw": round(slippage_krw, 2),
            "slippage_bp": round(slippage_bp, 2),
            "tag": tag,
        }
        if extra:
            rec.update(extra)
        async_writer.write(self.live_executions_file, rec)
        # Also mirror to trades_audit_file for backward compatibility
        async_writer.write(self.trades_audit_file, rec)

    def record_scanner_evaluation(
        self,
        symbol: str,
        name: str,
        open_gain_pct: float,
        turnover_krw: float,
        day_open: float,
        current_price: float,
        day_low: float,
        mae_pct: float,
        decision: str,
        reason: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Records single-stock evaluation event in scanner funnel."""
        if not self.audit_enabled:
            return
        rec = {
            "timestamp": datetime.now().isoformat(), "symbol": symbol, "name": name,
            "open_gain_pct": round(open_gain_pct, 2), "turnover_krw": round(turnover_krw),
            "turnover_eok": round(turnover_krw / 1e8, 2), "day_open": day_open,
            "current_price": current_price, "day_low": day_low,
            "mae_pct": round(mae_pct, 2) if mae_pct is not None else None,
            "decision": decision, "reason": reason,
        }
        if extra:
            rec.update(extra)
        async_writer.write(self.scanner_audit_file, rec)

        # Mirror into live_strategy_eval for unified reconciliation
        metrics = {
            "name": name, "open_gain_pct": round(open_gain_pct, 2),
            "turnover_krw": round(turnover_krw), "turnover_eok": round(turnover_krw / 1e8, 2),
            "day_open": day_open, "current_price": current_price, "day_low": day_low,
            "mae_pct": round(mae_pct, 2) if mae_pct is not None else None,
        }
        self.record_strategy_eval("SCANNER", symbol, decision, reason, metrics, extra=extra)

    def log_scanner_audit(self, **kwargs) -> None:
        """Alias for record_scanner_evaluation."""
        self.record_scanner_evaluation(**kwargs)

    def record_minutely_scanner_snapshot(
        self,
        minute_str: str,
        candidates: List[Dict[str, Any]],
        active_symbols: List[str],
    ) -> None:
        """Records 1-minute candidate stock selection snapshot."""
        now_iso = datetime.now().isoformat()
        today = date.today().isoformat()
        summary = {
            "record_type": "1MIN_SUMMARY", "timestamp": now_iso, "date": today, "minute": minute_str,
            "total_candidates": len(candidates), "active_symbols": list(active_symbols), "promoted_count": len(active_symbols),
            "rejected_turnover_count": sum(1 for c in candidates if "TURNOVER" in str(c.get("status", ""))),
            "rejected_mae_count": sum(1 for c in candidates if "MAE" in str(c.get("status", ""))),
            "rejected_gain_count": sum(1 for c in candidates if "GAIN" in str(c.get("status", ""))),
        }
        async_writer.write(self.scanner_1min_file, summary)

        for c in candidates:
            cand = {
                "record_type": "CANDIDATE", "timestamp": now_iso, "date": today, "minute": minute_str,
                "rank": c.get("rank"), "symbol": c.get("symbol"), "name": c.get("name"), "pool_origin": c.get("pool_origin", ""),
                "open_gain_pct": round(float(c.get("open_gain_pct", 0.0)), 2), "turnover_krw": round(float(c.get("turnover_krw", 0.0))),
                "turnover_eok": round(float(c.get("turnover_eok", 0.0)), 2), "day_open": float(c.get("day_open", 0.0)),
                "current_price": float(c.get("current_price", 0.0)), "day_low": float(c.get("day_low", 0.0)),
                "mae_pct": round(float(c.get("mae_pct", 0.0)), 2) if c.get("mae_pct") is not None else None,
                "status": c.get("status"), "reason": c.get("reason"),
            }
            async_writer.write(self.scanner_1min_file, cand)

    def record_trade_event(self, event_type: str, symbol: str, data: Dict[str, Any]) -> None:
        """Records lifecycle trading events."""
        if not self.audit_enabled:
            return
        rec = {
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "symbol": symbol,
            **data,
        }
        async_writer.write(self.trades_audit_file, rec)

    def flush(self, timeout: float = 2.0) -> None:
        """Flushes background async write buffer to disk."""
        async_writer.flush(timeout=timeout)


# Global singleton instance
telemetry = TelemetryManager.get_instance()
