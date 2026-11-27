"""
Korea Market v3 - Live Trading Pre-Flight Readiness Inspection Tool
===================================================================
Comprehensive diagnostic script verifying all KIS Open APIs, credentials,
account balance, stock holdings, and real-time streaming feeds prior to live trading.

Conducts 7 pre-flight tests:
  1. OAuth 2.0 Bearer Access Token
  2. WebSocket Approval Key
  3. Account Balance & Stock Holdings
  4. Current Market Price Quote (e.g. 005930)
  5. Market Volume & Turnover Ranking (TR FHPST01710000)
  6. HTS Condition Search API (TR HHKST03900400)
  7. WebSocket Connection & Handshake

Usage:
  python check_live_readiness.py [--env {real,vts}] [--skip-ws]
  python main.py check-live [--env {real,vts}] [--skip-ws]
"""
from __future__ import annotations

import sys
import time
import argparse
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional

# UTF-8 terminal encoding safeguard
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from config.settings import settings, Settings
from core.auth import TokenManager
from core.rest_client import KISRestClient

console = Console(force_terminal=True, legacy_windows=False)
logger = logging.getLogger("kis.preflight")
logging.basicConfig(level=logging.WARNING, format="%(message)s")


def mask_string(val: str, show_prefix: int = 4, show_suffix: int = 2) -> str:
    """Safely masks sensitive credentials for log and screen display."""
    if not val:
        return "N/A"
    if len(val) <= show_prefix + show_suffix:
        return "*" * len(val)
    return f"{val[:show_prefix]}{'*' * (len(val) - show_prefix - show_suffix)}{val[-show_suffix:]}"


def test_auth(cfg: Settings) -> tuple[bool, Dict[str, Any]]:
    """Test 1 & 2: OAuth 2.0 Access Token & WebSocket Approval Key."""
    result: Dict[str, Any] = {}
    success = True
    mgr = TokenManager(cfg)

    # 1. Access Token
    t0 = time.time()
    try:
        token = mgr.get_access_token()
        t_el = (time.time() - t0) * 1000
        expires_at = getattr(mgr, "_cached_token_expires_at", None)
        result["access_token"] = {
            "status": "PASS",
            "latency_ms": round(t_el, 1),
            "expires_at": str(expires_at) if expires_at else "Cached / Active",
            "token_masked": mask_string(token, 8, 4),
        }
    except Exception as e:
        success = False
        result["access_token"] = {"status": "FAIL", "error": str(e)}

    # 2. WebSocket Approval Key
    t0 = time.time()
    try:
        app_key = mgr.get_approval_key()
        t_el = (time.time() - t0) * 1000
        result["ws_approval_key"] = {
            "status": "PASS",
            "latency_ms": round(t_el, 1),
            "key_masked": mask_string(app_key, 8, 4),
        }
    except Exception as e:
        success = False
        result["ws_approval_key"] = {"status": "FAIL", "error": str(e)}

    return success, result


def test_balance(client: KISRestClient) -> tuple[bool, Dict[str, Any]]:
    """Test 3: Account Balance & Stock Holdings."""
    t0 = time.time()
    try:
        bal = client.get_balance()
        t_el = (time.time() - t0) * 1000
        holdings = bal.get("holdings", [])
        summary = bal.get("summary", {})

        cash = float(summary.get("dnca_tot_amt", 0.0))
        tot_equity = float(summary.get("tot_evlu_amt", 0.0))
        eval_pnl = float(summary.get("evlu_pfls_smtl_amt", 0.0))
        scts_evlu = float(summary.get("scts_evlu_amt", 0.0))

        return True, {
            "status": "PASS",
            "latency_ms": round(t_el, 1),
            "cash": cash,
            "tot_equity": tot_equity,
            "eval_pnl": eval_pnl,
            "scts_evlu": scts_evlu,
            "holdings": holdings,
        }
    except Exception as e:
        return False, {"status": "FAIL", "error": str(e)}


