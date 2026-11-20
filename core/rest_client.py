"""
Korea Investment & Securities (KIS) REST API Client
Provides domestic stock ranking, minute quotes, account balance, and order execution.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import requests
from requests.adapters import HTTPAdapter

from config.settings import Settings, settings
from core.auth import TokenManager
from core.rate_limiter import RateLimiter, get_rate_limiter
from core.krx_ticks import round_to_krx_tick
from core.telemetry import telemetry

logger = logging.getLogger("kis.rest")


class KISRestClient:
    """REST Client for KIS Developers Open API (VTS & REAL)."""

    def __init__(
        self,
        cfg: Optional[Settings] = None,
        token_mgr: Optional[TokenManager] = None,
        limiter: Optional[RateLimiter] = None,
    ):
        self.cfg = cfg or settings
        self.token_mgr = token_mgr or TokenManager(self.cfg)
        # Automatically select 1.8 TPS for VTS or 18.0 TPS for REAL
        self.limiter = limiter or get_rate_limiter(self.cfg.env)

        # High-performance persistent connection pooling (reduces REST latency by ~150ms per call)
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=15, pool_maxsize=20, max_retries=1)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    @property
    def base_url(self) -> str:
        return self.cfg.endpoints.rest_url

    def _get_headers(self, tr_id: str, hashkey: Optional[str] = None) -> Dict[str, str]:
        token = self.token_mgr.get_access_token()
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.cfg.app_key,
            "appsecret": self.cfg.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if hashkey:
            headers["hashkey"] = hashkey
        return headers

    def _send_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        timeout: int = 10,
    ) -> requests.Response:
        t0 = time.perf_counter()
        resp = None
        status_code = 0
        rt_cd = ""
        msg1 = ""
        try:
            if method.upper() == "POST":
                resp = self.session.post(url, headers=headers, json=json, timeout=timeout)
            else:
                resp = self.session.get(url, headers=headers, params=params, timeout=timeout)
            status_code = getattr(resp, "status_code", 0)
            try:
                data = resp.json() if hasattr(resp, "json") else {}
                if isinstance(data, dict):
                    rt_cd = str(data.get("rt_cd", ""))
                    msg1 = str(data.get("msg1", ""))
            except Exception:
                pass
            return resp
        finally:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            endpoint = url.replace(self.base_url, "") if self.base_url in url else url
            telemetry.record_api_call(
                endpoint=endpoint,
                method=method,
                params=params,
                body=json,
                status_code=status_code,
                rt_cd=rt_cd,
                msg1=msg1,
                latency_ms=latency_ms,
            )

    def generate_hashkey(self, body: Dict[str, Any]) -> str:
        """Generate hashkey for sensitive POST payloads (required on REAL)."""
        self.limiter.acquire_sync()
        url = f"{self.base_url}/uapi/hashkey"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "appkey": self.cfg.app_key,
            "appsecret": self.cfg.app_secret,
        }
        resp = self._send_request("POST", url, headers=headers, json=body, timeout=5)
        resp.raise_for_status()
        return resp.json().get("HASH", "")

    # =========================================================================
    # 1. Market Data APIs
    # =========================================================================

    def _fetch_volume_rank_single(
        self,
        market_div: str,
        market: str = "0000",
        rank_sort: str = "3",
        exclude_target: str = "0000001000",  # 9번째 비트 0: 신용주문불가(증거금 100%) 종목 허용
    ) -> List[Dict[str, Any]]:
        """단일 시장(KRX: 'J', NXT: 'NX') 거래량/거래대금 순위 조회."""
        self.limiter.acquire_sync()
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/volume-rank"
        tr_id = "FHPST01710000"
        headers = self._get_headers(tr_id)
        params = {
            "FID_COND_MRKT_DIV_CODE": market_div,
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": market,               # 0000: 전체, 0001: 코스피, 1001: 코스닥
            "FID_DIV_CLS_CODE": "0",                # 0: 전체
            "FID_BLNG_CLS_CODE": rank_sort,         # 3: 거래대금순
            "FID_TRGT_CLS_CODE": "111111111",       # 증거금률 100% 포함 등 전체
            "FID_TRGT_EXLS_CLS_CODE": exclude_target,  # ETF/ETN만 제외 (우선주/스팩/위험종목 등 다른 필터 모두 해제)
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_INPUT_DATE_1": "",
        }

        resp = self._send_request("GET", url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("rt_cd") != "0":
            logger.warning(f"get_volume_rank({market_div}) returned code {data.get('rt_cd')}: {data.get('msg1')}")
            return []

        return data.get("output", [])

    def get_volume_rank(
        self,
        market: str = "0000",
        rank_sort: str = "3",  # '3': 거래대금순, '0': 거래량순
        market_div: str = "UN",  # 'UN': KRX+NXT 통합, 'J': KRX 단독, 'NX': NXT 단독
        exclude_target: str = "0000001000",  # 9번째 비트 0: 신용주문불가(증거금 100%) 종목 허용
    ) -> List[Dict[str, Any]]:
        """
        주식 거래대금/거래량 순위 조회 (TR: FHPST01710000).
        09:00 개장 직후 수급이 가장 많이 몰리는 종목들을 추출하는 핵심 API입니다.
        market_div='UN'인 경우 KRX('J')와 NXT('NX')를 각각 조회하여 거래대금을 통합 합산/정렬합니다.
        """
        if market_div in ("J", "NX", "W"):
            return self._fetch_volume_rank_single(
                market_div=market_div,
                market=market,
                rank_sort=rank_sort,
                exclude_target=exclude_target,
            )

        # UN (KRX + NXT 통합 모드)
        krx_items = self._fetch_volume_rank_single(
            market_div="J",
            market=market,
            rank_sort=rank_sort,
            exclude_target=exclude_target,
        )
        nxt_items = self._fetch_volume_rank_single(
            market_div="NX",
            market=market,
            rank_sort=rank_sort,
            exclude_target=exclude_target,
        )

        merged: Dict[str, Dict[str, Any]] = {}

        for it in krx_items:
            code = it.get("mksc_shrn_iscd") or it.get("stck_shrn_iscd")
            if not code:
                continue
            to = float(it.get("acml_tr_pbmn", 0))
            if to < 100_000_000:
                to *= 1_000_000.0
            vol = int(it.get("acml_vol", 0))
            item_copy = dict(it)
            item_copy["_krx_to"] = to
            item_copy["_nxt_to"] = 0.0
            item_copy["_tot_to"] = to
            item_copy["acml_tr_pbmn"] = str(int(to))
            item_copy["acml_vol"] = str(vol)
            merged[code] = item_copy

        for it in nxt_items:
            code = it.get("mksc_shrn_iscd") or it.get("stck_shrn_iscd")
            if not code:
                continue
            to = float(it.get("acml_tr_pbmn", 0))
            if to < 100_000_000:
                to *= 1_000_000.0
            vol = int(it.get("acml_vol", 0))
            if code in merged:
                merged[code]["_nxt_to"] = to
                merged[code]["_tot_to"] += to
                merged[code]["acml_tr_pbmn"] = str(int(merged[code]["_tot_to"]))
                merged[code]["acml_vol"] = str(int(merged[code]["acml_vol"]) + vol)
            else:
                item_copy = dict(it)
                item_copy["_krx_to"] = 0.0
                item_copy["_nxt_to"] = to
                item_copy["_tot_to"] = to
                item_copy["acml_tr_pbmn"] = str(int(to))
                item_copy["acml_vol"] = str(vol)
                merged[code] = item_copy

        # Sort by total turnover descending
        sorted_list = sorted(merged.values(), key=lambda x: x.get("_tot_to", 0.0), reverse=True)
        for idx, item in enumerate(sorted_list, 1):
            item["data_rank"] = str(idx)

        return sorted_list

    def _fetch_fluctuation_rank_single(
        self,
        market_div: str,
        market: str = "0000",
        rank_sort: str = "2",
        exclude_target: str = "0000001000",
        min_rate: str = "",
        max_rate: str = "",
    ) -> List[Dict[str, Any]]:
        self.limiter.acquire_sync()
        url = f"{self.base_url}/uapi/domestic-stock/v1/ranking/fluctuation"
        tr_id = "FHPST01700000"
        headers = self._get_headers(tr_id)
        params = {
            "FID_COND_MRKT_DIV_CODE": market_div,
            "FID_COND_SCR_DIV_CODE": "20170",
            "FID_INPUT_ISCD": market,
            "FID_RANK_SORT_CLS_CODE": rank_sort,
            "FID_INPUT_CNT_1": "0",
            "FID_PRC_CLS_CODE": "0",
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_TRGT_CLS_CODE": "0",
            "FID_TRGT_EXLS_CLS_CODE": exclude_target,
            "FID_DIV_CLS_CODE": "0",
            "FID_RSFL_RATE1": min_rate,
            "FID_RSFL_RATE2": max_rate,
        }
        resp = self._send_request("GET", url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") != "0":
            logger.warning(f"get_fluctuation_rank({market_div}) returned code {data.get('rt_cd')}: {data.get('msg1')}")
            return []
        return data.get("output", [])

    def get_fluctuation_rank(
        self,
        market: str = "0000",
        market_div: str = "UN",  # 'UN': KRX+NXT 통합 모드 (기본값)
        rank_sort: str = "2",   # '2': 시가대비상승율순, '0': 전일대비상승율순
        exclude_target: str = "0000001000",  # 9번째 비트 0: 신용주문불가(증거금 100%) 종목 허용
        min_rate: str = "",
        max_rate: str = "",
    ) -> List[Dict[str, Any]]:
        """
        국내주식 등락율 순위 조회 (TR: FHPST01700000).
        market_div='UN'인 경우 KRX('J')와 NXT('NX') 양쪽의 시가대비 상승율 리더를
        각각 조회하여 중복 없이 통합 풀로 병합 반환합니다.
        """
        if market_div in ("J", "NX", "W"):
            return self._fetch_fluctuation_rank_single(
                market_div=market_div,
                market=market,
                rank_sort=rank_sort,
                exclude_target=exclude_target,
                min_rate=min_rate,
                max_rate=max_rate,
            )

        # UN (KRX + NXT 통합 병합)
        krx_items = self._fetch_fluctuation_rank_single(
            market_div="J",
            market=market,
            rank_sort=rank_sort,
            exclude_target=exclude_target,
            min_rate=min_rate,
            max_rate=max_rate,
        )
        nxt_items = self._fetch_fluctuation_rank_single(
            market_div="NX",
            market=market,
            rank_sort=rank_sort,
            exclude_target=exclude_target,
            min_rate=min_rate,
            max_rate=max_rate,
        )

        merged: Dict[str, Dict[str, Any]] = {}
        for it in krx_items:
            code = it.get("stck_shrn_iscd")
            if code:
                it_copy = dict(it)
                it_copy["_market_src"] = "KRX"
                merged[code] = it_copy

        for it in nxt_items:
            code = it.get("stck_shrn_iscd")
            if not code:
                continue
            if code not in merged:
                it_copy = dict(it)
                it_copy["_market_src"] = "NXT"
                merged[code] = it_copy
            else:
                merged[code]["_market_src"] = "BOTH"
                nxt_rate = float(it.get("oprc_vrss_prpr_rate", 0.0) or 0.0)
                krx_rate = float(merged[code].get("oprc_vrss_prpr_rate", 0.0) or 0.0)
                if nxt_rate > krx_rate:
                    merged[code]["oprc_vrss_prpr_rate"] = str(nxt_rate)

        def _get_rate(x: Dict[str, Any]) -> float:
            try:
                return float(x.get("oprc_vrss_prpr_rate", 0.0))
            except Exception:
                return 0.0

        sorted_list = sorted(merged.values(), key=_get_rate, reverse=True)
        for idx, item in enumerate(sorted_list, 1):
            item["data_rank"] = str(idx)
        return sorted_list

    def get_current_price(self, symbol: str, market_div: str = "UN") -> Dict[str, Any]:
        """주식 현재가 시세 조회 (TR: FHKST01010100). market_div='UN'으로 통합 시세 조회."""
        self.limiter.acquire_sync()
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        tr_id = "FHKST01010100"
        headers = self._get_headers(tr_id)
        params = {
            "FID_COND_MRKT_DIV_CODE": market_div,
            "FID_INPUT_ISCD": symbol,
        }
        resp = self._send_request("GET", url, headers=headers, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return data.get("output") or {}

    def get_index_price(self, iscd: str = "1001", market_div: str = "U") -> Dict[str, Any]:
        """국내업종 현재지수 조회 (TR: FHPUP02100000). 코스닥 1001, 코스피 0001 등 실시간 등락률 조회."""
        self.limiter.acquire_sync()
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-index-price"
        tr_id = "FHPUP02100000"
        headers = self._get_headers(tr_id)
        params = {
            "FID_COND_MRKT_DIV_CODE": market_div,
            "FID_INPUT_ISCD": iscd,
        }
        resp = self._send_request("GET", url, headers=headers, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return data.get("output") or {}

    def get_today_minute_bars(
        self,
        symbol: str,
        to_hour: Optional[str] = None,
        market_div: str = "UN",  # 'UN': KRX+NXT 통합 분봉 우선, 미지원 종목은 'J'로 자동 폴백
    ) -> List[Dict[str, Any]]:
        """
        주식 당일 분봉 조회 (TR: FHKST03010200).
        장중 실시간 주도주 발굴 시(구독 시점) 09:00부터 현재까지의 공인 1분봉 데이터를
        1회 즉시 동기화하여 진입 조건 및 기준봉 손절선을 완전체로 복원합니다.
        1차로 market_div='UN'(KRX+NXT 통합)을 시도하며,
        넥스트레이드 비대상 종목으로 인해 거래량이 0인 경우 2차로 'J'(KRX 정규장)로 자동 폴백하여
        모든 종목의 정확한 시초가와 통합 거래대금을 완전하게 수집합니다.
        """
        self.limiter.acquire_sync()
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
        tr_id = "FHKST03010200"
        headers = self._get_headers(tr_id)
        raw_hour = to_hour or datetime.now().strftime("%H%M%S")
        hour_str = min(raw_hour, "153000") if raw_hour > "153000" else raw_hour
        params = {
            "FID_COND_MRKT_DIV_CODE": market_div,
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_HOUR_1": hour_str,
            "FID_PW_DATA_INCU_YN": "N",
            "FID_ETC_CLS_CODE": "",
        }
        bars: List[Dict[str, Any]] = []
        try:
            resp = self._send_request("GET", url, headers=headers, params=params, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            if data.get("rt_cd") == "0":
                bars = data.get("output2", [])
        except Exception as e:
            logger.debug(f"get_today_minute_bars({symbol}, {market_div}) failed: {e}")

        # Check if UN returned real trading volume (NXT supported stock)
        # If UN returned only 0-volume placeholder bars (non-NXT stock like 020400), fallback to 'J' (KRX)
        has_volume = any(float(b.get("cntg_vol") or 0) > 0 for b in bars)
        if market_div != "J" and (not bars or not has_volume):
            params["FID_COND_MRKT_DIV_CODE"] = "J"
            try:
                self.limiter.acquire_sync()
                resp = self._send_request("GET", url, headers=headers, params=params, timeout=5)
                resp.raise_for_status()
                data = resp.json()
                if data.get("rt_cd") == "0":
                    bars = data.get("output2", [])
            except Exception as e:
                logger.warning(f"get_today_minute_bars fallback to 'J' failed for {symbol}: {e}")

        return bars

    # =========================================================================
    # 1b. HTS Condition Search APIs (종목조건검색)
    # =========================================================================

    def get_condition_search_title(self) -> List[Dict[str, Any]]:
        """
        HTS 서버 저장 사용자 조건검색 목록 조회 (TR: HHKST03900300).
        등록된 조건식의 seq 및 condition_nm 목록을 반환합니다.
        """
        self.limiter.acquire_sync()
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/psearch-title"
        tr_id = "HHKST03900300"
        headers = self._get_headers(tr_id)
        params = {"user_id": self.cfg.hts_id}
        resp = self._send_request("GET", url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") != "0":
            logger.warning(f"get_condition_search_title returned {data.get('rt_cd')}: {data.get('msg1')}")
            return []
        return data.get("output2", []) or data.get("output", [])

    def get_condition_search_result(self, seq: str = "0") -> List[Dict[str, Any]]:
        """
        HTS 서버 저장 종목조건검색 결과 실시간 조회 (TR: HHKST03900400).
        지정된 조건식(seq)에 부합하는 종목 리스트(최대 100건)를 반환합니다.
        반환 필드: code(종목코드), name(종목명), price(현재가), open(시가),
                  high(고가), low(저가), chgrate(등락률), trade_amt(거래대금-천원단위), cttr(체결강도)
        """
        self.limiter.acquire_sync()
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/psearch-result"
        tr_id = "HHKST03900400"
        headers = self._get_headers(tr_id)
        params = {
            "user_id": self.cfg.hts_id,
            "seq": str(seq),
        }
        resp = self._send_request("GET", url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") != "0":
            logger.warning(f"get_condition_search_result(seq={seq}) returned {data.get('rt_cd')}: {data.get('msg1')}")
            return []
        return data.get("output2", []) or data.get("output", [])

    # =========================================================================
    # 2. Account & Balance APIs
    # =========================================================================

    def get_balance(self) -> Dict[str, Any]:
        """
        주식 잔고 및 계좌 평가현황 조회 (TR: VTTC8434R for VTS, TTTC8434R for REAL).
        """
        self.limiter.acquire_sync()
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        tr_id = self.cfg.endpoints.tr_balance
        headers = self._get_headers(tr_id)
        params = {
            "CANO": self.cfg.account_no,
            "ACNT_PRDT_CD": self.cfg.account_prdt_cd,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        resp = self._send_request("GET", url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return {
            "holdings": data.get("output1", []),
            "summary": data.get("output2", [{}])[0] if data.get("output2") else {},
        }

    # =========================================================================
    # 3. Order Execution APIs
    # =========================================================================

    def order_cash_buy(
        self,
        symbol: str,
        qty: int,
        price: int = 0,
        order_type: str = "01",  # "01": 시장가, "00": 지정가, "03": 최유리지정가
        exchange: Optional[str] = None,  # Defaults: "KRX" for VTS, "SOR" for REAL
    ) -> Dict[str, Any]:
        """
        현금 매수 주문 (TR: VTTC0012U for VTS, TTTC0012U for REAL).
        Conforms strictly to KRX tick rules via round_to_krx_tick.
        """
        if exchange is None:
            exchange = "KRX" if getattr(self.cfg, "env", "real") == "vts" else "SOR"

        if price > 0:
            price = round_to_krx_tick(float(price), side="BUY")

        self.limiter.acquire_sync()
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        tr_id = self.cfg.endpoints.tr_buy
        body = {
            "CANO": self.cfg.account_no,
            "ACNT_PRDT_CD": self.cfg.account_prdt_cd,
            "PDNO": symbol,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
            "EXCG_ID_DVSN_CD": exchange,
        }
        hashkey = self.generate_hashkey(body) if self.cfg.env == "real" else None
        headers = self._get_headers(tr_id, hashkey=hashkey)

        type_str = "MARKET" if order_type == "01" else ("BEST_LIMIT(03)" if order_type == "03" else f"LIMIT@{price}")
        logger.info(f"Submitting BUY order ({exchange}): {symbol} x {qty} @ {type_str}")
        resp = self._send_request("POST", url, headers=headers, json=body, timeout=5)
        resp.raise_for_status()
        return resp.json()

    def order_cash_sell(
        self,
        symbol: str,
        qty: int,
        price: int = 0,
        order_type: str = "01",  # "01": 시장가, "00": 지정가, "03": 최유리지정가
        exchange: Optional[str] = None,  # Defaults: "KRX" for VTS, "SOR" for REAL
    ) -> Dict[str, Any]:
        """
        현금 매도 주문 (TR: VTTC0011U for VTS, TTTC0011U for REAL).
        Conforms strictly to KRX tick rules via round_to_krx_tick.
        """
        if exchange is None:
            exchange = "KRX" if getattr(self.cfg, "env", "real") == "vts" else "SOR"

        if price > 0:
            price = round_to_krx_tick(float(price), side="SELL")

        self.limiter.acquire_sync()
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        tr_id = self.cfg.endpoints.tr_sell
        body = {
            "CANO": self.cfg.account_no,
            "ACNT_PRDT_CD": self.cfg.account_prdt_cd,
            "PDNO": symbol,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
            "EXCG_ID_DVSN_CD": exchange,
        }
        hashkey = self.generate_hashkey(body) if self.cfg.env == "real" else None
        headers = self._get_headers(tr_id, hashkey=hashkey)

        type_str = "MARKET" if order_type == "01" else ("BEST_LIMIT(03)" if order_type == "03" else f"LIMIT@{price}")
        logger.info(f"Submitting SELL order ({exchange}): {symbol} x {qty} @ {type_str}")
        resp = self._send_request("POST", url, headers=headers, json=body, timeout=5)
        resp.raise_for_status()
        return resp.json()

    def order_cancel(
        self,
        order_no: str,
        org_no: str = "00000",
        qty: int = 0,
        cancel_all: bool = True,
        exchange: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        주문 취소 (TR: VTTC0013U for VTS, TTTC0013U for REAL).
        """
        if exchange is None:
            exchange = "KRX" if getattr(self.cfg, "env", "real") == "vts" else "SOR"

        self.limiter.acquire_sync()
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl"
        tr_id = self.cfg.endpoints.tr_cancel
        body = {
            "CANO": self.cfg.account_no,
            "ACNT_PRDT_CD": self.cfg.account_prdt_cd,
            "KRX_FWDG_ORD_ORGNO": org_no,
            "ORGN_ODNO": order_no,
            "ORD_DVSN": "00",
            "RVSN_CNCL_DVSN_CD": "02",  # 02: 취소
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y" if cancel_all else "N",
            "EXCG_ID_DVSN_CD": exchange,
        }
        hashkey = self.generate_hashkey(body) if self.cfg.env == "real" else None
        headers = self._get_headers(tr_id, hashkey=hashkey)

        logger.info(f"Submitting CANCEL order: ODNO={order_no}, ALL={cancel_all}")
        resp = self._send_request("POST", url, headers=headers, json=body, timeout=5)
        resp.raise_for_status()
        return resp.json()

    def inquire_daily_ccld(
        self,
        order_no: Optional[str] = None,
        symbol: str = "",
        ccld_dvsn: str = "00",  # "00": 전체, "01": 체결, "02": 미체결
    ) -> List[Dict[str, Any]]:
        """
        주식 일별 주문 체결/미체결 조회 (TR: VTTC8001R for VTS, TTTC8001R for REAL).
        Returns real fill details: order ID, symbol, executed shares, avg price, unfilled shares.
        """
        self.limiter.acquire_sync()
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        tr_id = self.cfg.endpoints.tr_ccld
        headers = self._get_headers(tr_id)
        today_str = datetime.now().strftime("%Y%m%d")
        params = {
            "CANO": self.cfg.account_no,
            "ACNT_PRDT_CD": self.cfg.account_prdt_cd,
            "INQR_STRT_DT": today_str,
            "INQR_END_DT": today_str,
            "SLL_BUY_DVSN_CD": "00",      # 00: 전체, 01: 매도, 02: 매수
            "INQR_DVSN": "00",             # 00: 역순
            "PDNO": symbol,
            "CCLD_DVSN": ccld_dvsn,        # 00: 전체, 01: 체결, 02: 미체결
            "ORD_GNO_BRNO": "",
            "ODNO": order_no or "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        resp = self._send_request("GET", url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("output1", [])

    def get_kospi_gap_and_market_regime(self) -> Dict[str, Any]:
        """
        국내업종 일별 지수 시세 조회 (TR: FHKUP03500100).
        09:00:01에 KOSPI(0001) 및 KOSDAQ(1001) 전일 종가, 당일 시가 및 갭률(%)과 5일선 추세를 병렬 조회합니다.

        규칙 [5일선 추세 결합]:
        - (KOSPI > 5MA OR KOSDAQ > 5MA) AND KOSDAQ 갭 >= 0.0%: 5슬롯 확장 (BULL)
        - 그 외: 3슬롯 방어 (BEAR/DEFENSIVE)

        Returns:
            Dict containing:
                - "kospi_gap_pct": float
                - "kosdaq_gap_pct": float
                - "is_both_gap_up": bool
                - "ks_above_5ma": bool
                - "kq_above_5ma": bool
                - "is_trend_bull": bool
                - "regime": str ("BULL" or "BEAR")
                - "max_slots": int (5 if BULL else 3)
                - "adr_pct": float
        """
        today_str = datetime.now().strftime("%Y%m%d")
        start_str = (datetime.now() - timedelta(days=25)).strftime("%Y%m%d")
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
        tr_id = "FHKUP03500100"

        def _fetch_index(iscd: str) -> tuple[float, float, float, float, float, bool]:
            self.limiter.acquire_sync()
            headers = self._get_headers(tr_id)
            params = {
                "FID_COND_MRKT_DIV_CODE": "U",
                "FID_INPUT_ISCD": iscd,
                "FID_INPUT_DATE_1": start_str,
                "FID_INPUT_DATE_2": today_str,
                "FID_PERIOD_DIV_CODE": "D",
            }
            resp = self._send_request("GET", url, headers=headers, params=params, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            if data.get("rt_cd") != "0":
                return -999.0, 0.0, 0.0, 50.0, 0.0, False
            out1 = data.get("output1", {})
            out2 = data.get("output2", [])

            # Check if out2[0] represents today or previous business day
            if out2 and out2[0].get("stck_bsop_date") == today_str:
                today_open = float(out2[0].get("bstp_nmix_oprc") or out1.get("bstp_nmix_oprc") or 0.0)
                past_closes = [float(x.get("bstp_nmix_prpr", 0.0)) for x in out2[1:6] if x.get("bstp_nmix_prpr")]
                prev_close = past_closes[0] if past_closes else float(out1.get("prdy_nmix", 0.0) or 0.0)
            else:
                today_open = float(out1.get("bstp_nmix_oprc") or (out2[0].get("bstp_nmix_oprc") if out2 else 0.0))
                past_closes = [float(x.get("bstp_nmix_prpr", 0.0)) for x in out2[:5] if x.get("bstp_nmix_prpr")]
                prev_close = past_closes[0] if past_closes else float(out1.get("prdy_nmix", 0.0) or 0.0)

            ascn = float(out1.get("ascn_issu_cnt", 0.0) or 0.0)
            down = float(out1.get("down_issu_cnt", 0.0) or 0.0)
            adr = (ascn / (ascn + down) * 100.0) if (ascn + down) > 0 else 50.0

            if prev_close > 0 and today_open > 0:
                gap = ((today_open - prev_close) / prev_close * 100.0)
            else:
                gap = -999.0

            ma5 = (sum(past_closes) / len(past_closes)) if len(past_closes) >= 5 else (prev_close if prev_close > 0 else 0.0)
            above_5ma = (prev_close >= ma5) or (today_open >= ma5 if today_open > 0 else False)
            return gap, prev_close, today_open, adr, ma5, above_5ma

        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as executor:
                f_ks = executor.submit(_fetch_index, "0001")
                f_kq = executor.submit(_fetch_index, "1001")
                ks_gap, ks_prev, ks_open, ks_adr, ks_ma5, ks_above = f_ks.result(timeout=6)
                try:
                    kq_gap, kq_prev, kq_open, kq_adr, kq_ma5, kq_above = f_kq.result(timeout=6)
                except Exception as e_kq:
                    logger.warning(f"Could not fetch KOSDAQ index, using KOSPI gap fallback: {e_kq}")
                    kq_gap = ks_gap
                    kq_adr = ks_adr
                    kq_ma5 = ks_ma5
                    kq_above = ks_above

            ks_gap_val = ks_gap if ks_gap != -999.0 else 0.0
            kq_gap_val = kq_gap if kq_gap != -999.0 else 0.0

            is_both_gap_up = (ks_gap >= 0.0 and kq_gap >= 0.0)
            is_trend_bull = (ks_above or kq_above) and (kq_gap >= 0.0)
            regime = "BULL" if is_trend_bull else "BEAR"
            max_slots = 5 if is_trend_bull else 3

            result = {
                "kospi_gap_pct": round(ks_gap_val, 4),
                "kosdaq_gap_pct": round(kq_gap_val, 4),
                "ks_above_5ma": ks_above,
                "kq_above_5ma": kq_above,
                "ks_ma5": round(ks_ma5, 2),
                "kq_ma5": round(kq_ma5, 2),
                "is_both_gap_up": is_both_gap_up,
                "is_trend_bull": is_trend_bull,
                "prev_close": ks_prev,
                "today_open": ks_open,
                "regime": regime,
                "max_slots": max_slots,
                "adr_pct": round(ks_adr, 2),
            }

            return result
        except Exception as e:
            logger.error(f"Error fetching KOSPI/KOSDAQ gap/regime: {e}")
            return {
                "kospi_gap_pct": 0.0,
                "kosdaq_gap_pct": 0.0,
                "ks_above_5ma": False,
                "kq_above_5ma": False,
                "is_both_gap_up": False,
                "is_trend_bull": False,
                "regime": "BEAR",
                "max_slots": 3,
                "error": str(e),
            }
