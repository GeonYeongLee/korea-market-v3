"""
Quantitative Overfitting, Survivorship Bias, and Live Microstructure Audit Script
Korea Market v3 - Golden Decoupled Strategy Audit
"""
import os
import sys
from pathlib import Path
import pandas as pd
import numpy as np

# Change directory and add to sys.path
root_dir = Path(__file__).resolve().parent.parent
os.chdir(root_dir)
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

print("=" * 80)
print("🔍 [QUANTITATIVE AUDIT] Starting comprehensive integrity & risk audit...")
print("=" * 80)

# 1. Load Data
pool = pd.read_parquet('research/round2_candidate_pool.parquet')
pool['date_str'] = pool['date_str'].astype(str)
all_trading_days = sorted(pool['date_str'].unique())

pool['s_rank'] = (20.0 - pool['to_rank'].clip(1, 20)) / 19.0
pool['s_shadow'] = (0.10 - pool['upper_shadow'].clip(0, 0.10)) / 0.10
pool['s_60d'] = (pool['ratio_60d'].clip(0.95, 1.10) - 0.95) / 0.15
pool['s_body'] = (pool['body_ret'].clip(5.0, 15.0) - 5.0) / 10.0
pool['score_alpha1'] = pool['s_rank'] * 0.25 + pool['s_shadow'] * 0.35 + pool['s_60d'] * 0.20 + pool['s_body'] * 0.20

df_daily = pd.read_parquet('data/dataset_2y/daily_bars_all.parquet')
df_daily['date_str'] = df_daily['date'].astype(str)
df_daily = df_daily[df_daily['date_str'] >= '2020-01-01'].copy()
df_daily = df_daily.sort_values(['symbol', 'date']).reset_index(drop=True)

# Build market calendar and check halt jumps
market_days = sorted(df_daily['date_str'].unique())
market_day_map = {d: i for i, d in enumerate(market_days)}

df_daily['next_date_str'] = df_daily.groupby('symbol')['date_str'].shift(-1)
df_daily['curr_idx'] = df_daily['date_str'].map(market_day_map)
df_daily['next_idx'] = df_daily['next_date_str'].map(market_day_map)
df_daily['day_diff'] = df_daily['next_idx'] - df_daily['curr_idx']

df_daily['next_open'] = df_daily.groupby('symbol')['open'].shift(-1)
df_daily['to_rank'] = df_daily.groupby('date')['turnover'].rank(ascending=False, method='min')
df_daily = df_daily[df_daily['change_pct'] > -29.0].copy()

# Index
df_idx = pd.read_parquet('data/dataset_2y/index_daily_full.parquet')
df_idx['date_str'] = df_idx['date'].astype(str)
df_idx = df_idx.sort_values('date_str').reset_index(drop=True)
df_idx['kq_prev'] = df_idx['kq_close'].shift(1)
df_idx['kq_chg'] = (df_idx['kq_close'] - df_idx['kq_prev']) / df_idx['kq_prev'] * 100.0
df_daily = df_daily.merge(df_idx[['date_str', 'kq_chg']], on='date_str', how='left')

df_daily['open_gap'] = (df_daily['open'] - df_daily['prev_close']) / df_daily['prev_close'] * 100.0
df_daily['body_pct'] = (df_daily['close'] - df_daily['open']) / df_daily['open'] * 100.0

# -------------------------------------------------------------
# AUDIT 1: DATA INTEGRITY & SURVIVORSHIP BIAS
# -------------------------------------------------------------
print("\n" + "=" * 80)
print("📋 [AUDIT 1] Data Integrity, Trading Halts, and Extreme Return Check")
print("=" * 80)

trades = pd.read_csv('research/golden_25_decoupled_trades_2020_2026.csv', dtype={'symbol': str})
trades['symbol'] = trades['symbol'].str.zfill(6)
df_daily['symbol'] = df_daily['symbol'].astype(str).str.zfill(6)

merged_trades = trades.merge(
    df_daily[['symbol', 'date_str', 'day_diff', 'next_date_str', 'close', 'open', 'high', 'low', 'change_pct', 'next_open']],
    left_on=['symbol', 'date'],
    right_on=['symbol', 'date_str'],
    how='left'
)

halt_jumps = merged_trades[merged_trades['day_diff'] > 1]
print(f"1. Trading Halt Jumps (day_diff > 1): {len(halt_jumps)}건 / {len(trades)}건")
if len(halt_jumps) > 0:
    print(halt_jumps[['date', 'symbol', 'source', 'ret', 'next_date_str', 'day_diff']].head(10))