def test_quote_and_ranks(client: KISRestClient) -> tuple[bool, Dict[str, Any]]:
    """Test 4 & 5 & 6: Current Price, Volume Rank, Fluctuation Rank, and HTS Condition Search."""
    results: Dict[str, Any] = {}
    all_ok = True

    # 4. Current Price (005930 Samsung Electronics)
    t0 = time.time()
    try:
        px_res = client.get_current_price("005930", market_div="J")
        t_el = (time.time() - t0) * 1000
        prpr = float(px_res.get("stck_prpr", 0.0))
        oprc = float(px_res.get("stck_oprc", 0.0))
        lwpr = float(px_res.get("stck_lwpr", 0.0))
        hgpr = float(px_res.get("stck_hgpr", 0.0))
        results["current_price"] = {
            "status": "PASS",
            "latency_ms": round(t_el, 1),
            "symbol": "005930 (삼성전자)",
            "price": prpr,
            "open": oprc,
            "high": hgpr,
            "low": lwpr,
        }
    except Exception as e:
        all_ok = False
        results["current_price"] = {"status": "FAIL", "error": str(e)}

    # 5. Volume Rank (Integrated UN: KRX + NXT)
    t0 = time.time()
    try:
        v_items = client.get_volume_rank(market="0000", rank_sort="3", market_div="UN", exclude_target="1111111101")
        t_el = (time.time() - t0) * 1000
        results["volume_rank"] = {
            "status": "PASS",
            "latency_ms": round(t_el, 1),
            "count": len(v_items),
            "top5": [
                {
                    "code": it.get("mksc_shrn_iscd") or it.get("stck_shrn_iscd"),
                    "name": it.get("hts_kor_isnm"),
                    "price": float(it.get("stck_prpr", 0)),
                    "turnover_raw": float(it.get("acml_tr_pbmn", 0)),
                }
                for it in v_items[:5]
            ],
        }
    except Exception as e:
        all_ok = False
        results["volume_rank"] = {"status": "FAIL", "error": str(e)}

    # Fluctuation Rank (KRX J, Open gain rank_sort="2")
    t0 = time.time()
    try:
        f_items = client.get_fluctuation_rank(market="0000", market_div="J", rank_sort="2", exclude_target="1111111101")
        t_el = (time.time() - t0) * 1000
        results["fluctuation_rank"] = {
            "status": "PASS",
            "latency_ms": round(t_el, 1),
            "count": len(f_items),
            "top5": [
                {
                    "code": it.get("stck_shrn_iscd"),
                    "name": it.get("hts_kor_isnm"),
                    "price": float(it.get("stck_prpr", 0)),
                    "rate": float(it.get("oprc_vrss_prpr_rate", 0)),
                }
                for it in f_items[:5]
            ],
        }
    except Exception as e:
        all_ok = False
        results["fluctuation_rank"] = {"status": "FAIL", "error": str(e)}

    # 6. HTS Condition Search (TR: HHKST03900400, seq="0": 모닝_돌파주)
    t0 = time.time()
    try:
        cond_items = client.get_condition_search_result(seq="0")
        t_el = (time.time() - t0) * 1000
        results["condition_search"] = {
            "status": "PASS",
            "latency_ms": round(t_el, 1),
            "count": len(cond_items),
            "top5": [
                {
                    "code": it.get("code"),
                    "name": it.get("name"),
                    "price": float(it.get("price", 0)),
                    "open": float(it.get("open", 0)),
                    "turnover_krw": float(it.get("trade_amt", 0)) * 1000.0,
                }
                for it in cond_items[:5]
            ],
        }
    except Exception as e:
        all_ok = False
        results["condition_search"] = {"status": "FAIL", "error": str(e)}

    return all_ok, results


def test_order_inquiry(client: KISRestClient) -> tuple[bool, Dict[str, Any]]:
    """Safe read-only Order Execution/Cancellation Inquiry."""
    t0 = time.time()
    try:
        ccld = client.inquire_daily_ccld(ccld_dvsn="00")
        t_el = (time.time() - t0) * 1000
        return True, {
            "status": "PASS",
            "latency_ms": round(t_el, 1),
            "total_orders_today": len(ccld),
            "orders": ccld[:5],
        }
    except Exception as e:
        return False, {"status": "FAIL", "error": str(e)}


