import duckdb
import pandas as pd
import numpy as np
import time

base_dir = 'D:/repo/korea-market_v3-anchor'
pool = pd.read_parquet(f'{base_dir}/research/round2_candidate_pool.parquet')
bars_path = f'{base_dir}/data/dataset_2y/daily_bars_all.parquet'
con = duckdb.connect()
all_trading_days = sorted(con.execute(f"SELECT DISTINCT strftime(date, '%Y-%m-%d') as d FROM '{bars_path}' WHERE date >= '2020-01-01'").df()['d'])

# Pre-calculate component scores
pool['s_rank'] = (20.0 - pool['to_rank'].clip(1, 20)) / 19.0
pool['s_shadow'] = (0.12 - pool['upper_shadow'].clip(0, 0.12)) / 0.12
pool['s_60d'] = (pool['ratio_60d'].clip(0.95, 1.10) - 0.95) / 0.15
pool['s_body'] = (pool['body_ret'].clip(6.0, 16.0) - 6.0) / 10.0
pool['s_mkt'] = (pool['to_mkt_share'].clip(0.6, 2.0) - 0.6) / 1.4

# Test several composite scoring formulas
# Score 0: Baseline (0.40 rank, 0.35 shadow, 0.25 60d)
pool['score_base'] = pool['s_rank'] * 0.40 + pool['s_shadow'] * 0.35 + pool['s_60d'] * 0.25

# Score 1: Add Body (0.30 rank, 0.30 shadow, 0.20 60d, 0.20 body)
pool['score_body'] = pool['s_rank'] * 0.30 + pool['s_shadow'] * 0.30 + pool['s_60d'] * 0.20 + pool['s_body'] * 0.20

# Score 2: Shadow & Body heavy (0.25 rank, 0.35 shadow, 0.20 60d, 0.20 body)
pool['score_heavy_shadow'] = pool['s_rank'] * 0.25 + pool['s_shadow'] * 0.35 + pool['s_60d'] * 0.20 + pool['s_body'] * 0.20

# Score 3: 5-Factor Balanced (0.25 rank, 0.25 shadow, 0.20 60d, 0.15 body, 0.15 mkt)
pool['score_5factor'] = pool['s_rank'] * 0.25 + pool['s_shadow'] * 0.25 + pool['s_60d'] * 0.20 + pool['s_body'] * 0.15 + pool['s_mkt'] * 0.15

# Fast Simulation function
initial_cap = 10_000_000.0

def run_sim(cond, score_col='score_base', max_slots=2, cap_pct=0.60):
    sub = pool[cond].copy()
    sub = sub.sort_values(['date_str', score_col], ascending=[True, False]).reset_index(drop=True)
    grouped = {d: g for d, g in sub.groupby('date_str')}
    
    equity = initial_cap
    curve = []
    tot_trades = 0
    trade_pnls = []
    yearly_pnl = {yr: 0.0 for yr in ['2020', '2021', '2022', '2023', '2024', '2025', '2026']}
    
    for d in all_trading_days:
        if d in grouped:
            picks = grouped[d].head(max_slots)
            k = len(picks)
            if k > 0:
                tot_trades += k
                yr = d[:4]
                pct = min(cap_pct, 1.0 / k)
                day_pnl = 0.0
                for _, r in picks.iterrows():
                    alloc = initial_cap * pct
                    pnl_val = r['pnl_open']
                    pnl_amt = alloc * (pnl_val / 100.0)
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
    acct_ret = (equity - initial_cap) / initial_cap * 100.0
    acct_profit = equity - initial_cap
    
    pnl_s = pd.Series(trade_pnls)
    wr = (pnl_s > 0).mean() * 100.0 if len(pnl_s) > 0 else 0.0
    w_sum = pnl_s[pnl_s > 0].sum()
    l_sum = abs(pnl_s[pnl_s <= 0].sum())
    pf = w_sum / l_sum if l_sum > 0 else 999.0
    
    daily_s = s.pct_change().dropna()
    sharpe = (daily_s.mean() / daily_s.std() * np.sqrt(252)) if daily_s.std() > 0 else 0.0
    losing_yrs = sum(1 for yr, pnl in yearly_pnl.items() if pnl < 0)
    
    return {
        'trades': tot_trades, 'wr': wr, 'pf': pf, 'avg': pnl_s.mean(),
        'profit': acct_profit, 'ret': acct_ret, 'mdd': mdd, 'sharpe': sharpe,
        'losing_yrs': losing_yrs, 'yearly': yearly_pnl
    }

# Grid Search over universes and parameters
universes = []

# U0: Baseline Universe
u0_cond = ((pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.12)) | \
          ((pool['to_rank'] > 10) & (pool['to_rank'] <= 20) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10))
universes.append(("U0. Baseline", u0_cond))

# U1: Add Body Ret >= 7.0%
u1_cond = u0_cond & (pool['body_ret'] >= 7.0)
universes.append(("U1. Body >= 7.0%", u1_cond))

# U2: Add Body Ret >= 8.0%
u2_cond = u0_cond & (pool['body_ret'] >= 8.0)
universes.append(("U2. Body >= 8.0%", u2_cond))

