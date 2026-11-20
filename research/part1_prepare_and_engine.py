import os
import sys
import time
import duckdb
import pandas as pd
import numpy as np

print("=" * 105)
print("🔬 [독립 퀀트 리서치] 2020~2026 한국 시장 앵커 종가배팅 전략 고도화 전수 백테스트")
print("=" * 105)

base_dir = 'D:/repo/korea-market_v3-anchor'
bars_path = f'{base_dir}/data/dataset_2y/daily_bars_all.parquet'
idx_path = f'{base_dir}/data/dataset_2y/index_daily_full.parquet'
nxt_path = f'{base_dir}/research/nxt_trades_merged.parquet'

t0 = time.time()
con = duckdb.connect()

# ---------------------------------------------------------
# 1. Load Market Index & NXT Data
# ---------------------------------------------------------
print("\n[STEP 1] 시장 지수 및 NXT 프리마켓 데이터 로딩")
idx_df = pd.read_parquet(idx_path)
idx_df['date_str'] = pd.to_datetime(idx_df['date']).dt.strftime('%Y-%m-%d')
idx_map = {r['date_str']: r for _, r in idx_df.iterrows()}

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

print(f"  • NXT 프리마켓 3% 익절 매핑 데이터: {len(nxt_tp_map):,}건 로드 완료")

# ---------------------------------------------------------
# 2. Extract Candidate Universe from Daily Bars (Zero Lookahead)
# ---------------------------------------------------------
print("\n[STEP 2] 2020~2026년 일봉 데이터(1,180만 행)에서 후보 풀 및 60일 신고가 추출 중...")
extract_query = f"""
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
        -- 60-day rolling high strictly BEFORE today (shift 1, 0% lookahead)
        MAX(high) OVER (
            PARTITION BY symbol 
            ORDER BY date 
            ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING
        ) as prev_60d_high,
        COUNT(high) OVER (
            PARTITION BY symbol 
            ORDER BY date 
            ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING
        ) as cnt_history,
        -- Next Day prices
        LEAD(open) OVER (PARTITION BY symbol ORDER BY date) as next_open,
        LEAD(high) OVER (PARTITION BY symbol ORDER BY date) as next_high,
        LEAD(low) OVER (PARTITION BY symbol ORDER BY date) as next_low,
        LEAD(close) OVER (PARTITION BY symbol ORDER BY date) as next_close,
        LEAD(date) OVER (PARTITION BY symbol ORDER BY date) as next_date
    FROM '{bars_path}'
    WHERE date >= '2019-09-01'
)
SELECT 
    symbol,
    date_str,
    to_rank,
    open, high, low, close, turnover, change_pct,
    upper_shadow,
    prev_60d_high,
    cnt_history,
    CASE WHEN prev_60d_high > 0 THEN (close / prev_60d_high) ELSE 0.0 END as ratio_60d,
    next_open, next_high, next_low, next_close,
    strftime(next_date, '%Y-%m-%d') as next_date_str
FROM base
WHERE date_str >= '2020-01-01'
  AND to_rank <= 50
  AND change_pct >= 6.0 AND change_pct <= 28.0
  AND close > open
  AND next_open IS NOT NULL
  AND date_diff('day', strptime(date_str, '%Y-%m-%d'), next_date) <= 5
ORDER BY date_str, change_pct DESC
"""
pool = con.execute(extract_query).df()
print(f"  • 후보 풀 추출 완료: {len(pool):,}건 (소요 시간: {time.time()-t0:.2f}초)")

# Add Base PnL (0.28% Fee)
pool['yr'] = pool['date_str'].str[:4]
pool['gap_pct'] = (pool['next_open'] - pool['close']) / pool['close'] * 100.0
pool['gap_ret'] = pool['gap_pct'] - 0.28
pool['next_high_pct'] = (pool['next_high'] - pool['close']) / pool['close'] * 100.0
pool['next_low_pct'] = (pool['next_low'] - pool['close']) / pool['close'] * 100.0
pool['next_close_pct'] = (pool['next_close'] - pool['close']) / pool['close'] * 100.0

# NXT PnL Mapping
nxt_rets = []
for _, r in pool.iterrows():
    sym = r['symbol']
    key = (sym, r['next_date_str'])
    if key in nxt_tp_map:
        exit_p = nxt_tp_map[key]
        pnl = ((exit_p - r['close']) / r['close'] * 100.0) - 0.28
    else:
        pnl = r['gap_ret']
    nxt_rets.append(pnl)
pool['nxt_gap_ret'] = nxt_rets

all_trading_days = sorted(con.execute(f"SELECT DISTINCT strftime(date, '%Y-%m-%d') as d FROM '{bars_path}' WHERE date >= '2020-01-01'").df()['d'])

