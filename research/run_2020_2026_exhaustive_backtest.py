import duckdb
import pandas as pd
import numpy as np

print("=" * 105)
print("🚀 [2020 ~ 2026년 7개년 전수 백테스트: 5슬롯 + 조건부 08:00 NXT 익절 + 09:00 KRX 시초가 청산]")
print("=" * 105)

con = duckdb.connect()
path = 'data/dataset_2y/daily_bars_all.parquet'

# 1. Market Index construction for 2020~2026
print("1. KOSPI 시장 지수 및 추세선(MA10 >= MA20) 생성 중...")
mkt_query = f"""
SELECT 
    date,
    strftime(date, '%Y-%m-%d') as date_str,
    sum(change_pct * turnover) / sum(turnover) as mkt_ret
FROM '{path}'
WHERE date >= '2019-11-01' AND turnover > 0
GROUP BY 1, 2
ORDER BY 1
"""
mkt_df = con.execute(mkt_query).df()
mkt_df['mkt_ret_clipped'] = mkt_df['mkt_ret'].clip(-15.0, 15.0)

# Build synthetic market index level
levels = [2200.0]
for r in mkt_df['mkt_ret_clipped'].iloc[1:]:
    levels.append(levels[-1] * (1.0 + r / 100.0))
mkt_df['mkt_index'] = levels

# Overwrite 2024~2026 with official KOSPI if available
idx_path = 'data/dataset_2y/index_daily.parquet'
try:
    idx_df = pd.read_parquet(idx_path)
    idx_df['date_str'] = pd.to_datetime(idx_df['date']).dt.strftime('%Y-%m-%d')
    idx_map = dict(zip(idx_df['date_str'], idx_df['ks_close']))
    for i, r in mkt_df.iterrows():
        d_str = r['date_str']
        if d_str in idx_map and pd.notna(idx_map[d_str]):
            mkt_df.at[i, 'mkt_index'] = idx_map[d_str]
except Exception:
    pass

mkt_df['ma10'] = mkt_df['mkt_index'].rolling(10).mean()
mkt_df['ma20'] = mkt_df['mkt_index'].rolling(20).mean()
mkt_df['is_bull'] = mkt_df['ma10'] >= mkt_df['ma20']
bull_dates = set(mkt_df[mkt_df['is_bull'] == True]['date_str'])

print(f"2020~2026 총 거래일 수: {len(mkt_df[mkt_df['date_str'] >= '2020-01-01'])}일 | 정배열(상승장) 일수: {len(bull_dates)}일")

# 2. Extract Candidates from daily_bars_all
print("2. 2020~2026 전체 350만 개 일봉 데이터에서 후보 종목 추출 중...")
extract_query = f"""
WITH ordered_bars AS (
    SELECT 
        symbol,
        date,
        strftime(date, '%Y-%m-%d') as date_str,
        open, high, low, close, volume, turnover, change_pct,
        (high - low) as candle_range,
        CASE WHEN (high - low) > 0 
             THEN (high - greatest(open, close)) / (high - low) 
             ELSE 0.0 END as upper_shadow,
        LEAD(open) OVER (PARTITION BY symbol ORDER BY date) as next_open,
        LEAD(date) OVER (PARTITION BY symbol ORDER BY date) as next_date
    FROM '{path}'
    WHERE date >= '2019-12-01'
)
SELECT *
FROM ordered_bars
WHERE date >= '2020-01-01'
  AND turnover >= 2000e8
  AND change_pct >= 10.0 
  AND change_pct < 20.0
  AND upper_shadow <= 0.25
  AND close > open
  AND next_open IS NOT NULL
  AND date_diff('day', date, next_date) <= 5
ORDER BY date, change_pct DESC
"""

cands = con.execute(extract_query).df()
print(f"총 추출된 후보 종목 수: {len(cands):,}건")

# 3. Load 08:00 NXT Pre-market Data (available from 2025-03-25)
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

