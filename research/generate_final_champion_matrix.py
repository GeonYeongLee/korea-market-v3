import duckdb
import pandas as pd
import numpy as np

print("=" * 105)
print("🏆 [MASTER CHAMPION COMPARISON MATRIX: 2020~2026 전수 성과 비교]")
print("=" * 105)

con = duckdb.connect()
bars_path = 'd:/repo/korea-market_v3-anchor/data/dataset_2y/daily_bars_all.parquet'
idx_path = 'd:/repo/korea-market_v3-anchor/data/dataset_2y/index_daily_full.parquet'
nxt_path = 'd:/repo/korea-market_v3-anchor/research/nxt_trades_merged.parquet'

# 1. NXT TP Map
nxt_df = pd.read_parquet(nxt_path)
nxt_valid = nxt_df[nxt_df['pm_close'].notna()].copy()
nxt_tp_map = {}
for (sym, nd), group in nxt_valid.groupby(['symbol', 'exit_date']):
    entry_p = group.iloc[0]['entry_price']
    first_open = group.iloc[0]['pm_open']
    max_high = group['pm_high'].max()
    target_tp = entry_p * 1.030
    if max_high >= target_tp:
        nxt_tp_map[(sym, nd)] = max(first_open, target_tp)

# 2. Extract Master Candidate Pool
query = f"""
WITH base AS (
    SELECT 
        symbol,
        date,
        strftime(date, '%Y-%m-%d') as date_str,
        open, high, low, close, turnover, change_pct,
        CASE WHEN (high - low) > 0 
             THEN (high - greatest(open, close)) / (high - low) 
             ELSE 0.0 END as upper_shadow,
        ROW_NUMBER() OVER (PARTITION BY date ORDER BY turnover DESC) as to_rank,
        MAX(high) OVER (
            PARTITION BY symbol 
            ORDER BY date 
            ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING
        ) as prev_60d_high,
        LEAD(open) OVER (PARTITION BY symbol ORDER BY date) as next_open,
        LEAD(date) OVER (PARTITION BY symbol ORDER BY date) as next_date
    FROM '{bars_path}'
    WHERE date >= '2019-09-01'
)
SELECT 
    symbol, date_str, to_rank, change_pct, upper_shadow, close, turnover,
    prev_60d_high,
    CASE WHEN prev_60d_high > 0 THEN (close / prev_60d_high) ELSE 1.0 END as ratio_60d,
    next_open, next_date
FROM base
WHERE date_str >= '2020-01-01'
  AND to_rank <= 35
  AND change_pct >= 7.0 AND change_pct <= 27.0
  AND close > open
  AND next_open IS NOT NULL
  AND date_diff('day', strptime(date_str, '%Y-%m-%d'), next_date) <= 5
ORDER BY date_str, to_rank
"""
pool = con.execute(query).df()

# Add net return with 0.28% fee
pool['gap_ret'] = (pool['next_open'] - pool['close']) / pool['close'] * 100.0 - 0.28
nxt_rets = []
for _, r in pool.iterrows():
    sym = r['symbol']
    nd_str = pd.to_datetime(r['next_date']).strftime('%Y-%m-%d')
    key = (sym, nd_str)
    if key in nxt_tp_map:
        exit_p = nxt_tp_map[key]
        pnl = ((exit_p - r['close']) / r['close'] * 100.0) - 0.28
    else:
        pnl = r['gap_ret']
    nxt_rets.append(pnl)
pool['pnl'] = nxt_rets
pool['yr'] = pool['date_str'].str[:4]

all_trading_days = sorted(con.execute(f"SELECT DISTINCT strftime(date, '%Y-%m-%d') as d FROM '{bars_path}' WHERE date >= '2020-01-01'").df()['d'])

# Simulation function
def run_sim(cond, max_slots=5, cap_pct=0.50, sort_by='change_pct'):
    sub = pool[cond].copy()
    if sort_by == 'change_pct':
        sub = sub.sort_values(['date_str', 'change_pct'], ascending=[True, False]).reset_index(drop=True)
    elif sort_by == 'to_rank':
        sub = sub.sort_values(['date_str', 'to_rank'], ascending=[True, True]).reset_index(drop=True)
    
    grouped = {d: g for d, g in sub.groupby('date_str')}
    initial_cap = 10_000_000.0
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
                pct = min(cap_pct, 1.0 / k)
                alloc = initial_cap * pct
                day_pnl = 0.0
                yr = d[:4]
                for pnl_val in picks['pnl']:
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
    avg_pnl = pnl_s.mean() if len(pnl_s) > 0 else 0.0
    
    daily_s = s.pct_change().dropna()
    sharpe = (daily_s.mean() / daily_s.std() * np.sqrt(252)) if daily_s.std() > 0 else 0.0
    
    losing_yrs = sum(1 for yr, pnl in yearly_pnl.items() if pnl < 0)
    
    return {
        'trades': tot_trades,
        'wr': wr,
        'pf': pf,
        'avg_pnl': avg_pnl,
        'acct_profit': acct_profit,
        'acct_ret': acct_ret,
        'mdd': mdd,
        'sharpe': sharpe,
        'losing_yrs': losing_yrs,
        'yearly': yearly_pnl
    }

