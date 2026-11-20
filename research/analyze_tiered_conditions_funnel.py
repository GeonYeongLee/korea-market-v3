import pandas as pd
import numpy as np

# 1. Load candidate pool & precomputed features
pool = pd.read_parquet('research/round2_candidate_pool.parquet')
pool['date_str'] = pool['date_str'].astype(str)
all_trading_days = sorted(pool['date_str'].unique())

# Alpha score
pool['s_rank'] = (20.0 - pool['to_rank'].clip(1, 20)) / 19.0
pool['s_shadow'] = (0.10 - pool['upper_shadow'].clip(0, 0.10)) / 0.10
pool['s_60d'] = (pool['ratio_60d'].clip(0.95, 1.10) - 0.95) / 0.15
pool['s_body'] = (pool['body_ret'].clip(5.0, 15.0) - 5.0) / 10.0
pool['score_alpha1'] = pool['s_rank'] * 0.25 + pool['s_shadow'] * 0.35 + pool['s_60d'] * 0.20 + pool['s_body'] * 0.20

cond_champ_b = (
    ((pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10) & (pool['body_ret'] >= 5.5)) |
    ((pool['to_rank'] > 10) & (pool['to_rank'] <= 20) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.08) & (pool['body_ret'] >= 5.5))
)
cands_b = pool[cond_champ_b].sort_values(['date_str', 'score_alpha1'], ascending=[True, False]).copy()
b_by_date = {d: grp['pnl_open'].tolist() for d, grp in cands_b.groupby('date_str')}

# 2. Daily Bars
df_daily = pd.read_parquet('data/dataset_2y/daily_bars_all.parquet')
df_daily['date_str'] = df_daily['date'].astype(str)
df_daily = df_daily[df_daily['date_str'] >= '2020-01-01'].copy()
df_daily = df_daily.sort_values(['symbol', 'date']).reset_index(drop=True)
df_daily['next_open'] = df_daily.groupby('symbol')['open'].shift(-1)
df_daily['gap_net'] = (df_daily['next_open'] - df_daily['close']) / df_daily['close'] * 100.0 - 0.28
df_daily['to_rank'] = df_daily.groupby('date')['turnover'].rank(ascending=False, method='min')
df_daily = df_daily[df_daily['change_pct'] > -29.0].copy()

# KOSDAQ Index
df_idx = pd.read_parquet('data/dataset_2y/index_daily_full.parquet')
df_idx['date_str'] = df_idx['date'].astype(str)
df_idx = df_idx.sort_values('date_str').reset_index(drop=True)
df_idx['kq_prev'] = df_idx['kq_close'].shift(1)
df_idx['kq_chg'] = (df_idx['kq_close'] - df_idx['kq_prev']) / df_idx['kq_prev'] * 100.0
df_daily = df_daily.merge(df_idx[['date_str', 'kq_chg']], on='date_str', how='left')

df_daily['open_gap'] = (df_daily['open'] - df_daily['prev_close']) / df_daily['prev_close'] * 100.0
df_daily['body_pct'] = (df_daily['close'] - df_daily['open']) / df_daily['open'] * 100.0

# Conditions:
# Pattern C: Individual price action (NO index condition)
c_cond = (df_daily['change_pct'] < 0.0) & (df_daily['open_gap'] <= -1.0) & (df_daily['body_pct'] >= 0.5) & (df_daily['to_rank'] <= 20) & df_daily['next_open'].notna()

# Pattern A: Market Panic Waterfall (KOSDAQ <= -2.0% condition)
a_cond = (df_daily['kq_chg'] <= -2.0) & (df_daily['change_pct'] <= -6.0) & (df_daily['to_rank'] <= 20) & df_daily['next_open'].notna()

c_by_date = {d: grp['gap_net'].tolist() for d, grp in df_daily[c_cond].groupby('date_str')}
a_by_date = {d: grp['gap_net'].tolist() for d, grp in df_daily[a_cond & (~c_cond)].groupby('date_str')}

# Index change mapping per date
kq_chg_map = df_idx.set_index('date_str')['kq_chg'].to_dict()

# Waterfal Funnel Analysis
total_days = len(all_trading_days)
b_hit_days = 0
b_miss_days = 0

c_hit_on_b_miss = 0
c_miss_on_b_miss = 0

kq_panic_on_c_miss = 0
kq_non_panic_on_c_miss = 0

a_hit_on_panic = 0
a_miss_on_panic = 0

total_trade_days = 0
total_zero_trade_days = 0

yearly_stats = {}

