"""
KRX Symbol and Name Resolution Utilities.
Extracts official KIS/KRX master file symbol names and code mappings.
Provides bidirectional lookup (symbol -> name, name -> symbol) with caching.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, Optional

from config.settings import settings

logger = logging.getLogger("kis.symbols")

_symbol_to_name: Dict[str, str] = {}
_name_to_symbol: Dict[str, str] = {}


def _clean_name(name: str) -> str:
    """Clean symbol name by removing trailing spaces and extra metadata."""
    return re.sub(r"\s{2,}.*$", "", name).strip()


def _load_local_metadata() -> Dict[str, str]:
    """Attempt loading symbols from local market_map.json."""
    candidates = [
        settings.data_dir / "metadata" / "market_map.json",
        Path(__file__).resolve().parent.parent / "data" / "metadata" / "market_map.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                mapping: Dict[str, str] = {}
                for code, info in data.items():
                    if isinstance(info, dict) and "name" in info:
                        mapping[code] = _clean_name(info["name"])
                    elif isinstance(info, str):
                        mapping[code] = _clean_name(info)
                if mapping:
                    return mapping
            except Exception as e:
                logger.debug(f"Failed to read local symbols from {p}: {e}")
    return {}


def refresh_krx_symbols(force_download: bool = False) -> Dict[str, str]:
    """
    Downloads and updates KRX master symbols from official KIS master servers.
    Falls back to local metadata if download fails.
    """
    global _symbol_to_name, _name_to_symbol

    downloaded: Dict[str, str] = {}
    if force_download or not _symbol_to_name:
        for market, url in [
            ("KOSPI", "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip"),
            ("KOSDAQ", "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip"),
        ]:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    z = zipfile.ZipFile(io.BytesIO(resp.read()))
                    fname = z.namelist()[0]
                    content = z.read(fname).decode("cp949", errors="ignore")
                    for line in content.splitlines():
                        code = line[:9].strip()
                        if len(code) == 6 and code.isdigit():
                            name = line[21:60].strip()
                            downloaded[code] = _clean_name(name)
            except Exception as e:
                logger.debug(f"Could not download {market} master from KIS: {e}")

    # Fall back to local metadata if download returned nothing
    if not downloaded:
        downloaded = _load_local_metadata()

    if downloaded:
        _symbol_to_name = downloaded
        _name_to_symbol = {name: code for code, name in downloaded.items()}

    return _symbol_to_name


def get_krx_name_map() -> Dict[str, str]:
    """Fetches or returns cached KRX symbol name map (code -> name)."""
    global _symbol_to_name
    if not _symbol_to_name:
        refresh_krx_symbols()
    return _symbol_to_name


def get_krx_code_map() -> Dict[str, str]:
    """Fetches or returns cached KRX reverse map (name -> code)."""
    global _name_to_symbol
    if not _name_to_symbol:
        get_krx_name_map()
    return _name_to_symbol


def get_name_by_symbol(symbol: str, default: Optional[str] = None) -> str:
    """Returns company name for a given 6-digit stock code."""
    name_map = get_krx_name_map()
    return name_map.get(symbol, default if default is not None else symbol)


def get_symbol_by_name(name: str) -> Optional[str]:
    """Returns 6-digit stock code for a given company name."""
    code_map = get_krx_code_map()
    return code_map.get(name.strip())