# 4. Compute Net Returns
trades = []
for _, row in cands.iterrows():
    sym = row['symbol']
    ed_str = row['date_str']
    nd_str = pd.to_datetime(row['next_date']).strftime('%Y-%m-%d')
    entry_p = row['close']
    open_0900 = row['next_open']
    
    key = (sym, nd_str)
    if key in nxt_tp_map:
        exit_p = nxt_tp_map[key]
        exit_reason = "NXT_0800_EARLY_TP"
    else:
        exit_p = open_0900
        exit_reason = "KRX_0900_OPEN"
        
    net_pnl = ((exit_p - entry_p) / entry_p * 100.0) - 0.28 # 0.28% fee
    is_bull = ed_str in bull_dates
    
    trades.append({
        'symbol': sym,
        'entry_date': ed_str,
        'exit_date': nd_str,
        'entry_price': entry_p,
        'exit_price': exit_p,
        'exit_reason': exit_reason,
        'net_pnl': net_pnl,
        'change_pct': row['change_pct'],
        'turnover': row['turnover'],
        'is_bull_kospi': is_bull,
        'yr': ed_str[:4],
        'ym': ed_str[:7],
    })

tdf = pd.DataFrame(trades)

# Rank candidates within each day by change_pct
tdf = tdf.sort_values(['entry_date', 'change_pct'], ascending=[True, False]).reset_index(drop=True)
tdf['rank_chg'] = tdf.groupby('entry_date').cumcount() + 1

# Strategy Variants to Test:
# Variant 1: 코스피 정배열 필터(MA10 >= MA20) + 5슬롯 (챔피언 전략)
# Variant 2: 필터 없는 순수 5슬롯 (전체 시장 환경)
print("\n" + "=" * 105)
print("📊 [2020 ~ 2026 연도별 성과 비교: '코스피 정배열 필터 5슬롯' vs '노필터 5슬롯']")
print("=" * 105)

t_filt = tdf[(tdf['rank_chg'] <= 5) & (tdf['is_bull_kospi'] == True)].copy()
t_nofilt = tdf[tdf['rank_chg'] <= 5].copy()

print(f"{'연도':<6} | {'필터 체결수':<10} | {'필터 승률':<9} | {'필터 PF':<8} | {'필터 건당평균':<11} | {'필터 누적합':<12} | {'노필터 체결수':<11} | {'노필터 건당평균':<13} | {'노필터 누적합'}")
print("-" * 105)

years = sorted(tdf['yr'].unique())
for yr in years:
    # Filtered
    gf = t_filt[t_filt['yr'] == yr]
    nf = len(gf)
    wrf = (gf['net_pnl'] > 0).mean() * 100.0 if nf > 0 else 0.0
    wf_sum = gf[gf['net_pnl'] > 0]['net_pnl'].sum()
    lf_sum = abs(gf[gf['net_pnl'] <= 0]['net_pnl'].sum())
    pff = wf_sum / lf_sum if lf_sum > 0 else 999.0
    avgf = gf['net_pnl'].mean() if nf > 0 else 0.0
    totf = gf['net_pnl'].sum() if nf > 0 else 0.0

    # No filter
    gn = t_nofilt[t_nofilt['yr'] == yr]
    nn = len(gn)
    avgn = gn['net_pnl'].mean() if nn > 0 else 0.0
    totn = gn['net_pnl'].sum() if nn > 0 else 0.0

    print(f"{yr:<6} | {nf:>6}회    | {wrf:>6.1f}%  | {pff:>6.2f} | {avgf:>+9.3f}% | {totf:>+10.2f}% | {nn:>7}회     | {avgn:>+10.3f}%  | {totn:>+10.2f}%")

print("=" * 105)

# Overall 2020~2026 Summary
nf_tot = len(t_filt)
wrf_tot = (t_filt['net_pnl'] > 0).mean() * 100.0
wf_sum_tot = t_filt[t_filt['net_pnl'] > 0]['net_pnl'].sum()
lf_sum_tot = abs(t_filt[t_filt['net_pnl'] <= 0]['net_pnl'].sum())
pff_tot = wf_sum_tot / lf_sum_tot
avgf_tot = t_filt['net_pnl'].mean()
totf_tot = t_filt['net_pnl'].sum()

