import pandas as pd
import numpy as np
import os

daily_path = "data/dataset_2y/daily"
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

# Anchors: +7% ~ +30%, to_ratio >= 3.0, turnover >= 5B
anchors = df_daily[
    (df_daily['change_pct'] >= 7.0) &
    (df_daily['to_ratio'] >= 3.0) &
    (df_daily['turnover'] >= 5_000_000_000)
].copy()

test_dates = [d.replace('-', '') for d in all_dates if '2025-01-02' <= d <= '2025-03-31']
grouped = {sym: g.set_index('date_idx') for sym, g in df_daily.groupby('symbol')}

valid_active = []
for _, a in anchors.iterrows():
    sym = a['symbol']
    a_idx = a['date_idx']
    a_low = a['low']
    a_high = a['high']
    sym_df = grouped.get(sym)
    if sym_df is None:
        continue
    
    for t_idx in range(a_idx + 1, min(a_idx + 21, len(all_dates))):
        d_str = all_dates[t_idx].replace('-', '')
        if d_str not in test_dates:
            continue
        prev_days = sym_df.loc[a_idx + 1 : t_idx - 1]
        if len(prev_days) > 0 and (prev_days['low'] < a_low).any():
            break
            
        d_minus_1_close = sym_df.loc[t_idx - 1]['close'] if (t_idx - 1) in sym_df.index else 0
        prox_to_high = (d_minus_1_close - a_high) / a_high * 100.0
        
        # Only candidates within -5.0% of anchor high on D-1 close
        if -5.0 <= prox_to_high <= 0.0:
            valid_active.append({
                'date_str': d_str,
                'symbol': sym,
                'anchor_high': a_high,
                'anchor_low': a_low,
                'prox_to_high': prox_to_high
            })

active_df = pd.DataFrame(valid_active)
print(f"Candidates within -5% of Anchor High on D-1: {len(active_df)} (mean per day: {active_df.groupby('date_str').size().mean():.1f})")

# Test: Buy at market open (09:01) if day_open > D-1 close (positive gap or positive start)
# Target: +2.5%, SL: -2.0%
trades = []
for _, c in active_df.iterrows():
    sym = str(c['symbol']).zfill(6)
    d_str = c['date_str']
    fp = os.path.join(daily_path, d_str, f"{sym}.parquet")
    if not os.path.exists(fp):
        continue
    try:
        df = pd.read_parquet(fp)
    except Exception:
        continue
    if len(df) < 10:
        continue
        
    day_open = df.iloc[0]['open']
    bar0 = df.iloc[0]
    
    # Must open with volume >= 100M KRW and green/flat open
    if bar0['turnover'] < 100_000_000:
        continue
    if day_open < bar0['open'] * 0.99:
        continue
        
    entry_p = bar0['open']
    tp_p = entry_p * 1.025
    sl_p = entry_p * 0.980
    
    post = df.iloc[1:]
    if len(post) == 0:
        continue
        
    max_h = post['high'].max()
    min_l = post['low'].min()
    eod_c = post.iloc[-1]['close']
    
    hit_tp = max_h >= tp_p
    hit_sl = min_l <= sl_p
    
    if hit_sl and hit_tp:
        pnl = (sl_p - entry_p) / entry_p * 100 - 0.25
    elif hit_sl:
        pnl = (sl_p - entry_p) / entry_p * 100 - 0.25
    elif hit_tp:
        pnl = (tp_p - entry_p) / entry_p * 100 - 0.25
    else:
        pnl = (eod_c - entry_p) / entry_p * 100 - 0.25
        
    trades.append({
        'pnl': pnl,
        'win': 1 if pnl > 0 else 0,
        'mfe': (max_h - entry_p) / entry_p * 100,
        'mae': (min_l - entry_p) / entry_p * 100
    })

tdf = pd.DataFrame(trades)
print(f"Total Pre-Breakout Open Trades: {len(tdf)}")
if len(tdf) > 0:
    wins = tdf[tdf['win'] == 1]
    losses = tdf[tdf['win'] == 0]
    pf = wins['pnl'].sum() / abs(losses['pnl'].sum()) if len(losses) > 0 else 0
    print(f"Win Rate: {tdf['win'].mean()*100:.1f}%")
    print(f"Net PnL: {tdf['pnl'].sum():.2f}%")
    print(f"PF: {pf:.2f}")
    print(f"Avg Win: +{wins['pnl'].mean():.2f}% | Avg Loss: {losses['pnl'].mean():.2f}%")
    print(f"Avg MFE: +{tdf['mfe'].mean():.2f}% | Avg MAE: {tdf['mae'].mean():.2f}%")