# ---------------------------------------------------------
# Portfolio Simulation Helper Function
# ---------------------------------------------------------
def simulate_portfolio(df_trades, max_slots=5, initial_cap=10_000_000.0, pnl_col='nxt_gap_ret'):
    if len(df_trades) == 0:
        return {
            'n': 0, 'wr': 0, 'pf': 0, 'avg': 0, 'tot': 0, 'acct_ret': 0, 
            'mdd': 0, 'sharpe': 0, 'yearly': {}, 'eq_df': pd.DataFrame()
        }
    
    # Sort and take top slots per day
    df_trades = df_trades.sort_values(['date_str', 'change_pct'], ascending=[True, False]).reset_index(drop=True)
    df_trades['rank'] = df_trades.groupby('date_str').cumcount() + 1
    t = df_trades[df_trades['rank'] <= max_slots].copy()
    
    n_tot = len(t)
    wr = (t[pnl_col] > 0).mean() * 100.0
    w_sum = t[t[pnl_col] > 0][pnl_col].sum()
    l_sum = abs(t[t[pnl_col] <= 0][pnl_col].sum())
    pf = w_sum / l_sum if l_sum > 0 else 999.0
    avg_ret = t[pnl_col].mean()
    tot_ret = t[pnl_col].sum()
    
    slot_cap = initial_cap / max_slots
    equity = initial_cap
    daily_equities = []
    trades_by_date = {d: g for d, g in t.groupby('date_str')}
    
    for d in all_trading_days:
        if d in trades_by_date:
            d_trades = trades_by_date[d]
            day_pnl = sum([slot_cap * (r[pnl_col] / 100.0) for _, r in d_trades.iterrows()])
            equity += day_pnl
        daily_equities.append({'date': d, 'equity': equity})
        
    eq_df = pd.DataFrame(daily_equities)
    eq_df['peak'] = eq_df['equity'].cummax()
    eq_df['dd'] = (eq_df['equity'] - eq_df['peak']) / eq_df['peak'] * 100.0
    mdd = eq_df['dd'].min()
    acct_ret = (equity / initial_cap - 1) * 100.0
    
    eq_df['daily_ret'] = eq_df['equity'].pct_change().fillna(0)
    sharpe = (eq_df['daily_ret'].mean() / eq_df['daily_ret'].std()) * np.sqrt(250) if eq_df['daily_ret'].std() > 0 else 0
    
    yearly = {}
    for yr in ['2020', '2021', '2022', '2023', '2024', '2025', '2026']:
        gy = t[t['yr'] == yr]
        ny = len(gy)
        if ny > 0:
            wry = (gy[pnl_col] > 0).mean() * 100.0
            wy = gy[gy[pnl_col] > 0][pnl_col].sum()
            ly = abs(gy[gy[pnl_col] <= 0][pnl_col].sum())
            pfy = wy / ly if ly > 0 else 999.0
            avgy = gy[pnl_col].mean()
            toty = gy[pnl_col].sum()
            yr_dates = eq_df[eq_df['date'].str.startswith(yr)]
            yr_start = yr_dates.iloc[0]['equity']
            yr_end = yr_dates.iloc[-1]['equity']
            yr_acct_ret = (yr_end - yr_start) / initial_cap * 100.0
            yearly[yr] = {'n': ny, 'wr': wry, 'pf': pfy, 'avg': avgy, 'tot': toty, 'acct_ret': yr_acct_ret}
        else:
            yearly[yr] = {'n': 0, 'wr': 0, 'pf': 0, 'avg': 0, 'tot': 0, 'acct_ret': 0}
            
    return {
        'n': n_tot, 'wr': wr, 'pf': pf, 'avg': avg_ret, 'tot': tot_ret, 
        'acct_ret': acct_ret, 'mdd': mdd, 'sharpe': sharpe, 'yearly': yearly, 
        'trades': t, 'eq_df': eq_df
    }

def print_result_block(title, res):
    print("\n" + "=" * 105)
    print(f"📊 [{title}]")
    print("=" * 105)
    print(f"• 총 체결 횟수         : {res['n']:,} 회 (연평균 {res['n']/7:.1f}회)")
    print(f"• 승률 (Win Rate)      : {res['wr']:.2f}%")
    print(f"• 손익비 (Profit Factor): {res['pf']:.2f}")
    print(f"• 건당 평균 순손익     : {res['avg']:+.3f}% (수수료 0.28% 전액 차감 후)")
    print(f"• 단순 합산 누적수익   : {res['tot']:+.2f}%")
    print(f"• 계좌 순수익률 (1천만): {res['acct_ret']:+.2f}%")
    print(f"• 계좌 최대 낙폭 (MDD) : {res['mdd']:.2f}%")
    print(f"• 샤프 지수 (Sharpe)   : {res['sharpe']:.2f}")
    print("-" * 105)
    print(f"{'연도':<6} | {'체결수':<8} | {'승률':<8} | {'손익비(PF)':<10} | {'건당평균':<10} | {'연간단순합':<12} | {'연간계좌수익':<12}")
    print("-" * 105)
    for yr, y in res['yearly'].items():
        print(f"{yr:<6} | {y['n']:>6}건 | {y['wr']:>6.1f}% | {y['pf']:>8.2f}   | {y['avg']:>+8.3f}% | {y['tot']:>+10.2f}% | {y['acct_ret']:>+10.2f}%")
    print("=" * 105)

# Save pool for next parts
pool.to_parquet(f'{base_dir}/research/quant_research_pool_temp.parquet')
print("임시 데이터풀 저장 완료.")