# Upper Limit Lock on Entry Day (T close == upper limit +30%)
# Upper limit in KRX is typically +29.5% ~ +30.0%
ul_entries = merged_trades[merged_trades['change_pct'] >= 29.5]
print(f"2. Upper Limit Lock Entry (+30% 체결불가 종목 매수 여부): {len(ul_entries)}건")
if len(ul_entries) > 0:
    print(ul_entries[['date', 'symbol', 'change_pct', 'ret', 'source']])

# Next day open at lower limit (-30% 하한가 잠김)
# Calculate next open return vs prev close
merged_trades['next_open_chg'] = (trades['ret'] + 0.28) # approx raw gap
ll_exits = merged_trades[merged_trades['next_open_chg'] <= -29.0]
print(f"3. Lower Limit Lock Exit (-30% 하한가 잠김 매도불가 여부): {len(ll_exits)}건")

# Extreme returns (> +30% or < -30% on overnight gap) indicating unadjusted splits / data errors
extreme_ret = trades[(trades['ret'] > 30.0) | (trades['ret'] < -30.0)]
print(f"4. Extreme Overnight Gap (> 30% or < -30% 이상치): {len(extreme_ret)}건")
if len(extreme_ret) > 0:
    print(extreme_ret)

# Unique symbols traded
unique_symbols = trades['symbol'].astype(str).unique()
print(f"5. Total Unique Symbols Traded: {len(unique_symbols)}종목")

# -------------------------------------------------------------
# AUDIT 2: OVERFITTING RISK & PARAMETER SENSITIVITY (PLATEAU)
# -------------------------------------------------------------
print("\n" + "=" * 80)
print("📊 [AUDIT 2] Parameter Sensitivity & Plateau Analysis (Overfitting Test)")
print("=" * 80)

def simulate_strategy(champ_body_min=5.5, c_gap_max=-1.0, c_body_min=0.5, a_kq_max=-2.5, to_rank_limit=20):
    # Champion B
    cond_b = (
        ((pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10) & (pool['body_ret'] >= champ_body_min)) |
        ((pool['to_rank'] > 10) & (pool['to_rank'] <= to_rank_limit) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.08) & (pool['body_ret'] >= champ_body_min))
    )
    cands_b = pool[cond_b].sort_values(['date_str', 'score_alpha1'], ascending=[True, False])
    b_dict = {d: grp['pnl_open'].tolist() for d, grp in cands_b.groupby('date_str')}

    # Decoupled Crisis
    c_cond = (df_daily['change_pct'] < 0.0) & (df_daily['open_gap'] <= c_gap_max) & (df_daily['body_pct'] >= c_body_min) & (df_daily['to_rank'] <= to_rank_limit) & df_daily['next_open'].notna()
    sub_c = df_daily[c_cond].copy(); sub_c['prio'] = 1
    
    a_cond = (df_daily['kq_chg'] <= a_kq_max) & (df_daily['change_pct'] <= -6.0) & (df_daily['to_rank'] <= to_rank_limit) & df_daily['next_open'].notna()
    sub_a = df_daily[a_cond & (~c_cond)].copy(); sub_a['prio'] = 2

    # calculate gap_net for sub_c and sub_a
    combo = pd.concat([sub_c, sub_a], ignore_index=True).sort_values(['date_str', 'prio', 'turnover'], ascending=[True, True, False])
    combo['gap_net'] = (combo['next_open'] - combo['close']) / combo['close'] * 100.0 - 0.28
    ac_dict = {d: grp['gap_net'].tolist() for d, grp in combo.groupby('date_str')}

    sim_trades = []
    cum_pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    fixed_slot = 5_000_000.0

    for d in all_trading_days:
        b_list = b_dict.get(d, [])
        ac_list = ac_dict.get(d, [])
        
        chosen = []
        if len(b_list) > 0:
            chosen = b_list[:2]
        elif len(ac_list) > 0:
            chosen = ac_list[:2]
            
        for ret in chosen:
            pnl = fixed_slot * (ret / 100.0)
            cum_pnl += pnl
            peak = max(peak, cum_pnl)
            max_dd = min(max_dd, cum_pnl - peak)
            sim_trades.append(ret)

    if not sim_trades:
        return {'trades': 0, 'wr': 0, 'avg_ret': 0, 'pf': 0, 'mdd': 0, 'pnl': 0}
        
    s = pd.Series(sim_trades)
    wr = (s > 0).mean() * 100.0
    avg_ret = s.mean()
    wins = s[s > 0].sum()
    losses = abs(s[s <= 0].sum())
    pf = wins / losses if losses > 0 else 0
    pnl = len(s) * avg_ret * 50_000.0 # approx
    mdd_pct = (max_dd / 10_000_000.0) * 100.0
    return {'trades': len(s), 'wr': wr, 'avg_ret': avg_ret, 'pf': pf, 'mdd': mdd_pct, 'pnl': cum_pnl}