for d in all_trading_days:
    yr = d[:4]
    if yr not in yearly_stats:
        yearly_stats[yr] = {
            'total': 0, 'b_hit': 0, 'b_miss': 0,
            'c_hit': 0, 'c_miss': 0,
            'kq_panic_miss': 0, 'a_hit': 0,
            'cash_defense': 0
        }
    yearly_stats[yr]['total'] += 1
    
    b_cands = b_by_date.get(d, [])
    c_cands = c_by_date.get(d, [])
    a_cands = a_by_date.get(d, [])
    kq_chg = kq_chg_map.get(d, 0.0)
    
    if len(b_cands) > 0:
        b_hit_days += 1
        total_trade_days += 1
        yearly_stats[yr]['b_hit'] += 1
    else:
        b_miss_days += 1
        yearly_stats[yr]['b_miss'] += 1
        
        # Step 2: Check Pattern C
        if len(c_cands) > 0:
            c_hit_on_b_miss += 1
            total_trade_days += 1
            yearly_stats[yr]['c_hit'] += 1
            # Note: if c_cands < 2, could also partially fill with A if kq <= -2.0%
        else:
            c_miss_on_b_miss += 1
            yearly_stats[yr]['c_miss'] += 1
            
            # Step 3: Check KOSDAQ Index Panic <= -2.0%
            if kq_chg <= -2.0:
                kq_panic_on_c_miss += 1
                yearly_stats[yr]['kq_panic_miss'] += 1
                if len(a_cands) > 0:
                    a_hit_on_panic += 1
                    total_trade_days += 1
                    yearly_stats[yr]['a_hit'] += 1
                else:
                    a_miss_on_panic += 1
                    total_zero_trade_days += 1
                    yearly_stats[yr]['cash_defense'] += 1
            else:
                kq_non_panic_on_c_miss += 1
                total_zero_trade_days += 1
                yearly_stats[yr]['cash_defense'] += 1

print("=" * 100)
print("📊 [3단계 조건부 필터링 단계별 일수 분석 (2020 ~ 2026 전체)]")
print("=" * 100)
print(f"전체 거래일수: {total_days}일 (100.0%)\n")

print(f"▶ 1단계: 챔피언 B 후보 검사")
print(f"  - 챔피언 B 후보 발생일 (매수):      {b_hit_days:>4}일 ({b_hit_days/total_days*100:5.1f}%)")
print(f"  - 챔피언 B 후보 미발생일 (안 걸린 날): {b_miss_days:>4}일 ({b_miss_days/total_days*100:5.1f}%)\n")

print(f"▶ 2단계: 챔피언 B 미발생일({b_miss_days}일) 중 패턴 C(갭하락 양봉) 검사")
print(f"  - 패턴 C 후보 발생일 (폴백 매수):    {c_hit_on_b_miss:>4}일 ({c_hit_on_b_miss/b_miss_days*100:5.1f}%) [전체 기준 {c_hit_on_b_miss/total_days*100:5.1f}%]")
print(f"  - 패턴 C도 안 걸린 날:              {c_miss_on_b_miss:>4}일 ({c_miss_on_b_miss/b_miss_days*100:5.1f}%) [전체 기준 {c_miss_on_b_miss/total_days*100:5.1f}%]\n")

print(f"▶ 3단계: 패턴 C도 안 걸린 날({c_miss_on_b_miss}일) 중 코스닥 폭락(<= -2.0%) 여부")
print(f"  - 코스닥 지수 폭락일 (<= -2.0%):     {kq_panic_on_c_miss:>4}일 ({kq_panic_on_c_miss/c_miss_on_b_miss*100:5.1f}%)")
print(f"    * 이 중 패턴 A(투매주) 매수 진입:  {a_hit_on_panic:>4}일 ({a_hit_on_panic/c_miss_on_b_miss*100:5.1f}%)")
print(f"    * 패턴 A 조건 미부합 (TOP20 투매無): {a_miss_on_panic:>4}일")
print(f"  - 코스닥 정상/약보합 (<= -2.0% 미만): {kq_non_panic_on_c_miss:>4}일 ({kq_non_panic_on_c_miss/c_miss_on_b_miss*100:5.1f}%)\n")

print("=" * 100)
print(f"🛡 [최종 결론: 3단계 모두 안 걸리고 '완전 현금 보유(매수 0건)'로 쉰 날]")
print("=" * 100)
print(f"  - 총 매매 진행 거래일:  {total_days - total_zero_trade_days:>4}일 ({(total_days - total_zero_trade_days)/total_days*100:5.1f}%)")
print(f"  - 3단계 모두 미부합(현금 방어): {total_zero_trade_days:>4}일 ({total_zero_trade_days/total_days*100:5.1f}%)")
print("=" * 100)

print("\n📅 [연도별 단계별 일수 분석표]")
print("=" * 110)
print(f"{'연도':<6} | {'전체거래일':>8} | {'챔피언B 걸린날':>11} | {'챔피언B 안걸림':>11} | {'패턴C 매수':>9} | {'코스닥폭락':>8} | {'패턴A 매수':>9} | {'완전 현금방어':>11}")
print("-" * 110)

for yr, stats in yearly_stats.items():
    tot = stats['total']
    b_h = stats['b_hit']
    b_m = stats['b_miss']
    c_h = stats['c_hit']
    kq_p = stats['kq_panic_miss']
    a_h = stats['a_hit']
    cash = stats['cash_defense']
    print(f"{yr:<6} | {tot:>7}일 | {b_h:>9}일 | {b_m:>9}일 | {c_h:>8}일 | {kq_p:>7}일 | {a_h:>8}일 | {cash:>9}일 ({cash/tot*100:4.1f}%)")

print("=" * 110)
