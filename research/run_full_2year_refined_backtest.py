import os
import pandas as pd
import numpy as np

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

anchors = df_daily[
    (df_daily['change_pct'] >= 7.0) &
    (df_daily['to_ratio'] >= 3.0) &
    (df_daily['turnover'] >= 5_000_000_000)
].copy()

# Test full 2 years: 2024-10-21 to 2026-11-16
test_dates = [d.replace('-', '') for d in all_dates if '2024-10-21' <= d <= '2026-11-16']
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
        
        valid_active.append({
            'date_str': d_str,
            'symbol': sym,
            'anchor_high': a_high,
            'anchor_low': a_low,
            'prox_to_high': prox_to_high
        })

active_df = pd.DataFrame(valid_active)
print(f"Total active candidate-days across 2 Years: {len(active_df)}")

# Simulate 2 Years with min_cum_to = 1B KRW (10억) and 2B KRW (20억)
for min_to in [1_000_000_000, 1_500_000_000, 2_000_000_000]:
    trades = []
    for d_str, group in active_df.groupby('date_str'):
        cands = group.sort_values('prox_to_high', ascending=False).head(30)
        day_entries = []
        for _, c in cands.iterrows():
            sym = str(c['symbol']).zfill(6)
            fp = os.path.join(daily_path, d_str, f"{sym}.parquet")
            if not os.path.exists(fp):
                continue
            try:
                df = pd.read_parquet(fp)
            except Exception:
                continue
            if len(df) < 15:
                continue
                
            a_high = c['anchor_high']
            df['time_str'] = df['timestamp'].astype(str).str.slice(11, 16)
            df['cum_to'] = df['turnover'].cumsum()
            
            morning = df[(df['time_str'] >= '09:01') & (df['time_str'] <= '09:30')]
            if len(morning) == 0:
                continue
            day_open = df.iloc[0]['open']
            
            for idx, bar in morning.iterrows():
                if bar['high'] < a_high:
                    continue
                if bar['open'] > a_high * 1.015:
                    continue
                cum_to_now = df.loc[idx, 'cum_to']
                if cum_to_now < min_to:
                    continue
                
                entry_p = max(bar['open'], a_high)
                surge_from_open = (entry_p - day_open) / day_open
                if surge_from_open > 0.08:
                    continue
                    
                day_entries.append({
                    'symbol': sym,
                    'bar_idx': df.index.get_loc(idx),
                    'time_str': bar['time_str'],
                    'entry_p': entry_p,
                    'cum_to': cum_to_now,
                    'df': df,
                })
                break
                
        day_entries.sort(key=lambda x: (x['time_str'], -x['cum_to']))
        selected = day_entries[:2]
        
        for cand in selected:
            c_df = cand['df']
            entry_idx = cand['bar_idx']
            entry_p = cand['entry_p']
            post = c_df.iloc[entry_idx + 1 :]
            if len(post) == 0:
                continue
                
            tp_p = entry_p * 1.030
            sl_p = entry_p * 0.975 # -2.5% SL
            hwm = entry_p
            pnl = None
            
            for b_idx, p_bar in post.iterrows():
                b_high = p_bar['high']
                b_low = p_bar['low']
                b_close = p_bar['close']
                if b_high > hwm:
                    hwm = b_high
                if hwm >= tp_p:
                    trail_sl = hwm * 0.985
                    sl_p = max(sl_p, trail_sl)
                if b_low <= sl_p:
                    pnl = (sl_p - entry_p) / entry_p * 100.0 - 0.25
                    break
                if b_high >= tp_p and hwm < tp_p:
                    pnl = (tp_p - entry_p) / entry_p * 100.0 - 0.25
                    break
                if p_bar['time_str'] >= '15:20':
                    pnl = (b_close - entry_p) / entry_p * 100.0 - 0.25
                    break
                    
            if pnl is None:
                pnl = (post.iloc[-1]['close'] - entry_p) / entry_p * 100.0 - 0.25
                
            trades.append(pnl)
            
    s = pd.Series(trades)
    wins = s[s > 0]
    losses = s[s <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else 0
    print(f"\n[2-YEAR FULL BACKTEST: Min Morning Turnover = {min_to/1e9:.1f}B KRW]")
    print(f"Total Trades: {len(s)}")
    print(f"Win Rate: {len(wins)/len(s)*100:.1f}%")
    print(f"Total Net Return (Sum %): {s.sum():.2f}%")
    print(f"Profit Factor: {pf:.2f}")
    print(f"Avg Win: +{wins.mean():.2f}% | Avg Loss: {losses.mean():.2f}%")
