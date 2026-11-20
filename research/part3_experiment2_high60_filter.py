import os
import sys
import duckdb
import pandas as pd
import numpy as np

print("=" * 105)
print("🚀 [EXPERIMENT 2: 60일 신고가 근접도(Breakout Proximity) 필터 전수 검증]")
print("=" * 105)

base_dir = 'D:/repo/korea-market_v3-anchor'
pool = pd.read_parquet(f'{base_dir}/research/quant_research_pool_temp.parquet')

from part1_prepare_and_engine import simulate_portfolio, print_result_block

# 1. Inspect ratio_60d distribution and stats across all candidates
print("\n--- 1. 60일 최고가 대비 비율(close / prev_60d_high) 구간별 오버나이트 성과 비교 ---")
print(f"{'구간':<25} | {'후보건수':<8} | {'승률':<8} | {'손익비(PF)':<10} | {'건당평균':<10} | {'단순합산':<10}")
print("-" * 80)

bins = [
    (0.0, 0.80, "< 0.80 (역배열 심각 바닥권)"),
    (0.80, 0.85, "0.80 ~ 0.85"),
    (0.85, 0.90, "0.85 ~ 0.90"),
    (0.90, 0.95, "0.90 ~ 0.95 (매물대 저항 진입)"),
    (0.95, 1.00, "0.95 ~ 1.00 (60일 신고가 근접/임박)"),
    (1.00, 999.0, ">= 1.00 (60일 신고가 완전 돌파)"),
    (0.95, 999.0, "[통합] >= 0.95 (신고가 돌파/근접)"),
    (0.0, 0.95, "[비교] < 0.95 (신고가 미달)")
]

for low, high, label in bins:
    sub = pool[(pool['ratio_60d'] >= low) & (pool['ratio_60d'] < high) & (pool['cnt_history'] >= 40)]
    n = len(sub)
    if n > 0:
        wr = (sub['nxt_gap_ret'] > 0).mean() * 100.0
        w_sum = sub[sub['nxt_gap_ret'] > 0]['nxt_gap_ret'].sum()
        l_sum = abs(sub[sub['nxt_gap_ret'] <= 0]['nxt_gap_ret'].sum())
        pf = w_sum / l_sum if l_sum > 0 else 999.0
        avg_ret = sub['nxt_gap_ret'].mean()
        tot_ret = sub['nxt_gap_ret'].sum()
        print(f"{label:<25} | {n:>6}건 | {wr:>6.1f}% | {pf:>8.2f}   | {avg_ret:>+8.3f}% | {tot_ret:>+9.1f}%")

# 2. Apply 60d High filter (ratio_60d >= 0.95) to Tiered Entry
cond_tier1 = (pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 25.0) & (pool['upper_shadow'] <= 0.15)
cond_tier2 = (pool['to_rank'] >= 11) & (pool['to_rank'] <= 25) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10)
cond_tiered = cond_tier1 | cond_tier2

# Tiered without 60d filter
res_tiered_base = simulate_portfolio(pool[cond_tiered].copy())

# Tiered with 60d filter (>= 0.95)
cond_tiered_60d_95 = cond_tiered & (pool['ratio_60d'] >= 0.95) & (pool['cnt_history'] >= 40)
res_tiered_60d_95 = simulate_portfolio(pool[cond_tiered_60d_95].copy())

# Tiered with 60d Breakout filter (>= 1.00)
cond_tiered_60d_100 = cond_tiered & (pool['ratio_60d'] >= 1.00) & (pool['cnt_history'] >= 40)
res_tiered_60d_100 = simulate_portfolio(pool[cond_tiered_60d_100].copy())

# Tiered with Low 60d (< 0.95) - To verify what happens if filtered out
cond_tiered_low_60d = cond_tiered & (pool['ratio_60d'] < 0.95) & (pool['cnt_history'] >= 40)
res_tiered_low_60d = simulate_portfolio(pool[cond_tiered_low_60d].copy())

print_result_block("1. 계층형 진입 기준형 (신고가 필터 미적용)", res_tiered_base)
print_result_block("2. 계층형 진입 + 60일 신고가 돌파/근접(ratio >= 0.95)", res_tiered_60d_95)
print_result_block("3. 계층형 진입 + 60일 신고가 완전 돌파(ratio >= 1.00)", res_tiered_60d_100)
print_result_block("4. 계층형 진입 탈락군 (60일 신고가 미달, ratio < 0.95)", res_tiered_low_60d)

# 3. Apply 60d High Filter to Flat TOP 25 Strict
cond_top25_strict = (pool['to_rank'] <= 25) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10)
res_top25_base = simulate_portfolio(pool[cond_top25_strict].copy())
res_top25_60d_95 = simulate_portfolio(pool[cond_top25_strict & (pool['ratio_60d'] >= 0.95) & (pool['cnt_history'] >= 40)].copy())

# Summary Comparison Table
print("\n" + "=" * 105)
print("📋 [EXPERIMENT 2 종합 비교 요약표 (60일 신고가 필터 적용 효과)]")
print("=" * 105)
print(f"{'전략명':<38} | {'체결수':<8} | {'승률':<8} | {'PF':<7} | {'건당평균':<9} | {'계좌수익':<10} | {'MDD':<8} | {'샤프':<6}")
print("-" * 105)
def print_comp_row(name, r):
    print(f"{name:<38} | {r['n']:>6}회 | {r['wr']:>6.2f}% | {r['pf']:>5.2f} | {r['avg']:>+7.3f}% | {r['acct_ret']:>+8.2f}% | {r['mdd']:>6.2f}% | {r['sharpe']:>5.2f}")

print_comp_row("1. 계층형 진입 (신고가 필터 없음)", res_tiered_base)
print_comp_row("2. 계층형 진입 + 60일 신고가 돌파/근접(>=0.95)", res_tiered_60d_95)
print_comp_row("3. 계층형 진입 + 60일 신고가 완전돌파(>=1.00)", res_tiered_60d_100)
print_comp_row("4. 계층형 진입 - 신고가 미달 탈락군(<0.95)", res_tiered_low_60d)
print_comp_row("5. Flat TOP 25 엄격 (신고가 필터 없음)", res_top25_base)
print_comp_row("6. Flat TOP 25 엄격 + 60일 신고가(>=0.95)", res_top25_60d_95)
print("=" * 105)