# Sensitivity 1: KOSDAQ Panic Threshold (Pattern A)
print("\n[Sensitivity 1] KOSDAQ Panic Threshold (Base: -2.5%)")
for th in [-1.5, -2.0, -2.5, -3.0, -3.5, -4.0]:
    res = simulate_strategy(a_kq_max=th)
    print(f"KOSDAQ <= {th:4.1f}% | 거래수: {res['trades']:>4} | 승률: {res['wr']:5.1f}% | 건당: {res['avg_ret']:+6.3f}% | PF: {res['pf']:4.2f} | MDD: {res['mdd']:5.2f}% | 순익: {int(res['pnl']):>11,d}원")

# Sensitivity 2: Pattern C Open Gap Max
print("\n[Sensitivity 2] Pattern C Open Gap Max (Base: -1.0%)")
for gap in [0.0, -0.5, -1.0, -1.5, -2.0]:
    res = simulate_strategy(c_gap_max=gap)
    print(f"Open Gap <= {gap:4.1f}% | 거래수: {res['trades']:>4} | 승률: {res['wr']:5.1f}% | 건당: {res['avg_ret']:+6.3f}% | PF: {res['pf']:4.2f} | MDD: {res['mdd']:5.2f}% | 순익: {int(res['pnl']):>11,d}원")

# Sensitivity 3: Pattern C Body Min
print("\n[Sensitivity 3] Pattern C Body Min (Base: +0.5%)")
for b in [0.0, 0.3, 0.5, 0.8, 1.0, 1.5]:
    res = simulate_strategy(c_body_min=b)
    print(f"Body Min >= {b:4.1f}% | 거래수: {res['trades']:>4} | 승률: {res['wr']:5.1f}% | 건당: {res['avg_ret']:+6.3f}% | PF: {res['pf']:4.2f} | MDD: {res['mdd']:5.2f}% | 순익: {int(res['pnl']):>11,d}원")

# Sensitivity 4: Champion B Body Min
print("\n[Sensitivity 4] Champion B Body Min (Base: 5.5%)")
for cb in [4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0]:
    res = simulate_strategy(champ_body_min=cb)
    print(f"Champ Body >= {cb:4.1f}% | 거래수: {res['trades']:>4} | 승률: {res['wr']:5.1f}% | 건당: {res['avg_ret']:+6.3f}% | PF: {res['pf']:4.2f} | MDD: {res['mdd']:5.2f}% | 순익: {int(res['pnl']):>11,d}원")

# Sensitivity 5: Turnover Rank Limit
print("\n[Sensitivity 5] Turnover Rank Limit (Base: TOP 20)")
for rk in [10, 15, 20, 25, 30]:
    res = simulate_strategy(to_rank_limit=rk)
    print(f"Turnover TOP {rk:>2} | 거래수: {res['trades']:>4} | 승률: {res['wr']:5.1f}% | 건당: {res['avg_ret']:+6.3f}% | PF: {res['pf']:4.2f} | MDD: {res['mdd']:5.2f}% | 순익: {int(res['pnl']):>11,d}원")

# -------------------------------------------------------------
# AUDIT 3: TRANSACTION COSTS, SLIPPAGE & KRX TICKS
# -------------------------------------------------------------
print("\n" + "=" * 80)
print("💸 [AUDIT 3] Transaction Cost, Slippage & Microstructure Audit")
print("=" * 80)

# Import KRX tick function
from core.krx_ticks import get_krx_tick_size

# Historical securities transaction tax by year in Korea:
# 2020: 0.25%
# 2021-2022: 0.23%
# 2023: 0.20%
# 2024: 0.18%
# 2025-2026: 0.15%
tax_by_year = {
    '2020': 0.25,
    '2021': 0.23,
    '2022': 0.23,
    '2023': 0.20,
    '2024': 0.18,
    '2025': 0.15,
    '2026': 0.15,
}

