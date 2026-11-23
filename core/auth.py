"""
KIS Open API Authentication & Token Management
Handles OAuth 2.0 Access Token & WebSocket Approval Key with atomic file caching.
"""
from __future__ import annotations

import json
import logging
import hashlib
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any
import requests

from config.settings import Settings, settings

logger = logging.getLogger("kis.auth")


class TokenManager:
    """
    Manages OAuth2 access tokens and WebSocket approval keys for KIS Developers.
    Caches tokens to file to avoid exhausting the daily limit (typically max 1-3 requests/day).
    Includes atomic writes and in-process lock synchronization to prevent file corruption.
    """

    def __init__(self, cfg: Optional[Settings] = None):
        self.cfg = cfg or settings
        self.cache_dir = self.cfg.token_cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cached_token: Optional[str] = None
        self._cached_token_expires_at: Optional[datetime] = None
        self._cached_approval_key: Optional[str] = None
        self._lock = threading.Lock()

    def _get_key_hash(self) -> str:
        raw = f"{self.cfg.env}_{self.cfg.app_key}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    @property
    def _token_cache_file(self) -> Path:
        return self.cache_dir / f"token_{self.cfg.env}_{self._get_key_hash()}.json"

    def _atomic_write_json(self, file_path: Path, data: Dict[str, Any]) -> None:
        """Atomically writes JSON payload using temporary file replacement."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temp_file = file_path.with_suffix(f".tmp.{os.getpid()}_{time.time_ns()}")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            temp_file.replace(file_path)
        except Exception as e:
            logger.warning(f"Failed to persist cache atomically to {file_path}: {e}")
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass

    def get_access_token(self, force_refresh: bool = False) -> str:
        """
        Retrieves a valid OAuth2 Bearer Access Token.
        Uses cached token if available and not within 60 minutes of expiry.
        """
        with self._lock:
            if not force_refresh:
                cached = self._load_cached_token()
                if cached:
                    return cached

            if not self.cfg.app_key or not self.cfg.app_secret:
                raise ValueError(
                    f"[{self.cfg.env.upper()}] KIS AppKey or AppSecret is missing. "
                    f"Please verify your .env file has valid KIS_{self.cfg.env.upper()}_APP_KEY and SECRET."
                )

            logger.info(f"[{self.cfg.env.upper()}] Requesting new KIS OAuth2 access token...")
            url = f"{self.cfg.endpoints.rest_url}/oauth2/tokenP"
            headers = {"content-type": "application/json"}
            payload = {
                "grant_type": "client_credentials",
                "appkey": self.cfg.app_key,
                "appsecret": self.cfg.app_secret,
            }

            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=10)
                if resp.status_code != 200:
                    err_desc = ""
                    try:
                        err_json = resp.json()
                        err_desc = err_json.get("error_description") or err_json.get("msg1") or ""
                    except Exception:
                        pass
                    msg = f"HTTP {resp.status_code}" + (f" ({err_desc})" if err_desc else "")
                    raise RuntimeError(f"KIS Token acquisition failed: {msg}")
                data = resp.json()
            except requests.exceptions.RequestException as e:
                logger.error(f"Failed to obtain KIS access token: {e}")
                raise RuntimeError(f"KIS Token acquisition failed: {e}") from e

            access_token = data.get("access_token")
            if not access_token:
                raise RuntimeError(f"KIS Token response did not contain access_token: {data}")

            expires_in = data.get("expires_in", 86400)
            
            # Parse expiration string if provided, else compute from expires_in
            expired_str = data.get("access_token_token_expired")
            if expired_str:
                try:
                    expires_at = datetime.strptime(expired_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    expires_at = datetime.now() + timedelta(seconds=int(expires_in))
            else:
                expires_at = datetime.now() + timedelta(seconds=int(expires_in))

            self._save_token(access_token, expires_at)
            logger.info(f"[{self.cfg.env.upper()}] Successfully issued new token, expires at {expires_at}")
            return access_token

    def _load_cached_token(self) -> Optional[str]:
        cache_path = self._token_cache_file
        if not cache_path.exists():
            return None

        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            expires_at_str = data.get("expires_at")
            if not expires_at_str:
                return None

            expires_at = datetime.fromisoformat(expires_at_str)
            # Renew if remaining time is less than 60 minutes
            if datetime.now() + timedelta(minutes=60) >= expires_at:
                logger.info("Cached token is near expiration (under 60 min), refreshing.")
                return None

            self._cached_token = data.get("access_token")
            self._cached_token_expires_at = expires_at
            return self._cached_token
        except Exception as e:
            logger.warning(f"Error reading token cache: {e}")
            return None

    def _save_token(self, token: str, expires_at: datetime) -> None:
        self._cached_token = token
        self._cached_token_expires_at = expires_at
        data = {
            "access_token": token,
            "expires_at": expires_at.isoformat(),
            "env": self.cfg.env,
            "saved_at": datetime.now().isoformat(),
        }
        self._atomic_write_json(self._token_cache_file, data)

    def get_approval_key(self, force_refresh: bool = False) -> str:
        """
        Retrieves WebSocket connection approval key (/oauth2/Approval).
        Strictly session-scoped and held in-memory (never written to disk)
        to prevent stale/revoked key reuse across processes or runs.
        """
        with self._lock:
            if not force_refresh and self._cached_approval_key:
                return self._cached_approval_key

            if not self.cfg.app_key or not self.cfg.app_secret:
                raise ValueError(
                    f"[{self.cfg.env.upper()}] KIS AppKey or AppSecret is missing for WebSocket Approval."
                )

            logger.info(f"[{self.cfg.env.upper()}] Requesting fresh in-memory WebSocket approval key...")
            url = f"{self.cfg.endpoints.rest_url}/oauth2/Approval"
            headers = {"content-type": "application/json"}
            payload = {
                "grant_type": "client_credentials",
                "appkey": self.cfg.app_key,
                "secretkey": self.cfg.app_secret,
            }

            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=10)
                if resp.status_code != 200:
                    err_desc = ""
                    try:
                        err_json = resp.json()
                        err_desc = err_json.get("error_description") or err_json.get("msg1") or ""
                    except Exception:
                        pass
                    msg = f"HTTP {resp.status_code}" + (f" ({err_desc})" if err_desc else "")
                    raise RuntimeError(f"WebSocket Approval failed: {msg}")
                data = resp.json()
            except requests.exceptions.RequestException as e:
                logger.error(f"Failed to get WebSocket approval key: {e}")
                raise RuntimeError(f"WebSocket Approval failed: {e}") from e

            approval_key = data.get("approval_key")
            if not approval_key:
                raise RuntimeError(f"WebSocket Approval response did not contain approval_key: {data}")

            self._cached_approval_key = approval_key
            logger.info(f"[{self.cfg.env.upper()}] In-memory WebSocket approval key acquired successfully.")
            return approval_key

    def invalidate_approval_key(self) -> None:
        """Invalidates in-memory WebSocket approval key so the next connection attempt fetches a fresh key."""
        with self._lock:
            self._cached_approval_key = None
            logger.info(f"[{self.cfg.env.upper()}] In-memory WebSocket approval key invalidated.")
