import pandas as pd
import numpy as np

print("Loading daily bars...")
df = pd.read_parquet("data/dataset_2y/daily_bars_2y.parquet")
df = df.sort_values(['symbol', 'date']).reset_index(drop=True)

df['change_pct'] = pd.to_numeric(df['change_pct'], errors='coerce')
df['turnover'] = pd.to_numeric(df['turnover'], errors='coerce')
df['adto20'] = pd.to_numeric(df['adto20'], errors='coerce')
df['to_ratio'] = df['turnover'] / df['adto20'].replace(0, np.nan)

body_top = np.maximum(df['open'], df['close'])
total_range = df['high'] - df['low']
df['upper_shadow_pct'] = np.where(total_range > 0, (df['high'] - body_top) / total_range, 0.0)

all_dates = sorted(df['date'].unique())
date_map = {d: i for i, d in enumerate(all_dates)}
df['date_idx'] = df['date'].map(date_map)

print("\n=== 1. DAILY CANDIDATE UNIVERSE SIZE (D-20 LOOKBACK) ===")
for min_chg, max_chg, label in [(7.0, 15.0, "+7% ~ +15%"), (10.0, 30.0, "+10% 이상"), (15.0, 30.0, "+15% 이상")]:
    for to_mult in [3.0, 5.0]:
        anchors = df[
            (df['change_pct'] >= min_chg) &
            (df['change_pct'] <= max_chg) &
            (df['to_ratio'] >= to_mult) &
            (df['turnover'] >= 5_000_000_000) &
            (df['upper_shadow_pct'] <= 0.35)
        ].copy()
        
        daily_counts = []
        for t_idx in range(25, len(all_dates)):
            c = ((anchors['date_idx'] >= t_idx - 20) & (anchors['date_idx'] < t_idx)).sum()
            daily_counts.append(c)
            
        s = pd.Series(daily_counts)
        print(f"기준봉 {label} & 거래대금 {to_mult}배 (D-20): 총 {len(anchors):,}개 | 일평균 감시종목: {s.mean():.1f}개 | 90%: {s.quantile(0.90):.1f}개 | 최대: {s.max()}개")

print("\n=== 2. FORWARD 20-DAY OUTCOME: +7% ANCHOR vs +15% ANCHOR ===")
def analyze_forward_20d(min_chg, max_chg, name):
    anchors = df[
        (df['change_pct'] >= min_chg) &
        (df['change_pct'] <= max_chg) &
        (df['to_ratio'] >= 3.0) &
        (df['turnover'] >= 5_000_000_000) &
        (df['upper_shadow_pct'] <= 0.35)
    ].copy()
    
    outcomes = []
    grouped = {sym: g.set_index('date_idx') for sym, g in df.groupby('symbol')}
    
    for _, a in anchors.sample(min(len(anchors), 1000), random_state=42).iterrows():
        sym = a['symbol']
        d_idx = a['date_idx']
        sym_df = grouped.get(sym)
        if sym_df is None:
            continue
        future = sym_df.loc[d_idx + 1 : d_idx + 20]
        if len(future) < 5:
            continue
            
        a_high = a['high']
        a_low = a['low']
        a_close = a['close']
        
        close_break = (future['close'] < a_low).any()
        low_break = (future['low'] < a_low).any()
        broke_high = (future['high'] >= a_high).any()
        
        if broke_high:
            first_brk_idx = future[future['high'] >= a_high].index[0]
            days_to_brk = first_brk_idx - d_idx
        else:
            days_to_brk = np.nan
            
        max_h = future['high'].max()
        min_l = future['low'].min()
        
        mfe = (max_h - a_close) / a_close * 100.0
        mae = (min_l - a_close) / a_close * 100.0
        
        outcomes.append({
            'low_break': low_break,
            'close_break': close_break,
            'broke_high': broke_high,
            'days_to_brk': days_to_brk,
            'mfe': mfe,
            'mae': mae
        })
        
    res = pd.DataFrame(outcomes)
    print(f"\n[{name} (표본 {len(res)}건)]")
    print(f"  - 20일간 저가(Low) 이탈 비율: {res['low_break'].mean()*100:.1f}%")
    print(f"  - 20일간 종가(Close) 저가 이탈 비율: {res['close_break'].mean()*100:.1f}%")
    
    safe = res[~res['low_break']]
    print(f"  [저가 미이탈 우량군 ({len(safe)}건, 전체의 {len(safe)/len(res)*100:.1f}%)]")
    print(f"    • 20일 이내 고가 재돌파율: {safe['broke_high'].mean()*100:.1f}%")
    print(f"    • 고가 재돌파까지 소요 일수: 중앙값 {safe['days_to_brk'].median():.1f}일 (평균: {safe['days_to_brk'].mean():.1f}일)")
    print(f"    • 평균 최대 상승(MFE): +{safe['mfe'].mean():.2f}%")
    print(f"    • 평균 최대 낙폭(MAE): {safe['mae'].mean():.2f}%")

analyze_forward_20d(7.0, 15.0, "+7% ~ +15% 기준봉")
analyze_forward_20d(15.0, 30.0, "+15% 이상 기준봉")
