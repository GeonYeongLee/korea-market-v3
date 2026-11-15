"""
System Configuration & KIS Open API Endpoint Definitions
Korea Market v3
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Base directory (korea-market_v3 root)
BASE_DIR = Path(__file__).resolve().parent.parent

# Shared repository root and data directory resolution
ROOT_REPO_DIR = BASE_DIR.parent if (BASE_DIR.parent / "data").exists() else BASE_DIR
SHARED_DATA_DIR = ROOT_REPO_DIR / "data"

# Load .env if present (check v3 local first, then korea-market_v2, then root repository if any)
env_file = BASE_DIR / ".env"
v2_env_file = BASE_DIR.parent / "korea-market_v2" / ".env"
root_env_file = ROOT_REPO_DIR / ".env"
if env_file.exists():
    load_dotenv(dotenv_path=env_file)
elif v2_env_file.exists():
    load_dotenv(dotenv_path=v2_env_file)
elif root_env_file.exists():
    load_dotenv(dotenv_path=root_env_file)
else:
    load_dotenv()


class KISEndpoints(BaseModel):
    """Endpoints and TR configuration for VTS (Mock) and REAL (Live) environments."""
    env: Literal["vts", "real"] = "vts"
    rest_url: str = ""
    ws_url: str = ""

    # Order TR IDs
    tr_buy: str = ""
    tr_sell: str = ""
    tr_cancel: str = ""
    tr_balance: str = ""
    tr_ccld: str = ""

    @classmethod
    def create(cls, env: Literal["vts", "real"] = "vts") -> KISEndpoints:
        if env == "real":
            return cls(
                env="real",
                rest_url="https://openapi.koreainvestment.com:9443",
                ws_url="ws://ops.koreainvestment.com:21000",
                tr_buy="TTTC0012U",      # 실전 현금 매수 (신규 SOR/대체거래소 대응)
                tr_sell="TTTC0011U",     # 실전 현금 매도 (신규 SOR/대체거래소 대응)
                tr_cancel="TTTC0013U",   # 실전 주문 정정/취소
                tr_balance="TTTC8434R",  # 실전 주식 잔고
                tr_ccld="TTTC8001R",     # 실전 주식 일별 주문 체결 조회
            )
        else:
            return cls(
                env="vts",
                rest_url="https://openapivts.koreainvestment.com:29443",
                ws_url="ws://ops.koreainvestment.com:31000",
                tr_buy="VTTC0012U",      # 모의 현금 매수 (신규 SOR/대체거래소 대응)
                tr_sell="VTTC0011U",     # 모의 현금 매도 (신규 SOR/대체거래소 대응)
                tr_cancel="VTTC0013U",   # 모의 주문 정정/취소
                tr_balance="VTTC8434R",  # 모의 주식 잔고
                tr_ccld="VTTC8001R",     # 모의 주식 일별 주문 체결 조회
            )


class Settings(BaseModel):
    """Global system configuration settings with dual-environment support."""

    # Dual-environment mode indicators (v3 backwards compatibility)
    kis_env: str = Field(default_factory=lambda: os.getenv("KIS_ENV", "vts"))
    kis_is_virtual: bool = Field(
        default_factory=lambda: os.getenv("KIS_IS_VIRTUAL", "true").lower() == "true"
    )

    # VTS explicit settings
    kis_vts_app_key: str = Field(default_factory=lambda: os.getenv("KIS_VTS_APP_KEY", ""))
    kis_vts_app_secret: str = Field(default_factory=lambda: os.getenv("KIS_VTS_APP_SECRET", ""))
    kis_vts_cano: str = Field(default_factory=lambda: os.getenv("KIS_VTS_CANO", os.getenv("KIS_CANO", "")))
    kis_vts_acnt_prdt_cd: str = Field(
        default_factory=lambda: os.getenv("KIS_VTS_ACNT_PRDT_CD", os.getenv("KIS_ACNT_PRDT_CD", "01"))
    )

    # REAL explicit settings
    kis_real_app_key: str = Field(default_factory=lambda: os.getenv("KIS_REAL_APP_KEY", ""))
    kis_real_app_secret: str = Field(default_factory=lambda: os.getenv("KIS_REAL_APP_SECRET", ""))
    kis_real_cano: str = Field(default_factory=lambda: os.getenv("KIS_REAL_CANO", ""))
    kis_real_acnt_prdt_cd: str = Field(default_factory=lambda: os.getenv("KIS_REAL_ACNT_PRDT_CD", "01"))

    # Active environment & credentials resolution
    env: Literal["vts", "real"] = Field(
        default_factory=lambda: (
            "real"
            if os.getenv("KIS_ENV", "").lower() == "real"
            or os.getenv("KIS_IS_VIRTUAL", "true").lower() == "false"
            else "vts"
        )
    )
    app_key: str = Field(
        default_factory=lambda: (
            os.getenv("KIS_REAL_APP_KEY")
            if (os.getenv("KIS_ENV", "").lower() == "real" or os.getenv("KIS_IS_VIRTUAL", "true").lower() == "false")
            else (os.getenv("KIS_VTS_APP_KEY") or os.getenv("KIS_APP_KEY", ""))
        )
    )
    app_secret: str = Field(
        default_factory=lambda: (
            os.getenv("KIS_REAL_APP_SECRET")
            if (os.getenv("KIS_ENV", "").lower() == "real" or os.getenv("KIS_IS_VIRTUAL", "true").lower() == "false")
            else (os.getenv("KIS_VTS_APP_SECRET") or os.getenv("KIS_APP_SECRET", ""))
        )
    )
    account_no: str = Field(
        default_factory=lambda: (
            (os.getenv("KIS_REAL_CANO") or os.getenv("KIS_REAL_ACCOUNT_NO"))
            if (os.getenv("KIS_ENV", "").lower() == "real" or os.getenv("KIS_IS_VIRTUAL", "true").lower() == "false")
            else (os.getenv("KIS_VTS_CANO") or os.getenv("KIS_CANO") or os.getenv("KIS_ACCOUNT_NO", ""))
        )
    )
    cano: str = Field(
        default_factory=lambda: (
            (os.getenv("KIS_REAL_CANO") or os.getenv("KIS_REAL_ACCOUNT_NO"))
            if (os.getenv("KIS_ENV", "").lower() == "real" or os.getenv("KIS_IS_VIRTUAL", "true").lower() == "false")
            else (os.getenv("KIS_VTS_CANO") or os.getenv("KIS_CANO") or os.getenv("KIS_ACCOUNT_NO", ""))
        )
    )
    account_prdt_cd: str = Field(
        default_factory=lambda: (
            (os.getenv("KIS_REAL_ACNT_PRDT_CD") or os.getenv("KIS_REAL_ACCOUNT_PRDT_CD"))
            if (os.getenv("KIS_ENV", "").lower() == "real" or os.getenv("KIS_IS_VIRTUAL", "true").lower() == "false")
            else (os.getenv("KIS_VTS_ACNT_PRDT_CD") or os.getenv("KIS_ACNT_PRDT_CD") or os.getenv("KIS_ACCOUNT_PRDT_CD", "01"))
        )
    )
    hts_id: str = Field(default_factory=lambda: os.getenv("KIS_HTS_ID") or os.getenv("HTS_ID", ""))

    # Notifications
    telegram_bot_token: str = Field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = Field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    discord_webhook_url: str = Field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_URL", ""))

    # Directories
    token_cache_dir: Path = BASE_DIR / ".kis_tokens"
    data_dir: Path = Field(
        default_factory=lambda: Path(
            os.getenv("DATA_DIR") or (str(SHARED_DATA_DIR) if SHARED_DATA_DIR.exists() else str(BASE_DIR / "data"))
        )
    )
    logs_dir: Path = BASE_DIR / "logs"
    backtest_results_dir: Path = BASE_DIR / "backtest_results"

    # Rate limiting
    rest_max_requests_per_sec: float = 18.0  # Under 20 req/s KIS hard limit
    log_level: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    @property
    def endpoints(self) -> KISEndpoints:
        return KISEndpoints.create(self.env)

    @property
    def is_configured(self) -> bool:
        """Returns True if essential KIS Open API credentials are set."""
        return bool(
            self.app_key
            and self.app_secret
            and self.account_no
            and not self.app_key.startswith("your_")
        )

    def switch_env(self, new_env: Literal["vts", "real"]) -> Settings:
        """Switch active environment to 'vts' or 'real' and re-resolve credentials."""
        self.env = new_env
        self.kis_env = new_env
        self.kis_is_virtual = (new_env == "vts")
        if new_env == "real":
            self.app_key = os.getenv("KIS_REAL_APP_KEY") or self.kis_real_app_key or self.app_key
            self.app_secret = os.getenv("KIS_REAL_APP_SECRET") or self.kis_real_app_secret or self.app_secret
            self.account_no = (
                os.getenv("KIS_REAL_CANO")
                or os.getenv("KIS_REAL_ACCOUNT_NO")
                or self.kis_real_cano
                or self.account_no
            )
            self.account_prdt_cd = (
                os.getenv("KIS_REAL_ACNT_PRDT_CD")
                or os.getenv("KIS_REAL_ACCOUNT_PRDT_CD")
                or self.kis_real_acnt_prdt_cd
                or self.account_prdt_cd
            )
        else:
            self.app_key = (
                os.getenv("KIS_VTS_APP_KEY")
                or os.getenv("KIS_APP_KEY")
                or self.kis_vts_app_key
                or self.app_key
            )
            self.app_secret = (
                os.getenv("KIS_VTS_APP_SECRET")
                or os.getenv("KIS_APP_SECRET")
                or self.kis_vts_app_secret
                or self.app_secret
            )
            self.account_no = (
                os.getenv("KIS_VTS_CANO")
                or os.getenv("KIS_CANO")
                or os.getenv("KIS_ACCOUNT_NO")
                or self.kis_vts_cano
                or self.account_no
            )
            self.account_prdt_cd = (
                os.getenv("KIS_VTS_ACNT_PRDT_CD")
                or os.getenv("KIS_ACNT_PRDT_CD")
                or os.getenv("KIS_ACCOUNT_PRDT_CD")
                or self.kis_vts_acnt_prdt_cd
                or self.account_prdt_cd
            )
        self.cano = self.account_no
        self.hts_id = os.getenv("KIS_HTS_ID") or os.getenv("HTS_ID") or self.hts_id
        return self


# Global singleton settings instance
settings = Settings()


def switch_env(mode: Literal["vts", "real"]) -> Settings:
    """Convenience helper to switch environment on global settings."""
    return settings.switch_env(mode)
