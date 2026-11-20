import duckdb
import pandas as pd
import numpy as np
import time

def run_quant_risk_audit():
    print("=" * 120)
    print("🔬 [독립 퀀트 리스크 감사 및 사이징 엔지니어 종합 리포트]")
    print("   1. 3단계 시장 국면(3-Tier Regime) 사이징 전수 탐색")
    print("   2. 하방 꼬리 미세 필터(Lower Shadow Cutoff) 영향 정량 검증")
    print("   3. NXT 장전 익절 타겟(+2.5% vs +3.0% vs +3.5%) 비교 최적화")
    print("   4. 순수 단리 1,000만 원 / 체결 780~850건 / 샤프 1.30+ / MDD < 9% 챔피언 선별")
    print("=" * 120)
    t0 = time.time()

    con = duckdb.connect()
    bars_path = 'data/dataset_2y/daily_bars_all.parquet'
    idx_path = 'data/dataset_2y/index_daily_full.parquet'
    pool_path = 'research/expanded_candidate_pool.parquet'
    nxt_path = 'research/nxt_trades_merged.parquet'

    # 1. Load bars with lower shadow and open/high/low
    q = f"""
    WITH bars_hl AS (
        SELECT symbol, strftime(date, '%Y-%m-%d') as date_str,
               open as bar_open, high as bar_high, low as bar_low, close as bar_close,
               CASE WHEN (high - low) > 0 THEN (least(open, close) - low) / (high - low) ELSE 0.0 END as lower_shadow,
               CASE WHEN open > 0 THEN (close - open) / open * 100.0 ELSE 0.0 END as body_ret
        FROM '{bars_path}'
        WHERE date >= '2020-01-01'
    )
    SELECT p.*, b.bar_open, b.bar_high, b.bar_low, b.lower_shadow, b.body_ret,
           strftime(p.next_date, '%Y-%m-%d') as next_date_str
    FROM '{pool_path}' p
    JOIN bars_hl b ON p.symbol = b.symbol AND p.date_str = b.date_str
    """
    df = con.execute(q).df()
    print(f"Loaded enhanced pool: {len(df):,} rows")

    all_trading_days = sorted(con.execute(f"SELECT DISTINCT strftime(date, '%Y-%m-%d') as d FROM '{bars_path}' WHERE date >= '2020-01-01'").df()['d'])

    # 2. Market Regime Map (Lookahead 0%: uses confirmed index data of date d)
    idx_df = con.execute(f"""
        SELECT date as date_str,
               ks_close, ks_ma10, ks_ma20, ks_ma60,
               ((ks_ma10 >= ks_ma20) AND (ks_close >= ks_ma60)) as is_super_bull,
               ((ks_ma10 >= ks_ma20) AND (ks_close < ks_ma60)) as is_normal_bull,
               (ks_ma10 < ks_ma20) as is_bear
        FROM '{idx_path}'
        WHERE date >= '2020-01-01'
    """).df().set_index('date_str')
    regime_map = idx_df.to_dict(orient='index')

    # 3. Load NXT Premarket TP maps for +2.5%, +3.0%, +3.5%
    nxt_df = pd.read_parquet(nxt_path)
    nxt_valid = nxt_df[nxt_df['pm_close'].notna()].copy()
    nxt_tp_maps = {}
    for tp_val in [2.5, 3.0, 3.5]:
        m = {}
        for (sym, nd), group in nxt_valid.groupby(['symbol', 'exit_date']):
            entry_p = group.iloc[0]['entry_price']
            first_open = group.iloc[0]['pm_open']
            max_high = group['pm_high'].max()
            target = entry_p * (1.0 + tp_val / 100.0)
            if max_high >= target:
                m[(sym, nd)] = max(first_open, target)
        nxt_tp_maps[tp_val] = m

    for tp_val, col_name in [(2.5, 'pnl_nxt_25'), (3.0, 'pnl_nxt_30'), (3.5, 'pnl_nxt_35')]:
        tp_m = nxt_tp_maps[tp_val]
        pnls = []
        for _, r in df.iterrows():
            key = (r['symbol'], r['next_date_str'])
            if key in tp_m:
                exit_p = tp_m[key]
                p = ((exit_p - r['close']) / r['close'] * 100.0) - 0.28
            else:
                p = r['pnl_open']
            pnls.append(p)
        df[col_name] = pnls

    # Scoring models
    df['s_rank'] = (25.0 - df['to_rank'].clip(1, 25)) / 24.0
    df['s_shadow'] = (0.12 - df['upper_shadow'].clip(0, 0.12)) / 0.12
    df['s_60d'] = (df['ratio_60d'].clip(0.95, 1.10) - 0.95) / 0.15
    df['s_body'] = (df['body_ret'].clip(4.0, 16.0) - 4.0) / 12.0

    df['score_baseline'] = (20.0 - df['to_rank'].clip(1, 20))/19.0 * 0.40 + (0.12 - df['upper_shadow'].clip(0, 0.12))/0.12 * 0.35 + (df['ratio_60d'].clip(0.95, 1.10) - 0.95)/0.15 * 0.25
    df['score_alpha'] = df['s_rank'] * 0.30 + df['s_shadow'] * 0.35 + df['s_60d'] * 0.20 + df['s_body'] * 0.15

    # ==================================================================================
    # [TASK 1: 하방 꼬리(Lower Shadow) 단독 필터 영향 정밀 감사]
    # ==================================================================================
    print("\n" + "=" * 120)
    print("📊 [TASK 1: 하방 꼬리(Lower Shadow) 미세 필터 정밀 감사]")
    print("=" * 120)

    # Base pool definition for comparison: Tiered TOP 20
    cond_base = (
        ((df['to_rank'] <= 10) & (df['change_pct'] >= 8.0) & (df['change_pct'] <= 22.0) & (df['upper_shadow'] <= 0.12)) |
        ((df['to_rank'] > 10) & (df['to_rank'] <= 20) & (df['change_pct'] >= 10.0) & (df['change_pct'] <= 22.0) & (df['upper_shadow'] <= 0.10))
    )
    base_pool = df[cond_base].copy()

    print(f"기준 풀(Tiered TOP 20) 전체 후보 수: {len(base_pool):,}개")
    for ls_cut in [0.20, 0.18, 0.15]:
        passed = base_pool[base_pool['lower_shadow'] <= ls_cut]
        dropped = base_pool[base_pool['lower_shadow'] > ls_cut]
        drop_pct = len(dropped) / len(base_pool) * 100.0

        wr_pass = (passed['pnl_open'] > 0).mean() * 100.0
        pf_pass = passed[passed['pnl_open'] > 0]['pnl_open'].sum() / abs(passed[passed['pnl_open'] <= 0]['pnl_open'].sum())
        avg_pass = passed['pnl_open'].mean()

        wr_drop = (dropped['pnl_open'] > 0).mean() * 100.0 if len(dropped) > 0 else 0
        pf_drop = (dropped[dropped['pnl_open'] > 0]['pnl_open'].sum() / abs(dropped[dropped['pnl_open'] <= 0]['pnl_open'].sum())) if len(dropped) > 0 else 0
        avg_drop = dropped['pnl_open'].mean() if len(dropped) > 0 else 0

        print(f"\n[하방 꼬리 컷오프 <= {ls_cut:.2f}]")
        print(f"  • 통과군: {len(passed):>4}개 ({100-drop_pct:>5.1f}%) | 승률: {wr_pass:>5.1f}% | PF: {pf_pass:>4.2f} | 평균순익: {avg_pass:>+6.2f}%")
        print(f"  • 탈락군: {len(dropped):>4}개 ({drop_pct:>5.1f}%) | 승률: {wr_drop:>5.1f}% | PF: {pf_drop:>4.2f} | 평균순익: {avg_drop:>+6.2f}%")

    # ==================================================================================
    # [TASK 2: NXT 장전 익절 타겟 최적화 (+2.5% vs +3.0% vs +3.5%)]
    # ==================================================================================
    print("\n" + "=" * 120)
    print("📊 [TASK 2: NXT 프리마켓(08:00~08:50) 익절 타겟별 단독 성과 비교]")
    print("=" * 120)

    # Inspect on the entire base pool
    for p_col, lbl in [('pnl_open', '09:00 KRX 시초가 청산'),
                       ('pnl_nxt_25', 'NXT 프리마켓 +2.5% 익절'),
                       ('pnl_nxt_30', 'NXT 프리마켓 +3.0% 익절'),
                       ('pnl_nxt_35', 'NXT 프리마켓 +3.5% 익절')]:
        wr = (base_pool[p_col] > 0).mean() * 100.0
        w_sum = base_pool[base_pool[p_col] > 0][p_col].sum()
        l_sum = abs(base_pool[base_pool[p_col] <= 0][p_col].sum())
        pf = w_sum / l_sum if l_sum > 0 else 999.0
        avg_ret = base_pool[p_col].mean()
        tot_ret = base_pool[p_col].sum()
        print(f"  • {lbl:<25}: 승률 {wr:>5.2f}% | PF {pf:>4.2f} | 건당평균 {avg_ret:>+6.3f}% | 단순합산 누적 {tot_ret:>+7.1f}%")

    # ==================================================================================
    # Fast Backtest Engine for 3-Tier Regime Sizing
    # ==================================================================================
    initial_cap = 10_000_000.0

    def run_sim(cands_df, score_col, pnl_col, cap_super, cap_norm, cap_bear, max_slots=2):
        sub = cands_df.sort_values(['date_str', score_col], ascending=[True, False]).reset_index(drop=True)
        grouped = {d: g for d, g in sub.groupby('date_str')}

        equity = initial_cap
        curve = []
        tot_trades = 0
        trade_pnls = []
        yearly_pnl = {yr: 0.0 for yr in ['2020', '2021', '2022', '2023', '2024', '2025', '2026']}

        for d in all_trading_days:
            r_info = regime_map.get(d, {})
            is_super = r_info.get('is_super_bull', False)
            is_normal = r_info.get('is_normal_bull', False)

            if is_super:
                cap = cap_super
            elif is_normal:
                cap = cap_norm
            else:
                cap = cap_bear

            if d in grouped and max_slots > 0:
                picks = grouped[d].head(max_slots)
                k = len(picks)
                if k > 0:
                    tot_trades += k
                    yr = d[:4]
                    pct = min(cap, 1.0 / k)
                    day_pnl = 0.0
                    for pnl_val in picks[pnl_col]:
                        pnl_amt = initial_cap * pct * (pnl_val / 100.0)
                        day_pnl += pnl_amt
                        trade_pnls.append(pnl_val)
                        if yr in yearly_pnl:
                            yearly_pnl[yr] += pnl_amt
                    equity += day_pnl
            curve.append(equity)

        s = pd.Series(curve)
        peaks = s.cummax()
        dd = (s - peaks) / peaks * 100.0
        mdd = dd.min()
        acct_profit = equity - initial_cap
        acct_ret = acct_profit / initial_cap * 100.0

        pnl_s = pd.Series(trade_pnls)
        wr = (pnl_s > 0).mean() * 100.0 if len(pnl_s) > 0 else 0.0
        w_sum = pnl_s[pnl_s > 0].sum()
        l_sum = abs(pnl_s[pnl_s <= 0].sum())
        pf = w_sum / l_sum if l_sum > 0 else 999.0
        avg_trade = pnl_s.mean() if len(pnl_s) > 0 else 0.0

        daily_s = s.pct_change().dropna()
        sharpe = (daily_s.mean() / daily_s.std() * np.sqrt(252)) if daily_s.std() > 0 else 0.0
        losing_yrs = sum(1 for yr, pnl in yearly_pnl.items() if pnl < 0)

        return {
            'trades': tot_trades, 'wr': wr, 'pf': pf, 'avg_trade': avg_trade,
            'profit': acct_profit, 'ret': acct_ret, 'mdd': mdd, 'sharpe': sharpe,
            'losing_yrs': losing_yrs, 'yearly': yearly_pnl
        }

    # ==================================================================================
    # [TASK 3 & 4: 유니버스 후보군 설계 (780~850건 유지) x 3-Tier Grid x NXT TP]
    # ==================================================================================
    print("\n" + "=" * 120)
    print("🚀 [TASK 3 & 4: 780~850건 체결 유지 유니버스 x 3-Tier Regime Sizing x NXT TP 합성 탐색]")
    print("=" * 120)

    # Define Candidate Pools targeting 780~850 trades after lower_shadow filter:
    pools = {}

    # Universe A: TOP 10 (8~24%, sh<=11.5%) + TOP 22 (10~22.5%, sh<=09.5%), ls <= 0.20
    cond_u_a = (
        (((df['to_rank'] <= 10) & (df['change_pct'] >= 8.0) & (df['change_pct'] <= 24.0) & (df['upper_shadow'] <= 0.115)) |
         ((df['to_rank'] > 10) & (df['to_rank'] <= 22) & (df['change_pct'] >= 10.0) & (df['change_pct'] <= 22.5) & (df['upper_shadow'] <= 0.095))) &
        (df['lower_shadow'] <= 0.20)
    )
    pools['Univ_A_TOP22_ls20'] = df[cond_u_a]

    # Universe B: TOP 10 (8~24%, sh<=11.5%) + TOP 22 (10~22.5%, sh<=09.5%), ls <= 0.18
    cond_u_b = (
        (((df['to_rank'] <= 10) & (df['change_pct'] >= 8.0) & (df['change_pct'] <= 24.0) & (df['upper_shadow'] <= 0.115)) |
         ((df['to_rank'] > 10) & (df['to_rank'] <= 22) & (df['change_pct'] >= 10.0) & (df['change_pct'] <= 22.5) & (df['upper_shadow'] <= 0.095))) &
        (df['lower_shadow'] <= 0.18)
    )
    pools['Univ_B_TOP22_ls18'] = df[cond_u_b]

    # Universe C: TOP 10 (8~24%, sh<=12.0%) + TOP 24 (10~23.0%, sh<=10.0%), ls <= 0.20
    cond_u_c = (
        (((df['to_rank'] <= 10) & (df['change_pct'] >= 8.0) & (df['change_pct'] <= 24.0) & (df['upper_shadow'] <= 0.120)) |
         ((df['to_rank'] > 10) & (df['to_rank'] <= 24) & (df['change_pct'] >= 10.0) & (df['change_pct'] <= 23.0) & (df['upper_shadow'] <= 0.100))) &
        (df['lower_shadow'] <= 0.20)
    )
    pools['Univ_C_TOP24_ls20'] = df[cond_u_c]

    # Universe D: TOP 10 (8~24%, sh<=12.0%) + TOP 24 (10~23.0%, sh<=10.0%), ls <= 0.18
    cond_u_d = (
        (((df['to_rank'] <= 10) & (df['change_pct'] >= 8.0) & (df['change_pct'] <= 24.0) & (df['upper_shadow'] <= 0.120)) |
         ((df['to_rank'] > 10) & (df['to_rank'] <= 24) & (df['change_pct'] >= 10.0) & (df['change_pct'] <= 23.0) & (df['upper_shadow'] <= 0.100))) &
        (df['lower_shadow'] <= 0.18)
    )
    pools['Univ_D_TOP24_ls18'] = df[cond_u_d]

    # Universe E: TOP 12 (8~24%, sh<=11.5%) + TOP 25 (10~23.0%, sh<=09.5%), ls <= 0.20
    cond_u_e = (
        (((df['to_rank'] <= 12) & (df['change_pct'] >= 8.0) & (df['change_pct'] <= 24.0) & (df['upper_shadow'] <= 0.115)) |
         ((df['to_rank'] > 12) & (df['to_rank'] <= 25) & (df['change_pct'] >= 10.0) & (df['change_pct'] <= 23.0) & (df['upper_shadow'] <= 0.095))) &
        (df['lower_shadow'] <= 0.20)
    )
    pools['Univ_E_TOP25_ls20'] = df[cond_u_e]

    for p_k, p_df in pools.items():
        print(f"  • {p_k:<22}: {len(p_df):>5} 후보 풀")

    grid_results = []

    # Grid parameters
    super_caps = [0.70, 0.75, 0.80]
    norm_caps = [0.55, 0.60, 0.65]
    bear_caps = [0.35, 0.40, 0.45]
    pnl_modes = [
        ('pnl_open', '09:00 시초가'),
        ('pnl_nxt_25', 'NXT +2.5% TP'),
        ('pnl_nxt_30', 'NXT +3.0% TP'),
        ('pnl_nxt_35', 'NXT +3.5% TP')
    ]

    for p_name, p_df in pools.items():
        for score_col, score_name in [('score_baseline', 'Composite Baseline'), ('score_alpha', 'Alpha 4-Factor')]:
            for pnl_col, exit_name in pnl_modes:
                for s_cap in super_caps:
                    for n_cap in norm_caps:
                        for b_cap in bear_caps:
                            res = run_sim(p_df, score_col, pnl_col, s_cap, n_cap, b_cap, max_slots=2)
                            grid_results.append({
                                'univ': p_name,
                                'score': score_name,
                                'exit': exit_name,
                                'pnl_col': pnl_col,
                                'super_cap': s_cap,
                                'norm_cap': n_cap,
                                'bear_cap': b_cap,
                                **res
                            })

    rdf = pd.DataFrame(grid_results)
    print(f"\n총 {len(rdf):,}개 시뮬레이션 완료. (소요시간: {time.time()-t0:.2f}초)")

    # Save all results to CSV
    rdf.to_csv('research/quant_risk_audit_full_grid.csv', index=False, encoding='utf-8-sig')

    # Filter by user strict constraints:
    # 1. trades in 780~850 (or >= 780)
    # 2. acct_profit >= 18_000_000 (target 18M ~ 19.5M+)
    # 3. sharpe >= 1.30 (target 1.30 ~ 1.35+)
    # 4. mdd >= -10.0% (strictly < 10%, ideally -7% ~ -8% range)
    # 5. losing_yrs == 0
    print("\n" + "=" * 135)
    print("🏆 [사용자 5대 핵심 요구조건 완벽 충족 최상위 챔피언 매트릭스]")
    print("   (체결수 780~850건, 순수익 >= +1,800만, 샤프 >= 1.30, MDD > -10.0%, 7개년 무패 0개년)")
    print("=" * 135)

    champions = rdf[
        (rdf['trades'] >= 780) & (rdf['trades'] <= 850) &
        (rdf['profit'] >= 18_000_000) &
        (rdf['sharpe'] >= 1.30) &
        (rdf['mdd'] >= -10.0) &
        (rdf['losing_yrs'] == 0)
    ].sort_values(['sharpe', 'profit'], ascending=[False, False])

    print(f"엄격 조건 충족 챔피언 수: {len(champions)}개\n")

    if len(champions) > 0:
        print(f"{'순위':<3} | {'유니버스':<20} | {'청산 방식':<14} | {'3-Tier Cap(슈퍼/노말/약세)':<24} | {'체결수':<6} | {'승률':<5} | {'PF':<5} | {'계좌순익 (1천만)':<19} | {'MDD':<7} | {'샤프':<5}")
        print("-" * 135)
        for i, (_, r) in enumerate(champions.head(15).iterrows(), 1):
            cap_str = f"S:{int(r['super_cap']*100)}%/N:{int(r['norm_cap']*100)}%/B:{int(r['bear_cap']*100)}%"
            profit_str = f"+{r['profit']:,.0f}원 (+{r['ret']:.1f}%)"
            print(f"{i:<3} | {r['univ']:<20} | {r['exit']:<14} | {cap_str:<24} | {r['trades']:>4}건 | {r['wr']:>4.1f}% | {r['pf']:>4.2f} | {profit_str:<19} | {r['mdd']:>6.2f}% | {r['sharpe']:>4.2f}")
            print(f"    연도별: " + " | ".join([f"{yr}: {pnl/10000:+,.0f}만" for yr, pnl in sorted(r['yearly'].items())]))
    else:
        print("780~850건 범위에서 샤프 1.30+ 완벽 충족이 없을 경우, 상위 후보군 출력 (체결수>=760, MDD>-10%):")
        sub_top = rdf[(rdf['trades'] >= 760) & (rdf['mdd'] >= -10.0)].sort_values(['sharpe', 'profit'], ascending=[False, False])
        print(f"{'순위':<3} | {'유니버스':<20} | {'청산 방식':<14} | {'3-Tier Cap(슈퍼/노말/약세)':<24} | {'체결수':<6} | {'승률':<5} | {'PF':<5} | {'계좌순익 (1천만)':<19} | {'MDD':<7} | {'샤프':<5}")
        print("-" * 135)
        for i, (_, r) in enumerate(sub_top.head(15).iterrows(), 1):
            cap_str = f"S:{int(r['super_cap']*100)}%/N:{int(r['norm_cap']*100)}%/B:{int(r['bear_cap']*100)}%"
            profit_str = f"+{r['profit']:,.0f}원 (+{r['ret']:.1f}%)"
            print(f"{i:<3} | {r['univ']:<20} | {r['exit']:<14} | {cap_str:<24} | {r['trades']:>4}건 | {r['wr']:>4.1f}% | {r['pf']:>4.2f} | {profit_str:<19} | {r['mdd']:>6.2f}% | {r['sharpe']:>4.2f}")
            print(f"    연도별: " + " | ".join([f"{yr}: {pnl/10000:+,.0f}만" for yr, pnl in sorted(r['yearly'].items())]))

if __name__ == '__main__':
    run_quant_risk_audit()
