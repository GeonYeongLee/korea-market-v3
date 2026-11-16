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

body_top = np.maximum(df_daily['open'], df_daily['close'])
total_range = df_daily['high'] - df_daily['low']
df_daily['upper_shadow_pct'] = np.where(total_range > 0, (df_daily['high'] - body_top) / total_range, 0.0)

all_dates = sorted([pd.to_datetime(d).strftime('%Y-%m-%d') for d in df_daily['date'].unique()])
date_map = {d: i for i, d in enumerate(all_dates)}
df_daily['date_str'] = pd.to_datetime(df_daily['date']).dt.strftime('%Y-%m-%d')
df_daily['date_idx'] = df_daily['date_str'].map(date_map)

anchors = df_daily[
    (df_daily['change_pct'] >= 7.0) &
    (df_daily['change_pct'] <= 15.0) &
    (df_daily['to_ratio'] >= 3.0) &
    (df_daily['turnover'] >= 5_000_000_000) &
    (df_daily['upper_shadow_pct'] <= 0.35)
].copy()

test_dates = [d.replace('-', '') for d in all_dates if '2025-01-02' <= d <= '2025-06-30']
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
            'days_since': t_idx - a_idx,
            'd_minus_1_close': d_minus_1_close,
            'prox_to_high': prox_to_high
        })

active_df = pd.DataFrame(valid_active)
print(f"Total active candidate-days in H1 2025: {len(active_df)}")
print("Daily active candidates mean:", round(active_df.groupby('date_str').size().mean(), 1))

trades = []
for d_str, group in active_df.groupby('date_str'):
    cands = group.sort_values('prox_to_high', ascending=False).head(25)
    
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
        if len(df) < 10:
            continue
            
        a_high = c['anchor_high']
        df['time_str'] = df['timestamp'].astype(str).str.slice(11, 16)
        morning = df[(df['time_str'] >= '09:01') & (df['time_str'] <= '09:30')]
        if len(morning) == 0:
            continue
        day_open = df.iloc[0]['open']
        
        for idx, bar in morning.iterrows():
            if bar['high'] < a_high:
                continue
            if bar['open'] > a_high * 1.015:
                continue
            if bar['turnover'] < 200_000_000:
                continue
            entry_p = max(bar['open'], a_high)
            surge_from_open = (entry_p - day_open) / day_open
            if surge_from_open > 0.06:
                continue
            if surge_from_open < 0.005:
                continue
                
            day_entries.append({
                'symbol': sym,
                'bar_idx': df.index.get_loc(idx),
                'time_str': bar['time_str'],
                'entry_p': entry_p,
                'turnover': bar['turnover'],
                'bar': bar,
                'df': df,
                'days_since': c['days_since']
            })
            break
            
    day_entries.sort(key=lambda x: (x['time_str'], -x['turnover']))
    selected = day_entries[:2]
    
    for cand in selected:
        c_df = cand['df']
        entry_idx = cand['bar_idx']
        entry_p = cand['entry_p']
        ref_bar = cand['bar']
        post_bars = c_df.iloc[entry_idx + 1 :]
        if len(post_bars) == 0:
            continue
            
        bar0_mid = (ref_bar['open'] + ref_bar['close']) / 2.0
        sl_price = max(entry_p * 0.985, min(entry_p * 0.990, bar0_mid))
        tp_price = entry_p * 1.030
        
        hwm = entry_p
        pnl = None
        exit_reason = None
        
        for b_idx, p_bar in post_bars.iterrows():
            b_high = p_bar['high']
            b_low = p_bar['low']
            b_close = p_bar['close']
            if b_high > hwm:
                hwm = b_high
            if hwm >= tp_price:
                trail_sl = hwm * 0.988
                sl_price = max(sl_price, trail_sl)
            if b_low <= sl_price:
                pnl = (sl_price - entry_p) / entry_p * 100.0 - 0.25
                exit_reason = 'STOP_LOSS' if hwm < tp_price else 'TRAILING_STOP'
                break
            if b_high >= tp_price and hwm < tp_price:
                pnl = (tp_price - entry_p) / entry_p * 100.0 - 0.25
                exit_reason = 'TAKE_PROFIT'
                break
            if p_bar['time_str'] >= '15:20':
                pnl = (b_close - entry_p) / entry_p * 100.0 - 0.25
                exit_reason = 'TIMEOUT_1520'
                break
                
        if pnl is None:
            pnl = (post_bars.iloc[-1]['close'] - entry_p) / entry_p * 100.0 - 0.25
            exit_reason = 'EOD'
            
        trades.append({
            'symbol': cand['symbol'],
            'date': d_str,
            'pnl': pnl,
            'win': 1 if pnl > 0 else 0,
            'exit_reason': exit_reason,
            'days_since': cand['days_since']
        })

tdf = pd.DataFrame(trades)
print(f"\n=== +7%~+15% D-20 기준봉 실전 돌파 단타 성과 (H1 2025, 6개월) ===")
print(f"총 거래 수: {len(tdf)}")
if len(tdf) > 0:
    wins = tdf[tdf['win'] == 1]
    losses = tdf[tdf['win'] == 0]
    win_pnl = wins['pnl'].sum()
    loss_pnl = abs(losses['pnl'].sum()) if len(losses) > 0 else 0.001
    pf = win_pnl / loss_pnl if loss_pnl > 0 else 999.0
    print(f"승률: {tdf['win'].mean() * 100:.1f}%")
    print(f"누적 손익: {tdf['pnl'].sum():.2f}%")
    print(f"Profit Factor: {pf:.2f}")
    print(f"평균 이익: +{wins['pnl'].mean():.2f}% | 평균 손실: {losses['pnl'].mean():.2f}%")
    print(f"손익비: {abs(wins['pnl'].mean() / losses['pnl'].mean()):.2f}배")
    print("\n기준봉 발생 후 진입까지 걸린 일수(days_since) 분포:")
    print(tdf['days_since'].value_counts().sort_index())
