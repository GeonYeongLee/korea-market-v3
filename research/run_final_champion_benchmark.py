import pandas as pd
import numpy as np

print("=" * 105)
print("📊 [STEP 9: 최종 올웨더 챔피언 vs 구버전 챔피언 전수 비교 벤치마크]")
print("=" * 105)

df = pd.read_parquet('research/candidate_pool_2020_2026.parquet')

# Load NXT TP map
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

nxt_rets = []
for _, r in df.iterrows():
    sym = r['symbol']
    nd_str = pd.to_datetime(r['next_date']).strftime('%Y-%m-%d')
    key = (sym, nd_str)
    if key in nxt_tp_map:
        exit_p = nxt_tp_map[key]
        pnl = ((exit_p - r['close']) / r['close'] * 100.0) - 0.28
    else:
        pnl = r['gap_ret']
    nxt_rets.append(pnl)
df['nxt_gap_ret'] = nxt_rets

all_dates = sorted(df['date_str'].unique())

def run_portfolio_benchmark(name, cond, max_slots=5):
    sub = df[cond].copy()
    sub = sub.sort_values(['date_str', 'change_pct'], ascending=[True, False]).reset_index(drop=True)
    sub['rank'] = sub.groupby('date_str').cumcount() + 1
    t = sub[sub['rank'] <= max_slots].copy()
    
    n_tot = len(t)
    wr = (t['nxt_gap_ret'] > 0).mean() * 100.0
    w_sum = t[t['nxt_gap_ret'] > 0]['nxt_gap_ret'].sum()
    l_sum = abs(t[t['nxt_gap_ret'] <= 0]['nxt_gap_ret'].sum())
    pf = w_sum / l_sum if l_sum > 0 else 999.0
    avg_ret = t['nxt_gap_ret'].mean()
    tot_ret = t['nxt_gap_ret'].sum()
    
    # Portfolio equity simulation (10M KRW, 5 slots = 2M per slot)
    initial_cap = 10_000_000.0
    slot_cap = initial_cap / max_slots
    equity = initial_cap
    daily_equities = []
    trades_by_date = {d: g for d, g in t.groupby('date_str')}
    
    for d in all_dates:
        if d in trades_by_date:
            d_trades = trades_by_date[d]
            day_pnl = sum([slot_cap * (r['nxt_gap_ret'] / 100.0) for _, r in d_trades.iterrows()])
            equity += day_pnl
        daily_equities.append({'date': d, 'equity': equity})
        
    eq_df = pd.DataFrame(daily_equities)
    eq_df['peak'] = eq_df['equity'].cummax()
    eq_df['dd'] = (eq_df['equity'] - eq_df['peak']) / eq_df['peak'] * 100.0
    mdd = eq_df['dd'].min()
    acct_ret = (equity / initial_cap - 1) * 100.0
    
    # Daily returns for Sharpe
    eq_df['daily_ret'] = eq_df['equity'].pct_change().fillna(0)
    sharpe = (eq_df['daily_ret'].mean() / eq_df['daily_ret'].std()) * np.sqrt(250) if eq_df['daily_ret'].std() > 0 else 0
    
    print("\n" + "=" * 105)
    print(f"📊 [{name}]")
    print("=" * 105)
    print(f"• 총 체결 횟수         : {n_tot:,} 회 (연평균 {n_tot/7:.1f}회)")
    print(f"• 승률 (Win Rate)      : {wr:.2f}% ({(t['nxt_gap_ret'] > 0).sum()}승 {(t['nxt_gap_ret'] <= 0).sum()}패)")
    print(f"• 손익비 (Profit Factor): {pf:.2f}")
    print(f"• 건당 평균 순손익     : {avg_ret:+.3f}% (수수료 0.28% 전액 차감 후)")
    print(f"• 단순 합산 누적수익   : {tot_ret:+.2f}%")
    print(f"• 계좌 순수익 (1,000만): {equity - initial_cap:+,.0f} 원 ({acct_ret:+.2f}%)")
    print(f"• 계좌 최대 낙폭 (MDD) : {mdd:.2f}%")
    print(f"• 샤프 지수 (Sharpe)   : {sharpe:.2f}")
    print("-" * 105)
    print(f"{'연도':<6} | {'체결수':<8} | {'승률':<8} | {'손익비(PF)':<10} | {'건당평균':<10} | {'연간단순합':<12} | {'연간계좌수익':<12}")
    print("-" * 105)
    
    for yr in ['2020', '2021', '2022', '2023', '2024', '2025', '2026']:
        gy = t[t['yr'] == yr]
        ny = len(gy)
        if ny > 0:
            wry = (gy['nxt_gap_ret'] > 0).mean() * 100.0
            wy = gy[gy['nxt_gap_ret'] > 0]['nxt_gap_ret'].sum()
            ly = abs(gy[gy['nxt_gap_ret'] <= 0]['nxt_gap_ret'].sum())
            pfy = wy / ly if ly > 0 else 999.0
            avgy = gy['nxt_gap_ret'].mean()
            toty = gy['nxt_gap_ret'].sum()
            # Year account return
            yr_dates = eq_df[eq_df['date'].str.startswith(yr)]
            yr_start = yr_dates.iloc[0]['equity']
            yr_end = yr_dates.iloc[-1]['equity']
            yr_acct_ret = (yr_end - yr_start) / initial_cap * 100.0
            print(f"{yr:<6} | {ny:>6}건 | {wry:>6.1f}% | {pfy:>8.2f}   | {avgy:>+8.3f}% | {toty:>+10.2f}% | {yr_acct_ret:>+10.2f}%")
    print("=" * 105)
    return t

# 1. Old Setup
t_old = run_portfolio_benchmark(
    "1. 구버전 챔피언 전략 (고정 2000억, 10~20%, 윗꼬리 25%, KOSPI 정배열)",
    (df['turnover'] >= 2000e8) & (df['change_pct'] >= 10) & (df['change_pct'] < 20) & 
    (df['upper_shadow'] <= 0.25) & (df['ks_bull'] == True)
)

# 2. All-Weather Champion 1: High Yield (상대거래대금 1.0%, 10~22%, 윗꼬리 20%)
t_aw1 = run_portfolio_benchmark(
    "2. 올웨더 챔피언 A [수익극대화형]: 상대거래대금(>=1.0%) + 10~22% + 윗꼬리<=20%",
    (df['turnover_ratio_to_mkt'] >= 1.0) & (df['change_pct'] >= 10) & (df['change_pct'] < 22) & 
    (df['upper_shadow'] <= 0.20)
)

# 3. All-Weather Champion 2: Ultra Stability (상대거래대금 1.0%, 10~22%, 윗꼬리 15%)
t_aw2 = run_portfolio_benchmark(
    "3. 올웨더 챔피언 B [초안정 방어형]: 상대거래대금(>=1.0%) + 10~22% + 윗꼬리<=15%",
    (df['turnover_ratio_to_mkt'] >= 1.0) & (df['change_pct'] >= 10) & (df['change_pct'] < 22) & 
    (df['upper_shadow'] <= 0.15)
)

# Save trades
t_aw1.to_csv('research/all_weather_champion_a_trades.csv', index=False, encoding='utf-8-sig')
t_aw2.to_csv('research/all_weather_champion_b_trades.csv', index=False, encoding='utf-8-sig')
print("체결 내역 CSV 저장 완료: research/all_weather_champion_a_trades.csv, research/all_weather_champion_b_trades.csv")
