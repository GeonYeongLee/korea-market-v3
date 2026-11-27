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
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Set

from config.settings import settings

logger = logging.getLogger("kis.symbols")

_symbol_to_name: Dict[str, str] = {}
_name_to_symbol: Dict[str, str] = {}
_nxt_symbols: Set[str] = set()
_nxt_loaded: bool = False


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


def _nxt_cache_candidates() -> list[Path]:
    return [
        settings.data_dir / "metadata" / "nxt_symbols.json",
        Path(__file__).resolve().parent.parent / "data" / "metadata" / "nxt_symbols.json",
    ]


def refresh_nxt_symbols(force_download: bool = False) -> Set[str]:
    """
    Loads Nextrade (NXT) tradable stock codes from official KIS master files
    (`nxt_kospi_code.mst.zip`, `nxt_kosdaq_code.mst.zip`) with daily local JSON cache.
    Returns empty set on failure so callers safely default to KRX `H0STCNT0`.
    """
    global _nxt_symbols, _nxt_loaded

    if not force_download and _nxt_loaded:
        return set(_nxt_symbols)

    today_str = datetime.now().strftime("%Y%m%d")
    cached_fallback: Set[str] = set()
    for p in _nxt_cache_candidates():
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                syms = {str(s).strip() for s in payload.get("symbols", []) if str(s).strip()}
                if syms:
                    cached_fallback = syms
                    if not force_download and payload.get("date") == today_str:
                        _nxt_symbols = syms
                        _nxt_loaded = True
                        return set(_nxt_symbols)
                    break
            except Exception as e:
                logger.debug(f"Failed reading NXT symbol cache {p}: {e}")

    downloaded: Set[str] = set()
    for market, url in [
        ("NXT_KOSPI", "https://new.real.download.dws.co.kr/common/master/nxt_kospi_code.mst.zip"),
        ("NXT_KOSDAQ", "https://new.real.download.dws.co.kr/common/master/nxt_kosdaq_code.mst.zip"),
    ]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                z = zipfile.ZipFile(io.BytesIO(resp.read()))
                fname = z.namelist()[0]
                content = z.read(fname).decode("cp949", errors="ignore")
                for line in content.splitlines():
                    code = line[:9].strip()
                    if len(code) == 6 and code.isdigit():
                        downloaded.add(code)
        except Exception as e:
            logger.debug(f"Could not download {market} master from KIS: {e}")

    if downloaded:
        _nxt_symbols = downloaded
        _nxt_loaded = True
        for p in _nxt_cache_candidates():
            try:
                if p.parent.exists():
                    tmp_p = p.with_suffix(".tmp")
                    with open(tmp_p, "w", encoding="utf-8") as f:
                        json.dump({"date": today_str, "symbols": sorted(downloaded)}, f)
                    os.replace(tmp_p, p)
                    break
            except Exception as e:
                logger.debug(f"Could not persist NXT symbol cache {p}: {e}")
    elif cached_fallback:
        _nxt_symbols = cached_fallback
        _nxt_loaded = True
    else:
        _nxt_loaded = True

    return set(_nxt_symbols)


def get_nxt_symbols() -> Set[str]:
    """Returns cached set of Nextrade (NXT) tradable stock codes."""
    if not _nxt_loaded:
        return refresh_nxt_symbols()
    return set(_nxt_symbols)


def is_nxt_symbol(symbol: str) -> bool:
    """Returns True if symbol is eligible for integrated NXT+KRX tick stream (H0UNCNT0)."""
    return str(symbol).strip() in get_nxt_symbols()


def load_nxt_symbol_set(nxt_file: Optional[str]) -> Optional[Set[str]]:
    """Loads NXT-eligible 6-digit symbol codes from JSON metadata if present."""
    if not nxt_file or not os.path.exists(nxt_file):
        return None
    try:
        with open(nxt_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            if "symbols" in raw and isinstance(raw["symbols"], list):
                return {str(s).strip() for s in raw["symbols"] if str(s).strip()}
            return {str(k).strip() for k, v in raw.items() if v and str(k).strip()}
        if isinstance(raw, list):
            return {str(s).strip() for s in raw if str(s).strip()}
    except Exception as exc:
        logger.warning(f"Failed to load NXT symbols from {nxt_file}: {exc}")
    return None
