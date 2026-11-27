"""
Structured Audit Trail Sinks for Korea Market v4 (Live & Backtest).
Provides `NullAuditSink` (zero-overhead no-op) and `JsonlAuditSink` (compact JSONL).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, IO, Optional

from v4.interfaces import IAuditSink

logger = logging.getLogger("korea_market.v4.audit_sink")


class NullAuditSink(IAuditSink):
    """Zero-overhead no-op audit sink when audit logging is disabled."""

    def emit(self, event_type: str, dt: datetime, symbol: str = "", **data: Any) -> None:
        return None

    def close(self) -> None:
        return None


class JsonlAuditSink(IAuditSink):
    """Writes one compact JSON line per audit event to a UTF-8 `.jsonl` file."""

    def __init__(self, file_path: Path) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._fp: Optional[IO[str]] = self.file_path.open("a", encoding="utf-8", buffering=1)
        self._error_logged: bool = False

    def _warn_once(self, action: str, exc: Exception) -> None:
        if not self._error_logged:
            self._error_logged = True
            logger.warning("Audit sink %s failed (%s): %s", action, self.file_path, exc)

    def emit(self, event_type: str, dt: datetime, symbol: str = "", **data: Any) -> None:
        if self._fp is None:
            return
        try:
            payload = {"event": event_type, "dt": dt.isoformat(), "symbol": symbol, **data}
            self._fp.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
        except Exception as exc:
            self._warn_once("write", exc)

    def close(self) -> None:
        fp = self._fp
        if fp is None:
            return
        try:
            fp.flush()
        except Exception as exc:
            self._warn_once("flush", exc)
        finally:
            try:
                fp.close()
            except Exception as exc:
                self._warn_once("close", exc)
            finally:
                self._fp = None