def test_v4_adapters_readiness(cfg: Settings) -> tuple[bool, Dict[str, Any]]:
    """Verifies v4.adapters.kis imports, NXT metadata, and H0UNCNT0/H0STCNT0 WS routing."""
    try:
        from v4.adapters.kis import (
            KisConditionSearchAdapter,
            KisLiveBrokerAdapter,
            KisLiveClock,
            KisRealtimeFeed,
            KisRegimeDataAdapter,
        )
        from v4.adapters.kis.regime_data import load_nxt_symbol_set

        nxt_set = load_nxt_symbol_set("data/metadata/nxt_symbols.json")
        feed = KisRealtimeFeed(cfg=cfg, nxt_symbols=nxt_set or {"005930"})
        nxt_tr = feed._resolve_tr_id("005930", True)
        krx_tr = feed._resolve_tr_id("999990", False)
        if nxt_tr != "H0UNCNT0" or krx_tr != "H0STCNT0":
            return False, {"status": "FAIL", "error": f"Unexpected WS routing: {nxt_tr}/{krx_tr}"}

        _ = (
            KisConditionSearchAdapter,
            KisLiveBrokerAdapter,
            KisLiveClock,
            KisRegimeDataAdapter,
        )
        return True, {
            "status": "PASS",
            "nxt_count": len(nxt_set) if nxt_set is not None else 0,
            "routing": f"NXT={nxt_tr}, KRX={krx_tr}",
        }
    except Exception as exc:
        return False, {"status": "FAIL", "error": str(exc)}


async def test_websocket_handshake(cfg: Settings) -> tuple[bool, Dict[str, Any]]:
    """Test 7: WebSocket Handshake & Connectivity."""
    import asyncio
    from core.ws_client import KISWebSocketClient

    t0 = time.time()
    ws = KISWebSocketClient(cfg)
    try:
        connect_task = asyncio.create_task(ws.connect())
        start_wait = time.time()
        while not ws.is_connected and (time.time() - start_wait) < 5.0:
            await asyncio.sleep(0.1)

        connected = ws.is_connected

        hts_ack_ok = False
        if connected and cfg.hts_id:
            wait_aes = time.time()
            while not getattr(ws, "_aes_key", None) and (time.time() - wait_aes) < 2.5:
                await asyncio.sleep(0.1)
            hts_ack_ok = bool(getattr(ws, "_aes_key", None))

        t_el = (time.time() - t0) * 1000

        await ws.disconnect()
        if not connect_task.done():
            connect_task.cancel()

        if connected:
            return True, {
                "status": "PASS",
                "latency_ms": round(t_el, 1),
                "url": cfg.endpoints.ws_url,
                "hts_id": cfg.hts_id,
                "hts_verified": hts_ack_ok,
            }
        else:
            return False, {"status": "FAIL", "error": "WebSocket connection timed out (5s)", "url": cfg.endpoints.ws_url}
    except Exception as e:
        return False, {"status": "FAIL", "error": str(e), "url": cfg.endpoints.ws_url}