# Broker commission: KIS standard API 0.015% each side = 0.030% roundtrip
broker_commission = 0.030

# Calculate exact tick slippage on historical trades
trades_fee_audit = merged_trades.copy()
trades_fee_audit['yr'] = trades_fee_audit['date'].str[:4]
trades_fee_audit['statutory_tax'] = trades_fee_audit['yr'].map(tax_by_year).fillna(0.15)

# Calculate KRX tick % at entry (close) and exit (next open)
trades_fee_audit['entry_tick'] = trades_fee_audit['close'].apply(get_krx_tick_size)
trades_fee_audit['exit_tick'] = trades_fee_audit['next_open'].apply(get_krx_tick_size)

trades_fee_audit['entry_tick_pct'] = (trades_fee_audit['entry_tick'] / trades_fee_audit['close']) * 100.0
trades_fee_audit['exit_tick_pct'] = (trades_fee_audit['exit_tick'] / trades_fee_audit['next_open']) * 100.0

# 1 tick roundtrip slippage = 1 tick at entry + 1 tick at exit
trades_fee_audit['slippage_1tick_pct'] = trades_fee_audit['entry_tick_pct'] + trades_fee_audit['exit_tick_pct']
trades_fee_audit['slippage_05tick_pct'] = 0.5 * trades_fee_audit['slippage_1tick_pct']

# Gross return before any deduction
trades_fee_audit['gross_ret'] = trades_fee_audit['ret'] + 0.28

# Scenarios:
# Scenario A: Baseline Backtest (0.28% fixed)
trades_fee_audit['ret_base'] = trades_fee_audit['ret'] # already gross - 0.28

# Scenario B: Realistic Historical Tax + Broker Commission (0.03%) + 0 Slippage
trades_fee_audit['cost_hist_notick'] = trades_fee_audit['statutory_tax'] + broker_commission
trades_fee_audit['ret_hist_notick'] = trades_fee_audit['gross_ret'] - trades_fee_audit['cost_hist_notick']

# Scenario C: Realistic Historical Tax + Broker Commission + 0.5 Tick Slippage (conservative)
trades_fee_audit['cost_hist_05tick'] = trades_fee_audit['statutory_tax'] + broker_commission + trades_fee_audit['slippage_05tick_pct']
trades_fee_audit['ret_hist_05tick'] = trades_fee_audit['gross_ret'] - trades_fee_audit['cost_hist_05tick']

# Scenario D: High Friction Stress Test: Historical Tax + Broker Commission + 1.0 Tick Full Slippage
trades_fee_audit['cost_hist_1tick'] = trades_fee_audit['statutory_tax'] + broker_commission + trades_fee_audit['slippage_1tick_pct']
trades_fee_audit['ret_hist_1tick'] = trades_fee_audit['gross_ret'] - trades_fee_audit['cost_hist_1tick']

print(f"평균 1틱 슬리피지(진입+청산): {trades_fee_audit['slippage_1tick_pct'].mean():.3f}% (중위수: {trades_fee_audit['slippage_1tick_pct'].median():.3f}%)")
print(f"평균 0.5틱 슬리피지(진입+청산): {trades_fee_audit['slippage_05tick_pct'].mean():.3f}%\n")

scenarios = [
    ("A. 백테스트 베이스라인 (고정 0.28%)", 'ret_base'),
    ("B. 연도별 실세율(0.15~0.25%) + 수수료(0.03%) [무슬리피지]", 'ret_hist_notick'),
    ("C. 현실적 표준 모델: 실세율 + 수수료 + 0.5틱 슬리피지", 'ret_hist_05tick'),
    ("D. 보수적 스트레스 모델: 실세율 + 수수료 + 1.0틱 슬리피지", 'ret_hist_1tick'),
]

for name, col in scenarios:
    s = trades_fee_audit[col]
    wr = (s > 0).mean() * 100.0
    avg_r = s.mean()
    wins = s[s > 0].sum()
    losses = abs(s[s <= 0].sum())
    pf = wins / losses if losses > 0 else 0
    pnl = (s / 100.0 * 5_000_000.0).sum()
    print(f"[{name}]")
    print(f"  승률: {wr:5.2f}% | 건당 평균: {avg_r:+6.3f}% | PF: {pf:4.2f} | 총 누적손익: {int(pnl):>12,d}원\n")

print("=" * 80)
print("✅ [AUDIT COMPLETE]")
print("=" * 80)