# U3: Expanded TOP 10 (8~24%, shadow <= 0.12, body >= 7%) + TOP 11~22 (10~22%, shadow <= 0.10, body >= 7%)
u3_cond = ((pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 24.0) & (pool['upper_shadow'] <= 0.12) & (pool['body_ret'] >= 7.0)) | \
          ((pool['to_rank'] > 10) & (pool['to_rank'] <= 22) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10) & (pool['body_ret'] >= 7.0))
universes.append(("U3. ExpTOP10(24%)+TOP22 & Body>=7%", u3_cond))

# U4: Hybrid: TOP 10 unconditional (8~23%, shadow<=0.12, body>=7%) + TOP 11~25 (10~22%, shadow<=0.10, body>=7% & (mkt>=1.0% or 60d>=0.95))
u4_cond = ((pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 23.0) & (pool['upper_shadow'] <= 0.12) & (pool['body_ret'] >= 7.0)) | \
          ((pool['to_rank'] > 10) & (pool['to_rank'] <= 25) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10) & (pool['body_ret'] >= 7.0) & ((pool['to_mkt_share'] >= 0.9) | (pool['ratio_60d'] >= 0.95)))
universes.append(("U4. Hybrid TOP10 + TOP11~25(Mkt>=0.9|60d>=0.95)", u4_cond))

# U5: Hybrid with Shadow Cut <= 0.10 for TOP 10, <= 0.08 for TOP 11~25
u5_cond = ((pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 24.0) & (pool['upper_shadow'] <= 0.10) & (pool['body_ret'] >= 7.0)) | \
          ((pool['to_rank'] > 10) & (pool['to_rank'] <= 25) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.08) & (pool['body_ret'] >= 7.0) & ((pool['to_mkt_share'] >= 0.8) | (pool['ratio_60d'] >= 0.95)))
universes.append(("U5. Hybrid TightShadow(10%/8%)", u5_cond))

# U6: Hybrid with TOP 10 (shadow<=0.11, 8~24%, body>=7.5%) + TOP 11~25 (shadow<=0.09, 10~22%, body>=7.5% & (mkt>=0.85|60d>=0.95))
u6_cond = ((pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 24.0) & (pool['upper_shadow'] <= 0.11) & (pool['body_ret'] >= 7.5)) | \
          ((pool['to_rank'] > 10) & (pool['to_rank'] <= 25) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.09) & (pool['body_ret'] >= 7.5) & ((pool['to_mkt_share'] >= 0.85) | (pool['ratio_60d'] >= 0.95)))
universes.append(("U6. Hybrid Balanced(11%/9%, body>=7.5%)", u6_cond))

# U7: Hybrid with TOP 10 (shadow<=0.12, 8~25%, body>=6.5%) + TOP 11~25 (shadow<=0.10, 10~22%, body>=6.5% & (mkt>=0.80|60d>=0.95))
u7_cond = ((pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 25.0) & (pool['upper_shadow'] <= 0.12) & (pool['body_ret'] >= 6.5)) | \
          ((pool['to_rank'] > 10) & (pool['to_rank'] <= 25) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10) & (pool['body_ret'] >= 6.5) & ((pool['to_mkt_share'] >= 0.80) | (pool['ratio_60d'] >= 0.95)))
universes.append(("U7. Hybrid Robust(12%/10%, body>=6.5%)", u7_cond))

print("\n" + "=" * 110)
print(f"{'유니버스/전략':<42} | {'스코어':<12} | {'Cap':<5} | {'체결수':<6} | {'승률':<6} | {'PF':<5} | {'건당평균':<8} | {'계좌수익':<12} | {'MDD':<7} | {'샤프':<5}")
print("-" * 110)

scores = ['score_base', 'score_body', 'score_heavy_shadow', 'score_5factor']
caps = [0.50, 0.55, 0.60]

results = []
for u_name, u_cond in universes:
    for s_name in scores:
        for cap in caps:
            res = run_sim(u_cond, score_col=s_name, max_slots=2, cap_pct=cap)
            results.append({
                'universe': u_name, 'score': s_name, 'cap': cap, **res
            })
            # Highlight target matches
            is_target = (res['trades'] >= 760) and (res['profit'] >= 17_000_000) and (res['mdd'] >= -10.0) and (res['sharpe'] >= 1.20)
            flag = " 🎯 [TARGET HIT!]" if is_target else ""
            if res['trades'] >= 700:
                print(f"{u_name:<42} | {s_name:<12} | {int(cap*100)}%  | {res['trades']:>5}건 | {res['wr']:>5.1f}% | {res['pf']:>4.2f} | {res['avg']:>+7.3f}% | +{res['profit']/10000:>6.0f}만(+{res['ret']:>5.1f}%) | {res['mdd']:>6.2f}% | {res['sharpe']:>4.2f}{flag}")

df_res = pd.DataFrame(results)
df_res.to_csv(f'{base_dir}/research/round2_grid_results.csv', index=False, encoding='utf-8-sig')
print("\n전체 그리드 결과 CSV 저장 완료: research/round2_grid_results.csv")
