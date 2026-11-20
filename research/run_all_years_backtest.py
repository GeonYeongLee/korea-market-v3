import duckdb
import pandas as pd
import numpy as np

print("=" * 100)
print("🚀 [전수 백테스트: 일봉 전체 데이터 (daily_bars_all.parquet) 기반 5슬롯 백테스트]")
print("=" * 100)

con = duckdb.connect()
path = 'data/dataset_2y/daily_bars_all.parquet'

# Query candidate bars from daily_bars_all.parquet
# Filter: turnover >= 2000e8, 10% <= change_pct < 20%, close > open, upper_shadow <= 0.25
query = f"""
WITH base AS (
    SELECT 
        symbol,
        date,
        open, high, low, close, volume, turnover, change_pct,
        (high - low) as candle_range,
        CASE WHEN (high - low) > 0 
             THEN (high - greatest(open, close)) / (high - low) 
             ELSE 0.0 END as upper_shadow,
        LEAD(open) OVER (PARTITION BY symbol ORDER BY date) as next_open,
        LEAD(date) OVER (PARTITION BY symbol ORDER BY date) as next_date
    FROM '{path}'
    WHERE turnover >= 2000e8
      AND change_pct >= 10.0 
      AND change_pct < 20.0
      AND close > open
)
SELECT *
FROM base
WHERE upper_shadow <= 0.25
  AND next_open IS NOT NULL
ORDER BY date, change_pct DESC
"""

print("Executing candidate extraction query on 11.8M daily bars...")
cands = con.execute(query).df()
print(f"Total historical candidate trades qualified: {len(cands):,} trades")
print(f"Candidate date range: {cands['date'].min()} to {cands['date'].max()}")

# Year breakdown of candidates
cands['yr'] = pd.to_datetime(cands['date']).dt.strftime('%Y')
print("\nQualified candidates per year:")
print(cands['yr'].value_counts().sort_index())

# Rank candidates by change_pct within each date
cands = cands.sort_values(['date', 'change_pct'], ascending=[True, False]).reset_index(drop=True)
cands['rank'] = cands.groupby('date').cumcount() + 1

# Filter for Top 5 slots
cands_5 = cands[cands['rank'] <= 5].copy()
print(f"\nCandidates with 5-slot cap: {len(cands_5):,} trades")

# Baseline Open Exit return
cands_5['open_pnl'] = ((cands_5['next_open'] - cands_5['close']) / cands_5['close'] * 100.0) - 0.28

# Now integrate conditional 08:00 NXT exit for 2025~2026
nxt_df = pd.read_parquet('research/nxt_trades_merged.parquet')
nxt_valid = nxt_df[nxt_df['pm_close'].notna()].copy()
nxt_tp_map = {}
for (sym, nd), group in nxt_valid.groupby(['symbol', 'exit_date']):
    entry_p = group.iloc[0]['entry_price']
    first_open = group.iloc[0]['pm_open']
    max_high = group['pm_high'].max()
    target_tp = entry_p * 1.030
    if max_high >= target_tp:
        nxt_tp_map[(sym, nd)] = max(first_open, target_tp)

# Apply NXT exit
final_pnls = []
exit_reasons = []
for _, row in cands_5.iterrows():
    sym = row['symbol']
    nd_str = pd.to_datetime(row['next_date']).strftime('%Y-%m-%d')
    key = (sym, nd_str)
    if key in nxt_tp_map:
        exec_p = nxt_tp_map[key]
        pnl = ((exec_p - row['close']) / row['close'] * 100.0) - 0.28
        final_pnls.append(pnl)
        exit_reasons.append('NXT_0800_TP')
    else:
        final_pnls.append(row['open_pnl'])
        exit_reasons.append('KRX_0900_OPEN')

cands_5['net_pnl'] = final_pnls
cands_5['exit_reason'] = exit_reasons

# Summary Statistics
print("\n" + "=" * 100)
print("📊 [역대 연도별 5슬롯 전략 성과 (수수료 0.28% 차감 후)]")
print("=" * 100)
print(f"{'연도':<6} | {'체결수':<8} | {'승률':<8} | {'PF':<6} | {'건당평균':<10} | {'누적합산 수익률':<15} | {'08시 익절 건수'}")
print("-" * 100)

for yr, g in cands_5.groupby('yr'):
    n = len(g)
    w = g[g['net_pnl'] > 0]
    l = g[g['net_pnl'] <= 0]
    wr = len(w) / n * 100.0 if n > 0 else 0.0
    w_sum = w['net_pnl'].sum()
    l_sum = abs(l['net_pnl'].sum())
    pf = w_sum / l_sum if l_sum > 0 else 999.0
    avg_p = g['net_pnl'].mean()
    tot_p = g['net_pnl'].sum()
    nxt_c = (g['exit_reason'] == 'NXT_0800_TP').sum()
    print(f"{yr:<6} | {n:>5}회   | {wr:>6.1f}% | {pf:>5.2f} | {avg_p:>+8.3f}% | {tot_p:>+13.2f}% | {nxt_c:>3}건")

print("=" * 100)

# 2020~2026 Modern Era Aggregate
modern = cands_5[cands_5['yr'] >= '2020']
m_n = len(modern)
m_w = modern[modern['net_pnl'] > 0]
m_l = modern[modern['net_pnl'] <= 0]
m_wr = len(m_w) / m_n * 100.0
m_pf = m_w['net_pnl'].sum() / abs(m_l['net_pnl'].sum())
print("\n" + "=" * 100)
print(f"🌟 [현대 유동성 장세 7개년 종합 (2020 ~ 2026년)]")
print("=" * 100)
print(f"• 총 체결 종목 수: {m_n:,}건 (연평균 {m_n / 7:.0f}건, 월평균 {m_n / (7*12):.1f}건)")
print(f"• 승률 (Win Rate): {m_wr:.2f}% ({len(m_w)}승 {len(m_l)}패)")
print(f"• 손익비 (PF)    : {m_pf:.2f}")
print(f"• 건당 평균 순익 : {modern['net_pnl'].mean():+.3f}%")
print(f"• 단순 누적 수익 : {modern['net_pnl'].sum():+.2f}%")
print("=" * 100)

# Save results
cands_5.to_parquet('research/historical_all_trades_5slots.parquet')
print("\nSaved research/historical_all_trades_5slots.parquet")