# Strategies to evaluate
strategies = [
    (
        "기준: 구버전 (고정 2000억, 10~20%, 꼬리25%, 고정 20% 슬롯)",
        (pool['turnover'] >= 2000e8) & (pool['change_pct'] >= 10) & (pool['change_pct'] <= 20) & (pool['upper_shadow'] <= 0.25),
        0.20, 'change_pct'
    ),
    (
        "전략 1 [수익 극대화형 챔피언]: TOP 20 + 10~22% + 꼬리 10% (50% Cap)",
        (pool['to_rank'] <= 20) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10),
        0.50, 'change_pct'
    ),
    (
        "전략 2 [초안정 방어 + 최고 샤프]: TOP 20 + 10~22% + 꼬리 10% + 60일 신고가(>=0.95)",
        (pool['to_rank'] <= 20) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10) & (pool['ratio_60d'] >= 0.95),
        0.50, 'change_pct'
    ),
    (
        "전략 3 [황금 밸런스 계층형 (840건)]: TOP 10(꼬리12%/8~22%) + TOP 20(꼬리10%/10~22%) (50% Cap)",
        ((pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.12)) | \
        ((pool['to_rank'] > 10) & (pool['to_rank'] <= 20) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10)),
        0.50, 'change_pct'
    ),
    (
        "전략 3-B [황금 밸런스 계층형 (840건, 40% Cap)]: TOP 10(꼬리12%) + TOP 20(꼬리10%)",
        ((pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.12)) | \
        ((pool['to_rank'] > 10) & (pool['to_rank'] <= 20) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10)),
        0.40, 'change_pct'
    ),
    (
        "전략 4 [최다 체결 1000+건형]: TOP 15(꼬리12%/8~22%) + TOP 25(꼬리10%/10~22%) (50% Cap)",
        ((pool['to_rank'] <= 15) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.12)) | \
        ((pool['to_rank'] > 15) & (pool['to_rank'] <= 25) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10)),
        0.50, 'change_pct'
    ),
    (
        "전략 4-B [최다 체결 1000+건형 (40% Cap)]: TOP 15(꼬리12%) + TOP 25(꼬리10%)",
        ((pool['to_rank'] <= 15) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.12)) | \
        ((pool['to_rank'] > 15) & (pool['to_rank'] <= 25) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10)),
        0.40, 'change_pct'
    ),
]

results = []
for name, cond, cap, sort_col in strategies:
    res = run_sim(cond, max_slots=5, cap_pct=cap, sort_by=sort_col)
    res['name'] = name
    results.append(res)
    print(f"[{name}]")
    print(f"  • 체결수: {res['trades']}건 (연평균 {res['trades']/7:.1f}회) | 승률: {res['wr']:.1f}% | PF: {res['pf']:.2f} | Sharpe: {res['sharpe']:.2f}")
    print(f"  • 계좌수익: +{res['acct_profit']:,.0f}원 ({res['acct_ret']:+.1f}%) | MDD: {res['mdd']:.2f}% | 마이너스 연도: {res['losing_yrs']}개년")
    print(f"  • 연도별: " + " | ".join([f"{yr}: {pnl/10000:+,.0f}만" for yr, pnl in sorted(res['yearly'].items())]))
    print()

df_res = pd.DataFrame([{
    '전략명': r['name'],
    '체결수': r['trades'],
    '승률': f"{r['wr']:.1f}%",
    '손익비(PF)': f"{r['pf']:.2f}",
    '샤프지수': f"{r['sharpe']:.2f}",
    '계좌수익': f"+{r['acct_profit']:,.0f}원 ({r['acct_ret']:+.1f}%)",
    'MDD': f"{r['mdd']:.2f}%",
    '마이너스연도': f"{r['losing_yrs']}개년",
} for r in results])
df_res.to_csv('d:/repo/korea-market_v3-anchor/research/master_champion_matrix.csv', index=False, encoding='utf-8-sig')
print("Master matrix saved to research/master_champion_matrix.csv")
