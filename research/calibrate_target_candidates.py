import duckdb
import pandas as pd
import numpy as np

base_dir = 'D:/repo/korea-market_v3-anchor'
pool = pd.read_parquet(f'{base_dir}/research/round2_candidate_pool.parquet')
bars_path = f'{base_dir}/data/dataset_2y/daily_bars_all.parquet'
con = duckdb.connect()
all_trading_days = sorted(con.execute(f"SELECT DISTINCT strftime(date, '%Y-%m-%d') as d FROM '{bars_path}' WHERE date >= '2020-01-01'").df()['d'])

# Normalization for scoring:
pool['s_rank'] = (28.0 - pool['to_rank'].clip(1, 28)) / 27.0
pool['s_shadow'] = (0.12 - pool['upper_shadow'].clip(0, 0.12)) / 0.12
pool['s_60d'] = (pool['ratio_60d'].clip(0.95, 1.10) - 0.95) / 0.15
pool['s_body'] = (pool['body_ret'].clip(4.5, 15.0) - 4.5) / 10.5

# Composite scoring formulas:
pool['score_user'] = pool['s_rank'] * 0.25 + pool['s_shadow'] * 0.35 + pool['s_body'] * 0.20 + pool['s_60d'] * 0.20
pool['score_shadow40'] = pool['s_rank'] * 0.20 + pool['s_shadow'] * 0.40 + pool['s_body'] * 0.20 + pool['s_60d'] * 0.20
pool['score_body25'] = pool['s_rank'] * 0.20 + pool['s_shadow'] * 0.35 + pool['s_body'] * 0.25 + pool['s_60d'] * 0.20
pool['score_base'] = pool['s_rank'] * 0.40 + pool['s_shadow'] * 0.35 + pool['s_60d'] * 0.25

initial_cap = 10_000_000.0

def eval_sim(sub_df, score_col='score_user', max_slots=2, cap_pct=0.55):
    sub = sub_df.sort_values(['date_str', score_col], ascending=[True, False]).reset_index(drop=True)
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
        'candidates': len(sub_df),
        'trades': tot_trades, 'wr': wr, 'pf': pf, 'avg': pnl_s.mean(),
        'profit': acct_profit, 'ret': acct_ret, 'mdd': mdd, 'sharpe': sharpe,
        'losing_yrs': losing_yrs, 'yearly': yearly_pnl
    }

print("Searching parameter grid where candidates = 870 ~ 1000 and trades = 780 ~ 850...")

grid_results = []

# T1: rank 1~10, change_pct 8.0 ~ t1_chg_max, upper_shadow <= t1_sh, body_ret >= b_min
# T2: rank 11~20, change_pct 10.0 ~ 22.0, upper_shadow <= t2_sh, body_ret >= b_min
# T3: rank 21~max_rank, change_pct 10.0 or 12.0 ~ 22.0, upper_shadow <= t3_sh, ratio_60d >= t3_r60, body_ret >= b_min

for max_r in [25, 28]:
    for b_min in [4.5, 4.8, 5.0]:
        for t1_chg_max in [23.0, 24.0, 25.0]:
            for t1_sh in [0.11, 0.115, 0.12]:
                for t2_sh in [0.09, 0.095, 0.10]:
                    for t3_sh in [0.05, 0.06, 0.07]:
                        for t3_chg_min in [10.0, 12.0]:
                            for t3_r60 in [0.98, 1.00]:
                                c1 = (pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= t1_chg_max) & (pool['upper_shadow'] <= t1_sh) & (pool['body_ret'] >= b_min)
                                c2 = (pool['to_rank'] > 10) & (pool['to_rank'] <= 20) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= t2_sh) & (pool['body_ret'] >= b_min)
                                c3 = (pool['to_rank'] > 20) & (pool['to_rank'] <= max_r) & (pool['change_pct'] >= t3_chg_min) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= t3_sh) & (pool['ratio_60d'] >= t3_r60) & (pool['body_ret'] >= b_min)
                                cond = c1 | c2 | c3
                                n_cands = cond.sum()
                                if 860 <= n_cands <= 980:
                                    sub_df = pool[cond]
                                    for sc in ['score_user', 'score_shadow40', 'score_body25', 'score_base']:
                                        for cap in [0.50, 0.55, 0.60]:
                                            res = eval_sim(sub_df, score_col=sc, max_slots=2, cap_pct=cap)
                                            if res and 780 <= res['trades'] <= 850 and res['mdd'] >= -10.0:
                                                grid_results.append({
                                                    'max_r': max_r, 'b_min': b_min, 't1_chg': t1_chg_max,
                                                    't1_sh': t1_sh, 't2_sh': t2_sh, 't3_sh': t3_sh,
                                                    't3_chg': t3_chg_min, 't3_r60': t3_r60,
                                                    'score': sc, 'cap': cap, **res
                                                })

df_res = pd.DataFrame(grid_results)
print(f"Total matching configurations (Trades 780~850 & MDD > -10.0%): {len(df_res)}")

if len(df_res) > 0:
    df_sorted = df_res.sort_values(['profit', 'sharpe'], ascending=[False, False])
    print("\n" + "=" * 135)
    print(f"{'설정 요약':<38} | {'Score':<15} | {'Cap':<5} | {'Cands':<5} | {'Trades':<6} | {'WR':<6} | {'PF':<5} | {'건당':<7} | {'순수익 (1천만)':<16} | {'MDD':<7} | {'Sharpe':<5}")
    print("-" * 135)
    for _, r in df_sorted.head(30).iterrows():
        name_str = f"R<={r['max_r']} b>={r['b_min']} T1<{int(r['t1_chg'])}% s({r['t1_sh']}/{r['t2_sh']}/{r['t3_sh']})"
        hit = " 🎯 [TARGET HIT!]" if (r['trades'] >= 780 and r['profit'] >= 18_000_000 and r['mdd'] >= -9.0 and r['sharpe'] >= 1.25) else ""
        print(f"{name_str:<38} | {r['score']:<15} | {int(r['cap']*100)}%  | {r['candidates']:>5} | {r['trades']:>5}건 | {r['wr']:>5.1f}% | {r['pf']:>4.2f} | {r['avg']:>+6.3f}% | +{r['profit']/10000:>4.0f}만(+{r['ret']:>5.1f}%) | {r['mdd']:>6.2f}% | {r['sharpe']:>4.2f}{hit}")

    df_res.to_csv(f'{base_dir}/research/grid_780_850_trades_results.csv', index=False)
