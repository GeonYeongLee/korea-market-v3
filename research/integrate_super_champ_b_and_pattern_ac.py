"""
Precise Integration: Super Champion B (Univ 1, 622 trades, +18.9M) vs Pattern A+C
===============================================================================
Using the exact Champion B engine from research/synthesize_super_champion_b_c.py:
- Univ 1: Body5.5-Micro (622 trades)
- Sizing: 2-slot concentrated allocation (cap=0.60, 6M / 5M)
- Base Equity: 10,000,000 KRW
"""
import pandas as pd
import numpy as np

print("=" * 95)
print("🔬 [정밀 검증: 슈퍼 챔피언 B (+18.9M) 엔진 기반 패턴 A+C 결합 시뮬레이션]")
print("=" * 95)

# 1. Load candidate pool & precomputed features
pool = pd.read_parquet('research/round2_candidate_pool.parquet')
pool['date_str'] = pool['date_str'].astype(str)
all_trading_days = sorted(pool['date_str'].unique())

# Alpha Score
pool['s_rank'] = (20.0 - pool['to_rank'].clip(1, 20)) / 19.0
pool['s_shadow'] = (0.10 - pool['upper_shadow'].clip(0, 0.10)) / 0.10
pool['s_60d'] = (pool['ratio_60d'].clip(0.95, 1.10) - 0.95) / 0.15
pool['s_body'] = (pool['body_ret'].clip(5.0, 15.0) - 5.0) / 10.0
pool['score_alpha1'] = pool['s_rank'] * 0.25 + pool['s_shadow'] * 0.35 + pool['s_60d'] * 0.20 + pool['s_body'] * 0.20

# Champion B Universe (Univ 1: Body5.5-Micro)
cond_champ_b = (
    ((pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10) & (pool['body_ret'] >= 5.5)) |
    ((pool['to_rank'] > 10) & (pool['to_rank'] <= 20) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.08) & (pool['body_ret'] >= 5.5))
)
cands_b = pool[cond_champ_b].sort_values(['date_str', 'score_alpha1'], ascending=[True, False]).copy()

# 2. Build Pattern A+C Candidates from Daily Bars
df_daily = pd.read_parquet('data/dataset_2y/daily_bars_all.parquet')
df_daily['date_str'] = df_daily['date'].astype(str)
df_daily = df_daily[df_daily['date_str'] >= '2020-01-01'].copy()
df_daily = df_daily.sort_values(['symbol', 'date']).reset_index(drop=True)

df_daily['next_open'] = df_daily.groupby('symbol')['open'].shift(-1)
df_daily['gap_net'] = (df_daily['next_open'] - df_daily['close']) / df_daily['close'] * 100.0 - 0.28
df_daily['to_rank'] = df_daily.groupby('date')['turnover'].rank(ascending=False, method='min')
mkt_avg = df_daily.groupby('date')['change_pct'].mean().rename('mkt_avg_chg')
df_daily = df_daily.merge(mkt_avg, on='date', how='left')
df_daily = df_daily[df_daily['change_pct'] > -29.0].copy()

df_daily['open_gap'] = (df_daily['open'] - df_daily['prev_close']) / df_daily['prev_close'] * 100.0
df_daily['body_pct'] = (df_daily['close'] - df_daily['open']) / df_daily['open'] * 100.0

cond_a = (df_daily['mkt_avg_chg'] <= -2.0) & (df_daily['change_pct'] <= -5.0) & (df_daily['to_rank'] <= 20) & df_daily['next_open'].notna()
cond_c = (df_daily['mkt_avg_chg'] <= -2.0) & (df_daily['change_pct'] < 0.0) & (df_daily['open_gap'] <= -1.0) & (df_daily['body_pct'] >= 0.0) & (df_daily['to_rank'] <= 20) & df_daily['next_open'].notna()
df_daily['is_pattern_ac'] = cond_a | cond_c

cands_ac = df_daily[df_daily['is_pattern_ac']].sort_values(['date_str', 'turnover'], ascending=[True, False]).copy()

# Group candidates by date
b_by_date = {d: grp['pnl_open'].tolist() for d, grp in cands_b.groupby('date_str')}
ac_by_date = {d: grp['gap_net'].tolist() for d, grp in cands_ac.groupby('date_str')}

