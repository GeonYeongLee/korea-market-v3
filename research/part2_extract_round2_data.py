import os
import sys
import time
import duckdb
import pandas as pd
import numpy as np

print("=" * 105)
print("🔬 [독립 퀀트 리서치 2차: 단리 순수익 극대화 & 샤프 1.25+ & MDD < 10% 방어]")
print("=" * 105)

base_dir = 'D:/repo/korea-market_v3-anchor'
bars_path = f'{base_dir}/data/dataset_2y/daily_bars_all.parquet'
nxt_path = f'{base_dir}/research/nxt_trades_merged.parquet'

con = duckdb.connect()
t0 = time.time()

# 1. Load NXT Data
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

print(f"1. NXT 3% 익절 맵 로드 완료: {len(nxt_tp_map):,}건")

# 2. Extract Full Data with Open, Market Turnover, 60d High
print("2. DuckDB 일봉 전수 추출 (시가, 당일 몸통상승률, 시장비율, 60일 신고가)...")
extract_q = f"""
WITH mkt AS (
    SELECT date, sum(turnover) as mkt_turnover
    FROM '{bars_path}'
    WHERE date >= '2019-09-01'
    GROUP BY date
),
base AS (
    SELECT 
        b.symbol,
        b.date,
        strftime(b.date, '%Y-%m-%d') as date_str,
        b.open, b.high, b.low, b.close, b.turnover, b.change_pct,
        (b.close - b.open) / b.open * 100.0 as body_ret,
        CASE WHEN (b.high - b.low) > 0 
             THEN (b.high - greatest(b.open, b.close)) / (b.high - b.low) 
             ELSE 0.0 END as upper_shadow,
        ROW_NUMBER() OVER (PARTITION BY b.date ORDER BY b.turnover DESC) as to_rank,
        b.turnover / m.mkt_turnover * 100.0 as to_mkt_share,
        MAX(b.high) OVER (
            PARTITION BY b.symbol 
            ORDER BY b.date 
            ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING
        ) as prev_60d_high,
        LEAD(b.open) OVER (PARTITION BY b.symbol ORDER BY b.date) as next_open,
        LEAD(b.high) OVER (PARTITION BY b.symbol ORDER BY b.date) as next_high,
        LEAD(b.low) OVER (PARTITION BY b.symbol ORDER BY b.date) as next_low,
        LEAD(b.close) OVER (PARTITION BY b.symbol ORDER BY b.date) as next_close,
        LEAD(b.date) OVER (PARTITION BY b.symbol ORDER BY b.date) as next_date
    FROM '{bars_path}' b
    JOIN mkt m ON b.date = m.date
    WHERE b.date >= '2019-09-01'
)
SELECT 
    symbol, date_str, open, high, low, close, turnover, change_pct, body_ret,
    upper_shadow, to_rank, to_mkt_share, prev_60d_high,
    CASE WHEN prev_60d_high > 0 THEN (close / prev_60d_high) ELSE 1.0 END as ratio_60d,
    next_open, next_high, next_low, next_close,
    strftime(next_date, '%Y-%m-%d') as next_date_str
FROM base
WHERE date_str >= '2020-01-01'
  AND to_rank <= 30
  AND change_pct >= 7.0 AND change_pct <= 26.0
  AND close > open
  AND next_open IS NOT NULL
  AND date_diff('day', strptime(date_str, '%Y-%m-%d'), next_date) <= 5
ORDER BY date_str, to_rank
"""
pool = con.execute(extract_q).df()
print(f"후보 풀 추출 완료: {len(pool):,}건 (소요 시간: {time.time()-t0:.2f}초)")

# Calculate PnL (08:00 NXT TP + 09:00 Open, 0.28% Fee)
gap_rets = (pool['next_open'] - pool['close']) / pool['close'] * 100.0 - 0.28
pool['gap_ret'] = gap_rets

nxt_rets = []
for _, r in pool.iterrows():
    key = (r['symbol'], r['next_date_str'])
    if key in nxt_tp_map:
        exit_p = nxt_tp_map[key]
        pnl = ((exit_p - r['close']) / r['close'] * 100.0) - 0.28
    else:
        pnl = r['gap_ret']
    nxt_rets.append(pnl)
pool['pnl_open'] = nxt_rets
pool['yr'] = pool['date_str'].str[:4]

all_trading_days = sorted(con.execute(f"SELECT DISTINCT strftime(date, '%Y-%m-%d') as d FROM '{bars_path}' WHERE date >= '2020-01-01'").df()['d'])

# Save extracted clean pool
pool.to_parquet(f'{base_dir}/research/round2_candidate_pool.parquet')
print("2차 리서치 풀 parquet 저장 완료:", pool.shape)
