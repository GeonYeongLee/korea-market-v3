import pandas as pd
import numpy as np
import json

# 1. Load candidate pool & precomputed features
pool = pd.read_parquet('research/round2_candidate_pool.parquet')
pool['date_str'] = pool['date_str'].astype(str)
all_trading_days = sorted(pool['date_str'].unique())

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
b_by_date = {d: grp[['symbol', 'pnl_open']].to_dict('records') for d, grp in cands_b.groupby('date_str')}

# 2. Daily Bars
df_daily = pd.read_parquet('data/dataset_2y/daily_bars_all.parquet')
df_daily['date_str'] = df_daily['date'].astype(str)
df_daily = df_daily[df_daily['date_str'] >= '2020-01-01'].copy()
df_daily = df_daily.sort_values(['symbol', 'date']).reset_index(drop=True)
df_daily['next_open'] = df_daily.groupby('symbol')['open'].shift(-1)
df_daily['gap_net'] = (df_daily['next_open'] - df_daily['close']) / df_daily['close'] * 100.0 - 0.28
df_daily['to_rank'] = df_daily.groupby('date')['turnover'].rank(ascending=False, method='min')
df_daily = df_daily[df_daily['change_pct'] > -29.0].copy()

# Index Data
df_idx = pd.read_parquet('data/dataset_2y/index_daily_full.parquet')
df_idx['date_str'] = df_idx['date'].astype(str)
df_idx = df_idx.sort_values('date_str').reset_index(drop=True)
df_idx['kq_prev'] = df_idx['kq_close'].shift(1)
df_idx['kq_chg'] = (df_idx['kq_close'] - df_idx['kq_prev']) / df_idx['kq_prev'] * 100.0
df_daily = df_daily.merge(df_idx[['date_str', 'kq_chg']], on='date_str', how='left')

df_daily['open_gap'] = (df_daily['open'] - df_daily['prev_close']) / df_daily['prev_close'] * 100.0
df_daily['body_pct'] = (df_daily['close'] - df_daily['open']) / df_daily['open'] * 100.0

# Decoupled Rules:
# Pattern C: Decoupled (No index requirement)
c_cond = (df_daily['change_pct'] < 0.0) & (df_daily['open_gap'] <= -1.0) & (df_daily['body_pct'] >= 0.5) & (df_daily['to_rank'] <= 20) & df_daily['next_open'].notna()
sub_c = df_daily[c_cond].copy(); sub_c['prio'] = 1; sub_c['type'] = 'C'

# Pattern A: KOSDAQ Panic <= -2.5%
a_cond = (df_daily['kq_chg'] <= -2.5) & (df_daily['change_pct'] <= -6.0) & (df_daily['to_rank'] <= 20) & df_daily['next_open'].notna()
sub_a = df_daily[a_cond & (~c_cond)].copy(); sub_a['prio'] = 2; sub_a['type'] = 'A'

combo = pd.concat([sub_c, sub_a], ignore_index=True).sort_values(['date_str', 'prio', 'turnover'], ascending=[True, True, False])
ac_by_date = {d: grp[['symbol', 'gap_net', 'type']].to_dict('records') for d, grp in combo.groupby('date_str')}

# Run simulation
init_cap = 10_000_000.0
fixed_slot = 5_000_000.0 # Fixed slot size for linear PnL

trades = []
cum_pnl = 0.0
peak_pnl = 0.0
max_dd = 0.0

for d in all_trading_days:
    b_list = b_by_date.get(d, [])
    ac_list = ac_by_date.get(d, [])
    
    chosen = []
    # Tier 1: Champion B first
    if len(b_list) > 0:
        for it in b_list[:2]:
            chosen.append({'symbol': it['symbol'], 'ret': it['pnl_open'], 'source': 'CHAMPION_B'})
    # Tier 2 & 3: Fallback if Champion B is 0
    elif len(ac_list) > 0:
        for it in ac_list[:2]:
            chosen.append({'symbol': it['symbol'], 'ret': it['gap_net'], 'source': f'CRISIS_{it["type"]}'})
            
    for item in chosen:
        ret = item['ret']
        pnl = fixed_slot * (ret / 100.0)
        cum_pnl += pnl
        peak_pnl = max(peak_pnl, cum_pnl)
        dd = cum_pnl - peak_pnl
        max_dd = min(max_dd, dd)
        trades.append({
            'date': d,
            'yr': d[:4],
            'month': d[:7],
            'symbol': item['symbol'],
            'ret': ret,
            'pnl': pnl,
            'cum_pnl': cum_pnl,
            'source': item['source']
        })