print(f"\n🏆 [7개년 종합 (2020 ~ 2026) 챔피언 전략 성적 (코스피 정배열 + 5슬롯)]")
print(f"• 총 체결수           : {nf_tot:,}회 (연평균 {nf_tot / 7:.1f}회, 월평균 {nf_tot / (7*12):.1f}회)")
print(f"• 전체 승률 (Win Rate): {wrf_tot:.2f}% ({(t_filt['net_pnl'] > 0).sum()}승 {(t_filt['net_pnl'] <= 0).sum()}패)")
print(f"• 전체 손익비 (PF)    : {pff_tot:.2f}")
print(f"• 보상위험비 (Payoff) : {t_filt[t_filt['net_pnl'] > 0]['net_pnl'].mean() / abs(t_filt[t_filt['net_pnl'] <= 0]['net_pnl'].mean()):.2f}배")
print(f"• 건당 평균 순손익    : {avgf_tot:+.3f}% (0.28% 수수료/세금 전액 차감 후)")
print(f"• 단순 합산 누적수익  : {totf_tot:+.2f}%")

# 5. Portfolio Simulation (10M KRW, 5 slots = 2M per slot)
all_dates = sorted(cands['date_str'].unique())
initial_capital = 10_000_000.0
slot_unit = initial_capital / 5.0

simple_equity = initial_capital
compound_equity = initial_capital

simple_curve = []
compound_curve = []

trades_by_date = {d: g for d, g in t_filt.groupby('entry_date')}

for d in all_dates:
    if d in trades_by_date:
        d_trades = trades_by_date[d]
        k = len(d_trades)
        day_simple_pnl = sum([slot_unit * (r['net_pnl'] / 100.0) for _, r in d_trades.iterrows()])
        simple_equity += day_simple_pnl
        
        slot_alloc_c = compound_equity / 5.0
        day_compound_pnl = sum([slot_alloc_c * (r['net_pnl'] / 100.0) for _, r in d_trades.iterrows()])
        compound_equity += day_compound_pnl
    else:
        day_simple_pnl = 0.0
        day_compound_pnl = 0.0
        
    simple_curve.append({'date': d, 'equity': simple_equity, 'pnl': day_simple_pnl})
    compound_curve.append({'date': d, 'equity': compound_equity, 'pnl': day_compound_pnl})

sc_df = pd.DataFrame(simple_curve)
cc_df = pd.DataFrame(compound_curve)

sc_df['peak'] = sc_df['equity'].cummax()
sc_df['dd'] = (sc_df['equity'] - sc_df['peak']) / sc_df['peak'] * 100.0
mdd_s = sc_df['dd'].min()

cc_df['peak'] = cc_df['equity'].cummax()
cc_df['dd'] = (cc_df['equity'] - cc_df['peak']) / cc_df['peak'] * 100.0
mdd_c = cc_df['dd'].min()

print("\n" + "=" * 105)
print("💰 [1,000만원 계좌 포트폴리오 7개년 시뮬레이션 결과]")
print("=" * 105)
print(f"• [단리 / 5슬롯 정액 분할] 누적 수익금: {simple_equity - initial_capital:+,.0f} 원 (+{(simple_equity/initial_capital - 1)*100:+.2f}%) | MDD: {mdd_s:.2f}%")
print(f"• [복리 / 5슬롯 재투자 분할] 최종 자산: {compound_equity:,.0f} 원 (+{(compound_equity/initial_capital - 1)*100:+.2f}%) | MDD: {mdd_c:.2f}%")
print("=" * 105)

# Save trades
t_filt.to_csv('research/backtest_2020_2026_trades.csv', index=False, encoding='utf-8-sig')
print("\n전체 7개년 체결 상세 저장 완료: research/backtest_2020_2026_trades.csv")
