import pandas as pd
import numpy as np

print("Testing Pullback Rebound on +7% anchors...")
df_daily = pd.read_parquet("data/dataset_2y/daily_bars_2y.parquet")
df_daily = df_daily.sort_values(['symbol', 'date']).reset_index(drop=True)

df_daily['change_pct'] = pd.to_numeric(df_daily['change_pct'], errors='coerce')
df_daily['turnover'] = pd.to_numeric(df_daily['turnover'], errors='coerce')
df_daily['adto20'] = pd.to_numeric(df_daily['adto20'], errors='coerce')
df_daily['to_ratio'] = df_daily['turnover'] / df_daily['adto20'].replace(0, np.nan)

all_dates = sorted([pd.to_datetime(d).strftime('%Y-%m-%d') for d in df_daily['date'].unique()])
date_map = {d: i for i, d in enumerate(all_dates)}
df_daily['date_str'] = pd.to_datetime(df_daily['date']).dt.strftime('%Y-%m-%d')
df_daily['date_idx'] = df_daily['date_str'].map(date_map)

# Anchors: +7% ~ +15%
anchors = df_daily[
    (df_daily['change_pct'] >= 7.0) &
    (df_daily['change_pct'] <= 15.0) &
    (df_daily['to_ratio'] >= 3.0) &
    (df_daily['turnover'] >= 5_000_000_000)
].copy()

# For 500 sample anchors that held low for at least 3 days:
# Suppose we buy when stock pulls back into anchor upper half [mid, high] and closes as an upward rebound candle:
grouped = {sym: g.set_index('date_idx') for sym, g in df_daily.groupby('symbol')}

rebound_trades = []
for _, a in anchors.sample(min(len(anchors), 1000), random_state=42).iterrows():
    sym = a['symbol']
    a_idx = a['date_idx']
    a_low = a['low']
    a_high = a['high']
    a_mid = (a_high + a_low) / 2.0
    sym_df = grouped.get(sym)
    if sym_df is None:
        continue
        
    for t_idx in range(a_idx + 1, min(a_idx + 21, len(all_dates))):
        prev = sym_df.loc[a_idx + 1 : t_idx - 1]
        if len(prev) > 0 and (prev['low'] < a_low).any():
            break
        if t_idx not in sym_df.index:
            continue
        bar = sym_df.loc[t_idx]
        # Pullback rebound condition:
        # 1. Low dipped near or above anchor mid (low >= a_low and low <= a_mid * 1.03)
        # 2. Turned into a green candle (close > open) with gain >= +2.0%
        if bar['low'] >= a_low and bar['close'] > bar['open'] and bar['change_pct'] >= 2.0:
            # Entry at close or next open
            entry_p = bar['close']
            future = sym_df.loc[t_idx + 1 : t_idx + 10]
            if len(future) < 3:
                continue
            max_h = future['high'].max()
            min_l = future['low'].min()
            mfe = (max_h - entry_p) / entry_p * 100.0
            mae = (min_l - entry_p) / entry_p * 100.0
            # Target +5%, SL -2.5%
            hit_tp = max_h >= entry_p * 1.05
            hit_sl = min_l <= entry_p * 0.975
            win = 1 if (hit_tp and not hit_sl) else (0 if hit_sl else (1 if mfe > 2.0 else 0))
            rebound_trades.append({
                'mfe': mfe, 'mae': mae, 'win': win,
                'days_since': t_idx - a_idx
            })
            break # 1 entry per anchor

res = pd.DataFrame(rebound_trades)
print(f"Total Pullback Rebound setups found: {len(res)}")
print(f"Win Rate (+5% TP vs -2.5% SL): {res['win'].mean()*100:.1f}%")
print(f"Average MFE: +{res['mfe'].mean():.2f}% | Average MAE: {res['mae'].mean():.2f}%")
