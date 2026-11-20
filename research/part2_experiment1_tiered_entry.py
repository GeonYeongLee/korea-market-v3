import os
import sys
import duckdb
import pandas as pd
import numpy as np

print("=" * 105)
print("🚀 [EXPERIMENT 1: 계층형 진입(Tiered Entry) 전수 검증]")
print("=" * 105)

base_dir = 'D:/repo/korea-market_v3-anchor'
pool = pd.read_parquet(f'{base_dir}/research/quant_research_pool_temp.parquet')

# Import simulate_portfolio and print_result_block
from part1_prepare_and_engine import simulate_portfolio, print_result_block

# 1. Define Candidate Groups
# Tier 1 (TOP 10 초특급 대장주): to_rank <= 10, change_pct 8~25%, upper_shadow <= 0.15
cond_tier1 = (pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 25.0) & (pool['upper_shadow'] <= 0.15)

# Tier 2 (TOP 11~25 일반 대장주): to_rank 11~25, change_pct 10~22%, upper_shadow <= 0.10
cond_tier2 = (pool['to_rank'] >= 11) & (pool['to_rank'] <= 25) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10)

# Combined Tiered Condition
cond_tiered = cond_tier1 | cond_tier2

# Comparative Baselines:
# Baseline A: Flat TOP 10 Strict (to_rank <= 10, 10~22%, shadow <= 10%)
cond_top10_strict = (pool['to_rank'] <= 10) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10)

# Baseline B: Flat TOP 10 Loose (to_rank <= 10, 8~25%, shadow <= 15%)
cond_top10_loose = (pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 25.0) & (pool['upper_shadow'] <= 0.15)

# Baseline C: Flat TOP 25 Strict (to_rank <= 25, 10~22%, shadow <= 10%)
cond_top25_strict = (pool['to_rank'] <= 25) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10)

# Baseline D: Flat TOP 25 Loose (to_rank <= 25, 8~25%, shadow <= 15%)
cond_top25_loose = (pool['to_rank'] <= 25) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 25.0) & (pool['upper_shadow'] <= 0.15)

# Baseline E: Champion A (turnover_ratio >= 1.0% or turnover >= 2000B, 10~22%, shadow <= 20%)
# In our pool, to_rank <= 20 and shadow <= 20% approximates Champion A
cond_champ_b = (pool['to_rank'] <= 20) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.15)

print("\n--- 1. 단순 종목 풀 크기 비교 (체결 슬롯 제한 전) ---")
print(f"• Tier 1 단독 (TOP 10 초특급 대장주)      : {cond_tier1.sum():,} 건")
print(f"• Tier 2 단독 (TOP 11~25 일반 대장주)    : {cond_tier2.sum():,} 건")
print(f"• 계층형 결합 풀 (Tier 1 + Tier 2)        : {cond_tiered.sum():,} 건")
print(f"• Flat TOP 10 엄격형                     : {cond_top10_strict.sum():,} 건")
print(f"• Flat TOP 10 완화형                     : {cond_top10_loose.sum():,} 건")
print(f"• Flat TOP 25 엄격형                     : {cond_top25_strict.sum():,} 건")
print(f"• Flat TOP 25 완화형                     : {cond_top25_loose.sum():,} 건")

# Run Portfolios (5 slots, 10M KRW)
res_tiered = simulate_portfolio(pool[cond_tiered].copy())
res_top10_loose = simulate_portfolio(pool[cond_top10_loose].copy())
res_top25_strict = simulate_portfolio(pool[cond_top25_strict].copy())
res_top25_loose = simulate_portfolio(pool[cond_top25_loose].copy())

# Priority-sorted Tiered Portfolio:
# When selecting top 5 slots, pick Tier 1 candidates first (sorted by change_pct), then fill remaining slots with Tier 2 candidates!
tiered_df = pool[cond_tiered].copy()
tiered_df['tier_prio'] = np.where(tiered_df['to_rank'] <= 10, 1, 2)
tiered_df = tiered_df.sort_values(['date_str', 'tier_prio', 'change_pct'], ascending=[True, True, False]).reset_index(drop=True)
tiered_df['rank'] = tiered_df.groupby('date_str').cumcount() + 1
res_tiered_prio = simulate_portfolio(tiered_df, max_slots=5)

# Turnover-sorted Tiered Portfolio:
tiered_to_df = pool[cond_tiered].copy()
tiered_to_df = tiered_to_df.sort_values(['date_str', 'to_rank'], ascending=[True, True]).reset_index(drop=True)
tiered_to_df['rank'] = tiered_to_df.groupby('date_str').cumcount() + 1
res_tiered_to = simulate_portfolio(tiered_to_df, max_slots=5)

print_result_block("1. 계층형 진입 (Tiered Entry) - 기본 상승률 정렬", res_tiered)
print_result_block("2. 계층형 진입 (Tiered Entry) - Tier 1 우선 배정형", res_tiered_prio)
print_result_block("3. 계층형 진입 (Tiered Entry) - 거래대금 순위(Rank 1~N) 우선 정렬", res_tiered_to)
print_result_block("4. 비교군: Flat TOP 10 완화형 (8~25%, 윗꼬리 15%)", res_top10_loose)
print_result_block("5. 비교군: Flat TOP 25 엄격형 (10~22%, 윗꼬리 10%)", res_top25_strict)
print_result_block("6. 비교군: Flat TOP 25 완화형 (8~25%, 윗꼬리 15%)", res_top25_loose)

# Summary Comparison Table
print("\n" + "=" * 105)
print("📋 [EXPERIMENT 1 종합 비교 요약표 (2020~2026 7개년)]")
print("=" * 105)
print(f"{'전략명':<32} | {'체결수':<8} | {'승률':<8} | {'PF':<7} | {'건당평균':<9} | {'계좌수익':<10} | {'MDD':<8} | {'샤프':<6}")
print("-" * 105)
def print_comp_row(name, r):
    print(f"{name:<32} | {r['n']:>6}회 | {r['wr']:>6.2f}% | {r['pf']:>5.2f} | {r['avg']:>+7.3f}% | {r['acct_ret']:>+8.2f}% | {r['mdd']:>6.2f}% | {r['sharpe']:>5.2f}")

print_comp_row("1. 계층형 진입 (상승률순)", res_tiered)
print_comp_row("2. 계층형 진입 (Tier1우선)", res_tiered_prio)
print_comp_row("3. 계층형 진입 (대금순위순)", res_tiered_to)
print_comp_row("4. Flat TOP 10 완화형", res_top10_loose)
print_comp_row("5. Flat TOP 25 엄격형", res_top25_strict)
print_comp_row("6. Flat TOP 25 완화형", res_top25_loose)
print("=" * 105)