def run_preflight_checks(env_override: Optional[str] = None, skip_ws: bool = False) -> bool:
    """Executes the full diagnostic suite and returns True if all pass."""
    target_env = env_override or settings.env
    cfg = settings.switch_env(target_env)
    client = KISRestClient(cfg)

    env_color = "red" if cfg.env == "real" else "green"
    env_title = "실전투자 (REAL)" if cfg.env == "real" else "모의투자 (VTS)"

    console.print()
    console.print(Panel.fit(
        f"[bold {env_color}]🚀 한국투자증권 Open API 실전매매 전 진단 검사 (Pre-Flight Healthcheck)[/bold {env_color}]\n"
        f"[cyan]실행 환경:[/cyan] [bold {env_color}]{env_title}[/bold {env_color}]  |  "
        f"[cyan]진단 시각:[/cyan] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        border_style=env_color,
    ))

    # 1. Credentials Summary
    t_env = Table(title="1. 환경 및 계정 설정 정보 (Credentials & Configuration)", title_style="bold yellow")
    t_env.add_column("항목 (Key)", style="cyan", width=25)
    t_env.add_column("설정값 (Value)", style="white")
    t_env.add_column("비고 / 설명", style="dim")

    t_env.add_row("환경 구분 (Environment)", f"[bold {env_color}]{cfg.env.upper()}[/bold {env_color}]", "real (실전) / vts (모의)")
    t_env.add_row("계좌번호 (CANO)", f"{mask_string(cfg.account_no, 4, 2)}-{cfg.account_prdt_cd}", "마스킹 처리된 종합위탁 계좌")
    t_env.add_row("App Key", mask_string(cfg.app_key, 6, 4), "KIS 발급 고유 앱키")
    t_env.add_row("App Secret", mask_string(cfg.app_secret, 6, 4), "KIS 발급 시크릿키")
    t_env.add_row("HTS ID", cfg.hts_id or "[yellow]미설정 (실시간 체결통보용)[/yellow]", "HTS ID")
    t_env.add_row("REST API URL", cfg.endpoints.rest_url, "Open API 엔드포인트")
    t_env.add_row("WebSocket URL", cfg.endpoints.ws_url, "실시간 시세/체결 피드 엔드포인트")
    console.print(t_env)
    console.print()

    checklist: List[tuple[str, str, str]] = []

    # 2. Authentication Check
    console.print("[bold yellow]2. 인증 토큰 검증 중 (OAuth 2.0 & WebSocket Approval)...[/bold yellow]")
    auth_ok, auth_res = test_auth(cfg)
    acc_tok = auth_res.get("access_token", {})
    ws_app = auth_res.get("ws_approval_key", {})

    if acc_tok.get("status") == "PASS":
        checklist.append(("OAuth2 Access Token", "PASS", f"{acc_tok.get('latency_ms')}ms (만료: {acc_tok.get('expires_at')})"))
        console.print(f"  [green]✓[/green] OAuth2 Access Token 정상 ({acc_tok.get('latency_ms')}ms, 만료: {acc_tok.get('expires_at')})")
    else:
        checklist.append(("OAuth2 Access Token", "FAIL", acc_tok.get("error", "오류")))
        console.print(f"  [red]✗[/red] OAuth2 Access Token 실패: {acc_tok.get('error')}")

    if ws_app.get("status") == "PASS":
        checklist.append(("WebSocket 승인키", "PASS", f"{ws_app.get('latency_ms')}ms (Key: {ws_app.get('key_masked')})"))
        console.print(f"  [green]✓[/green] WebSocket Approval Key 정상 ({ws_app.get('latency_ms')}ms)")
    else:
        checklist.append(("WebSocket 승인키", "FAIL", ws_app.get("error", "오류")))
        console.print(f"  [red]✗[/red] WebSocket Approval Key 실패: {ws_app.get('error')}")
    console.print()

    # 3. Account Balance & Holdings
    console.print("[bold yellow]3. 계좌 예수금 및 보유 주식 잔고 조회 중...[/bold yellow]")
    bal_ok, bal_res = test_balance(client)
    if bal_ok:
        cash = bal_res["cash"]
        tot_eq = bal_res["tot_equity"]
        eval_pnl = bal_res["eval_pnl"]
        scts = bal_res["scts_evlu"]
        holdings = bal_res["holdings"]

        cap_note = f"단리 1,000만 원 고정 운용 기준 (가용현금: {cash:,.0f}원)"
        checklist.append(("계좌 잔고 & 예수금 조회", "PASS", f"예수금: {cash:,.0f}원 ({cap_note})"))
        console.print(f"  [green]✓[/green] 잔고 조회 성공 ({bal_res['latency_ms']}ms)")

        t_bal = Table(title="[계좌 자산 현황 요약]", title_style="bold cyan")
        t_bal.add_column("구분 (Asset Category)", style="cyan")
        t_bal.add_column("금액 (Amount)", justify="right", style="bold white")
        t_bal.add_column("비고 / 전략 기준", style="green")

        t_bal.add_row("D+2 예수금 (가용 현금)", f"{cash:,.0f} 원", f"전략 배팅 자본: [bold green]{cap_note}[/bold green]")
        t_bal.add_row("유가증권 평가금액", f"{scts:,.0f} 원", "보유 주식 평가액")
        t_bal.add_row("총 평가금액 (Total Equity)", f"{tot_eq:,.0f} 원", "순자산 합계")
        pnl_style = "green" if eval_pnl >= 0 else "red"
        t_bal.add_row("당일 유가증권 평가손익", f"[{pnl_style}]{eval_pnl:+,.0f} 원[/{pnl_style}]", "미실현 손익")
        console.print(t_bal)

        if holdings:
            t_hld = Table(title=f"[현재 보유 주식 상세: {len(holdings)}종목]", title_style="bold magenta")
            t_hld.add_column("종목코드", style="cyan")
            t_hld.add_column("종목명", style="bold white")
            t_hld.add_column("보유수량", justify="right")
            t_hld.add_column("매입평균가", justify="right")
            t_hld.add_column("현재가", justify="right")
            t_hld.add_column("평가손익", justify="right")
            t_hld.add_column("수익률", justify="right")

            for h in holdings:
                code = h.get("pdno", "")
                name = h.get("prdt_name", "")
                qty = int(h.get("hldg_qty") or h.get("hld_qty") or 0)
                p_buy = float(h.get("pchs_avg_pric", 0))
                p_cur = float(h.get("prpr", 0))
                pnl = float(h.get("evlu_pfls_amt", 0))
                rate = float(h.get("evlu_pfls_rt", 0))
                st = "green" if pnl >= 0 else "red"
                t_hld.add_row(code, name, f"{qty:,}주", f"{p_buy:,.0f}원", f"{p_cur:,.0f}원", f"[{st}]{pnl:+,.0f}원[/{st}]", f"[{st}]{rate:+.2f}%[/{st}]")
            console.print(t_hld)
        else:
            console.print("  [dim]ℹ️ 현재 보유 중인 주식 잔고가 없습니다 (100% 현금 보유 상태).[/dim]")
    else:
        checklist.append(("계좌 잔고 & 예수금 조회", "FAIL", bal_res.get("error", "오류")))
        console.print(f"  [red]✗[/red] 계좌 잔고 조회 실패: {bal_res.get('error')}")
    console.print()

    # 4. Market Data & Ranking APIs
    console.print("[bold yellow]4. 시세 및 실시간 랭킹 API 점검 중...[/bold yellow]")
    mkt_ok, mkt_res = test_quote_and_ranks(client)

    px_data = mkt_res.get("current_price", {})
    if px_data.get("status") == "PASS":
        checklist.append(("현재가 시세 API (KRX)", "PASS", f"{px_data.get('latency_ms')}ms ({px_data.get('symbol')} {px_data.get('price'):,.0f}원)"))
        console.print(f"  [green]✓[/green] 현재가 시세 API 수신 성공 ({px_data.get('latency_ms')}ms) | {px_data.get('symbol')}: 현재가 {px_data.get('price'):,.0f}원")
    else:
        checklist.append(("현재가 시세 API (KRX)", "FAIL", px_data.get("error", "오류")))
        console.print(f"  [red]✗[/red] 현재가 시세 API 실패: {px_data.get('error')}")

    v_data = mkt_res.get("volume_rank", {})
    if v_data.get("status") == "PASS":
        checklist.append(("통합 거래대금 순위 API (UN)", "PASS", f"{v_data.get('latency_ms')}ms ({v_data.get('count')}개 종목 수신)"))
        console.print(f"  [green]✓[/green] 통합 거래대금 순위 API (KRX+NXT) 수신 성공 ({v_data.get('latency_ms')}ms, {v_data.get('count')}개)")
    else:
        checklist.append(("통합 거래대금 순위 API (UN)", "FAIL", v_data.get("error", "오류")))
        console.print(f"  [red]✗[/red] 통합 거래대금 순위 API 실패: {v_data.get('error')}")

    f_data = mkt_res.get("fluctuation_rank", {})
    if f_data.get("status") == "PASS":
        checklist.append(("시초가 대비 상승률 순위 (KRX)", "PASS", f"{f_data.get('latency_ms')}ms ({f_data.get('count')}개 종목 수신)"))
        console.print(f"  [green]✓[/green] KRX 시초가 대비 상승률 순위 API 수신 성공 ({f_data.get('latency_ms')}ms, {f_data.get('count')}개)")
    else:
        checklist.append(("시초가 대비 상승률 순위 (KRX)", "FAIL", f_data.get("error", "오류")))
        console.print(f"  [red]✗[/red] 시초가 대비 상승률 순위 API 실패: {f_data.get('error')}")

    cs_data = mkt_res.get("condition_search", {})
    if cs_data.get("status") == "PASS":
        checklist.append(("HTS 조건검색 API (모닝_돌파주도주)", "PASS", f"{cs_data.get('latency_ms')}ms ({cs_data.get('count')}개 종목 수신)"))
        console.print(f"  [green]✓[/green] HTS 조건검색 API (seq='0', 모닝_돌파주도주) 수신 성공 ({cs_data.get('latency_ms')}ms, {cs_data.get('count')}개)")
    else:
        checklist.append(("HTS 조건검색 API (모닝_돌파주도주)", "FAIL", cs_data.get("error", "오류")))
        console.print(f"  [red]✗[/red] HTS 조건검색 API 실패: {cs_data.get('error')}")
    console.print()

    # 5. Order Inquiry (Safe)
    console.print("[bold yellow]5. 일별 주문 체결/미체결 내역 조회 점검 (주문 미발주)...[/bold yellow]")
    ord_ok, ord_res = test_order_inquiry(client)
    if ord_ok:
        checklist.append(("일별 주문체결 조회 API", "PASS", f"{ord_res.get('latency_ms')}ms (당일 주문: {ord_res.get('total_orders_today')}건)"))
        console.print(f"  [green]✓[/green] 주문체결 조회 API 정상 작동 ({ord_res.get('latency_ms')}ms, 당일 주문: {ord_res.get('total_orders_today')}건)")
    else:
        checklist.append(("일별 주문체결 조회 API", "FAIL", ord_res.get("error", "오류")))
        console.print(f"  [red]✗[/red] 주문체결 조회 API 실패: {ord_res.get('error')}")
    console.print()

    # 6. WebSocket Handshake Test
    if not skip_ws:
        console.print("[bold yellow]6. 실시간 WebSocket 서버 연결 및 핸드셰이크 점검...[/bold yellow]")
        import asyncio
        ws_ok, ws_res = asyncio.run(test_websocket_handshake(cfg))
        if ws_ok:
            detail_msg = f"{ws_res.get('latency_ms')}ms ({ws_res.get('url')})"
            if cfg.hts_id:
                if ws_res.get("hts_verified"):
                    detail_msg += f" | 실시간 체결통보 ({cfg.hts_id}) 암호화 세션 정상"
                else:
                    detail_msg += f" | HTS_ID ({cfg.hts_id}) 응답 대기/확인 필요"
            checklist.append(("실시간 WebSocket 연결", "PASS", detail_msg))
            console.print(f"  [green]✓[/green] WebSocket 서버 핸드셰이크 성공 ({detail_msg})")
        else:
            checklist.append(("실시간 WebSocket 연결", "FAIL", ws_res.get("error", "오류")))
            console.print(f"  [red]✗[/red] WebSocket 연결 실패: {ws_res.get('error')}")
    else:
        checklist.append(("실시간 WebSocket 연결", "SKIPPED", "--skip-ws 옵션으로 생략됨"))
        console.print("  [dim]ℹ️ WebSocket 점검을 건너뜁니다 (--skip-ws).[/dim]")
    console.print()

    # 6b. v4 Adapters & NXT/KRX WS Routing Readiness
    v4_ok, v4_res = test_v4_adapters_readiness(cfg)
    if v4_ok:
        v4_detail = f"{v4_res.get('routing')} (NXT 종목: {v4_res.get('nxt_count')}개)"
        checklist.append(("v4 KIS 어댑터 & WS 라우팅", "PASS", v4_detail))
        console.print(f"  [green]✓[/green] v4 KIS 어댑터 & NXT/KRX WS 라우팅 정상 ({v4_detail})")
    else:
        checklist.append(("v4 KIS 어댑터 & WS 라우팅", "FAIL", v4_res.get("error", "오류")))
        console.print(f"  [red]✗[/red] v4 KIS 어댑터 점검 실패: {v4_res.get('error')}")
    console.print()

    # 7. Final Go / No-Go Decision Table
    all_passed = all(st == "PASS" for item_name, st, _ in checklist if item_name != "실시간 WebSocket 연결" or not skip_ws)
    status_text = "[bold green]🎯 LIVE TRADING READY (실전매매 가동 적격)[/bold green]" if all_passed else "[bold red]⚠️ ACTION REQUIRED (일부 항목 점검 필요)[/bold red]"

    t_final = Table(title="[최종 실전매매 가동 적격성 점검표 (Go / No-Go Checklist)]", title_style="bold white")
    t_final.add_column("검증 항목 (Inspection Item)", style="cyan", width=32)
    t_final.add_column("판정 (Result)", justify="center", width=12)
    t_final.add_column("세부 진단 결과", style="white")

    for item, status, detail in checklist:
        color = "green" if status == "PASS" else ("yellow" if status == "SKIPPED" else "red")
        t_final.add_row(item, f"[bold {color}]{status}[/bold {color}]", detail)

    console.print(t_final)
    console.print(Panel.fit(
        f"종합 판정: {status_text}\n"
        f"가동 명령어:\n"
        f"  - 실전투자 가동 (v4): [bold red]python main.py live --v4 --yes[/bold red]\n"
        f"  - 전자동 데일리 루틴: [bold cyan]python main.py routine --mode mock[/bold cyan]",
        border_style="green" if all_passed else "red"
    ))
    console.print()
    return all_passed


def main():
    parser = argparse.ArgumentParser(description="Korea Market v3 - Pre-Flight Live Trading Diagnostic")
    parser.add_argument("--env", type=str, choices=["real", "vts"], default=None, help="Environment to test (defaults to .env setting)")
    parser.add_argument("--skip-ws", action="store_true", help="Skip WebSocket connectivity handshake test")
    args = parser.parse_args()

    all_passed = run_preflight_checks(env_override=args.env, skip_ws=args.skip_ws)
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