print(f"챔피언 B 신호 발생 일수: {len(b_by_date)}일 (총 {len(cands_b)}건)")
print(f"패턴 A+C 신호 발생 일수: {len(ac_by_date)}일 (총 {len(cands_ac)}건)")

def run_simulation(name, mode, max_slots=2, cap=0.60):
    initial_cap = 10_000_000.0
    equity = initial_cap
    curve = np.zeros(len(all_trading_days), dtype=np.float64)
    tot_trades = 0
    trade_pnls = []
    yearly_pnl = {'2020': 0.0, '2021': 0.0, '2022': 0.0, '2023': 0.0, '2024': 0.0, '2025': 0.0, '2026': 0.0}

    for idx, d in enumerate(all_trading_days):
        has_b = d in b_by_date
        has_ac = d in ac_by_date
        
        selected_pnls = []
        if mode == 'b_alone':
            if has_b:
                selected_pnls = b_by_date[d][:max_slots]
        elif mode == 'fallback': # 챔피언 B 0건일 때만 A+C
            if has_b:
                selected_pnls = b_by_date[d][:max_slots]
            elif has_ac:
                selected_pnls = ac_by_date[d][:max_slots]
        elif mode == 'override': # 패닉(A+C) 신호 뜨면 A+C 우선, 평소엔 B
            if has_ac:
                selected_pnls = ac_by_date[d][:max_slots]
            elif has_b:
                selected_pnls = b_by_date[d][:max_slots]
        elif mode == 'parallel': # 둘 다 가동 (슬롯 분할)
            pnls = []
            if has_b:
                pnls.extend(b_by_date[d][:1])
            if has_ac:
                pnls.extend(ac_by_date[d][:1])
            selected_pnls = pnls[:max_slots]

        k = len(selected_pnls)
        if k > 0:
            tot_trades += k
            pct = min(cap, 1.0 / k)
            alloc = initial_cap * pct
            yr = d[:4]
            day_pnl = 0.0
            for pnl_val in selected_pnls:
                amt = alloc * (pnl_val / 100.0)
                day_pnl += amt
                trade_pnls.append(pnl_val)
                if yr in yearly_pnl:
                    yearly_pnl[yr] += amt
            equity += day_pnl

        curve[idx] = equity

    peaks = np.maximum.accumulate(curve)
    dds = (curve - peaks) / peaks * 100.0
    mdd = np.min(dds)
    profit = equity - initial_cap
    ret = profit / initial_cap * 100.0

    pnl_arr = np.array(trade_pnls)
    wr = np.mean(pnl_arr > 0) * 100.0 if len(pnl_arr) > 0 else 0
    pos = pnl_arr[pnl_arr > 0].sum() if len(pnl_arr) > 0 else 0
    neg = np.abs(pnl_arr[pnl_arr <= 0].sum()) if len(pnl_arr) > 0 else 1
    pf = pos / neg if neg > 0 else 999.0
    avg_trade = np.mean(pnl_arr) if len(pnl_arr) > 0 else 0

    daily_rets = np.diff(curve) / curve[:-1]
    std_ret = np.std(daily_rets)
    sharpe = (np.mean(daily_rets) / std_ret * np.sqrt(252)) if std_ret > 0 else 0.0

    print(f"[{name}]")
    print(f"  • 총 거래 건수     : {tot_trades:>4}건 (승률 {wr:5.1f}%, PF {pf:4.2f})")
    print(f"  • 건당 평균 순손익 : {avg_trade:+.3f}%")
    print(f"  • 최종 계좌 순수익 : {profit:+12,.0f}원 ({ret:+.2f}%)")
    print(f"  • 계좌 최대 낙폭   : {mdd:6.2f}% | Sharpe: {sharpe:4.2f}")
    print(f"  • 연도별 순익: {yearly_pnl}\n")

run_simulation("1. 챔피언 B 단독 운용 (실제 오리지널 엔진)", 'b_alone')
run_simulation("2. 가설 1: 챔피언 B 0건일 때만 A+C 가동 (Fallback)", 'fallback')
run_simulation("3. 가설 2: 패닉일엔 A+C 우선, 평소엔 챔피언 B (Panic Override)", 'override')
run_simulation("4. 병행 운용: 둘 다 발생 시 슬롯 분할", 'parallel')
