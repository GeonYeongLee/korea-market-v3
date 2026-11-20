import duckdb
import pandas as pd
import numpy as np

base_dir = 'D:/repo/korea-market_v3-anchor'
pool = pd.read_parquet(f'{base_dir}/research/round2_candidate_pool.parquet')
bars_path = f'{base_dir}/data/dataset_2y/daily_bars_all.parquet'
con = duckdb.connect()
all_trading_days = sorted(con.execute(f"SELECT DISTINCT strftime(date, '%Y-%m-%d') as d FROM '{bars_path}' WHERE date >= '2020-01-01'").df()['d'])

print("=" * 90)
print("🔍 1. 양봉 몸통 상승률((close - open) / open) 구간별 단독 성과 실측")
print("=" * 90)

# In round2_candidate_pool:
bins_body = [
    (-999, 4.0, "< 4.0% (시가 갭만 크고 장중 힘없음)"),
    (4.0, 6.0, "4.0% ~ 6.0%"),
    (6.0, 8.0, "6.0% ~ 8.0%"),
    (8.0, 10.0, "8.0% ~ 10.0% (실질 장대양봉 시작)"),
    (10.0, 14.0, "10.0% ~ 14.0% (강력한 장대양봉)"),
    (14.0, 999, ">= 14.0% (초강력 장대양봉)"),
    (8.0, 999, "[합산] >= 8.0% (실질 몸통 필터)"),
    (-999, 8.0, "[비교] < 8.0% (몸통 미달군)")
]

for b_min, b_max, label in bins_body:
    sub = pool[(pool['to_rank'] <= 20) & (pool['upper_shadow'] <= 0.12) & (pool['body_ret'] >= b_min) & (pool['body_ret'] < b_max)]
    if len(sub) > 0:
        wr = (sub['pnl_open'] > 0).mean() * 100.0
        w_sum = sub[sub['pnl_open'] > 0]['pnl_open'].sum()
        l_sum = abs(sub[sub['pnl_open'] <= 0]['pnl_open'].sum())
        pf = w_sum / l_sum if l_sum > 0 else 999.0
        avg_ret = sub['pnl_open'].mean()
        tot_ret = sub['pnl_open'].sum()
        print(f"{label:<35} | {len(sub):>5}건 | 승률 {wr:5.1f}% | PF {pf:4.2f} | 건당 {avg_ret:>+6.3f}% | 단순합 {tot_ret:>+7.1f}%")

print("\n" + "=" * 90)
print("🔍 2. 윗꼬리(upper_shadow) 구간별 단독 성과 실측 (TOP 20 유니버스)")
print("=" * 90)

bins_shadow = [
    (0.00, 0.05, "<= 0.05 (거의 민머리 장대양봉)"),
    (0.05, 0.08, "0.05 ~ 0.08 (초극소 윗꼬리)"),
    (0.08, 0.10, "0.08 ~ 0.10 (극소 윗꼬리)"),
    (0.10, 0.12, "0.10 ~ 0.12 (완화 윗꼬리)"),
    (0.12, 0.15, "0.12 ~ 0.15 (경계 윗꼬리)"),
    (0.15, 0.20, "0.15 ~ 0.20 (긴 윗꼬리)"),
    (0.00, 0.08, "[합산] <= 0.08"),
    (0.00, 0.10, "[합산] <= 0.10")
]

for s_min, s_max, label in bins_shadow:
    sub = pool[(pool['to_rank'] <= 20) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] >= s_min) & (pool['upper_shadow'] <= s_max)]
    if len(sub) > 0:
        wr = (sub['pnl_open'] > 0).mean() * 100.0
        w_sum = sub[sub['pnl_open'] > 0]['pnl_open'].sum()
        l_sum = abs(sub[sub['pnl_open'] <= 0]['pnl_open'].sum())
        pf = w_sum / l_sum if l_sum > 0 else 999.0
        avg_ret = sub['pnl_open'].mean()
        tot_ret = sub['pnl_open'].sum()
        print(f"{label:<35} | {len(sub):>5}건 | 승률 {wr:5.1f}% | PF {pf:4.2f} | 건당 {avg_ret:>+6.3f}% | 단순합 {tot_ret:>+7.1f}%")

print("\n" + "=" * 90)
print("🔍 3. 상대거래대금(to_mkt_share) 구간별 단독 성과 실측")
print("=" * 90)

bins_mkt = [
    (0.0, 0.5, "< 0.5%"),
    (0.5, 0.8, "0.5% ~ 0.8%"),
    (0.8, 1.0, "0.8% ~ 1.0%"),
    (1.0, 1.5, "1.0% ~ 1.5%"),
    (1.5, 999, ">= 1.5% (초대형 유동성)"),
    (1.0, 999, "[합산] >= 1.0%"),
    (0.8, 999, "[합산] >= 0.8%")
]

for m_min, m_max, label in bins_mkt:
    sub = pool[(pool['to_rank'] <= 25) & (pool['upper_shadow'] <= 0.12) & (pool['to_mkt_share'] >= m_min) & (pool['to_mkt_share'] < m_max)]
    if len(sub) > 0:
        wr = (sub['pnl_open'] > 0).mean() * 100.0
        w_sum = sub[sub['pnl_open'] > 0]['pnl_open'].sum()
        l_sum = abs(sub[sub['pnl_open'] <= 0]['pnl_open'].sum())
        pf = w_sum / l_sum if l_sum > 0 else 999.0
        avg_ret = sub['pnl_open'].mean()
        tot_ret = sub['pnl_open'].sum()
        print(f"{label:<35} | {len(sub):>5}건 | 승률 {wr:5.1f}% | PF {pf:4.2f} | 건당 {avg_ret:>+6.3f}% | 단순합 {tot_ret:>+7.1f}%")