df_t = pd.DataFrame(trades)

# Save trades to csv for reference
df_t.to_csv('research/golden_25_decoupled_trades_2020_2026.csv', index=False)

# Compute full statistics
total_trades = len(df_t)
wr = (df_t['ret'] > 0).mean() * 100.0
avg_ret = df_t['ret'].mean()
med_ret = df_t['ret'].median()
std_ret = df_t['ret'].std()
wins = df_t[df_t['ret'] > 0]['ret'].sum()
losses = abs(df_t[df_t['ret'] <= 0]['ret'].sum())
pf = wins / losses if losses > 0 else 0
total_pnl = df_t['pnl'].sum()
mdd_pct = (max_dd / init_cap) * 100.0
sharpe = (avg_ret / std_ret) * np.sqrt(250) if std_ret > 0 else 0

# Max consecutive wins and losses
is_win = (df_t['ret'] > 0).astype(int)
win_streaks = is_win.groupby((~is_win.astype(bool)).cumsum()).sum()
loss_streaks = (~is_win.astype(bool)).astype(int).groupby(is_win.cumsum()).sum()
max_win_streak = win_streaks.max()
max_loss_streak = loss_streaks.max()

print("=" * 80)
print(f"📊 [골든 2.5% 분리 구조 백테스트 통계 종합 (2020 ~ 2026)]")
print("=" * 80)
print(f"총 거래수:           {total_trades:>6}건")
print(f"순 승률:             {wr:6.2f}%")
print(f"건당 평균 순수익률:  {avg_ret:+6.3f}% (중위수: {med_ret:+6.3f}%)")
print(f"Profit Factor (PF):  {pf:6.2f}")
print(f"총 누적 순손익:      {int(total_pnl):>12,d}원 (수익률: +{total_pnl/init_cap*100:.1f}%)")
print(f"최대 낙폭 (MDD):     {mdd_pct:6.2f}% ({int(max_dd):,d}원)")
print(f"Sharpe Ratio (연율): {sharpe:6.2f}")
print(f"최대 연속 승리:      {max_win_streak}연승 | 최대 연속 패배: {max_loss_streak}연패")

print("\n" + "=" * 80)
print("📌 [엔진별 기여도 분해 (Engine Attribution)]")
print("=" * 80)
for src, g in df_t.groupby('source'):
    src_n = len(g)
    src_wr = (g['ret'] > 0).mean() * 100.0
    src_avg = g['ret'].mean()
    src_pnl = g['pnl'].sum()
    src_wins = g[g['ret'] > 0]['ret'].sum()
    src_losses = abs(g[g['ret'] <= 0]['ret'].sum())
    src_pf = src_wins / src_losses if src_losses > 0 else 0
    print(f"{src:<18} | {src_n:>4}건 ({src_n/total_trades*100:4.1f}%) | 승률 {src_wr:5.1f}% | 건당 {src_avg:+6.3f}% | PF {src_pf:4.2f} | 손익 {int(src_pnl):>11,d}원")

print("\n" + "=" * 80)
print("📅 [연도별 성과 상세]")
print("=" * 80)
for yr, g in df_t.groupby('yr'):
    yr_n = len(g)
    yr_wr = (g['ret'] > 0).mean() * 100.0
    yr_avg = g['ret'].mean()
    yr_pnl = g['pnl'].sum()
    print(f"{yr:<6} | {yr_n:>4}건 | 승률 {yr_wr:5.1f}% | 건당 {yr_avg:+6.3f}% | 손익 {int(yr_pnl):>11,d}원")
