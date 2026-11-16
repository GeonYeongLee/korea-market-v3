import os
import pandas as pd
import numpy as np

daily_path = "D:/repo/korea-market_v3-anchor/data/dataset_2y/daily"
anchors = pd.read_csv("D:/repo/korea-market_v3-anchor/research/q2_position_low_break.csv")
anchors['date_str'] = pd.to_datetime(anchors['date']).dt.strftime('%Y%m%d')

all_dates = sorted([d for d in os.listdir(daily_path) if d.isdigit()])
date_to_idx = {d: i for i, d in enumerate(all_dates)}
test_dates = [d for d in all_dates if '20250102' <= d <= '20250331']

def run_simulation(max_surge_pct=0.06, sl_pct=0.015, tp_pct=0.030, trailing_buffer=0.012, max_slots=2):
    trades = []
    
    for d_str in test_dates:
        d_idx = date_to_idx.get(d_str)
        if not d_idx:
            continue
            
        past_dates = [k for k, v in date_to_idx.items() if d_idx - 3 <= v < d_idx]
        day_anchors = anchors[(anchors['date_str'].isin(past_dates)) & (~anchors['low_broken'])]
        
        day_trades = []
        for _, a in day_anchors.iterrows():
            sym = str(a['symbol']).zfill(6)
            fp = os.path.join(daily_path, d_str, f"{sym}.parquet")
            if not os.path.exists(fp):
                continue
            try:
                df = pd.read_parquet(fp)
            except Exception:
                continue
            if len(df) < 10:
                continue
                
            a_high = a['anchor_high']
            df['time_str'] = df['timestamp'].astype(str).str.slice(11, 16)
            morning = df[(df['time_str'] >= '09:01') & (df['time_str'] <= '09:30')]
            if len(morning) == 0:
                continue
                
            day_open = df.iloc[0]['open']
            
            # Check 1-min bars for breakout
            for idx, bar in morning.iterrows():
                # Filter 1: bar high reaches anchor high
                if bar['high'] < a_high:
                    continue
                # Filter 2: not opened way above anchor high (> 1.5% gap)
                if bar['open'] > a_high * 1.015:
                    continue
                # Filter 3: bar turnover >= 100M KRW
                if bar['turnover'] < 100_000_000:
                    continue
                
                entry_p = max(bar['open'], a_high)
                
                # Filter 4: Exhaustion filter (must not have surged > max_surge_pct from day open)
                surge_from_open = (entry_p - day_open) / day_open
                if surge_from_open > max_surge_pct:
                    continue
                # Also must be at least +1.0% from day open to show upward momentum
                if surge_from_open < 0.005:
                    continue
                    
                # Passed filters -> record candidate
                day_trades.append({
                    'symbol': sym,
                    'bar_idx': df.index.get_loc(idx),
                    'time_str': bar['time_str'],
                    'entry_p': entry_p,
                    'turnover': bar['turnover'],
                    'surge_from_open': surge_from_open,
                    'df': df
                })
                break # Only 1 entry per symbol per day
                
        # Sort candidates by time, then turnover (if tied)
        day_trades.sort(key=lambda x: (x['time_str'], -x['turnover']))
        
        # Take up to max_slots
        selected = day_trades[:max_slots]
        
        for cand in selected:
            c_df = cand['df']
            entry_idx = cand['bar_idx']
            entry_p = cand['entry_p']
            post_bars = c_df.iloc[entry_idx + 1 :]
            if len(post_bars) == 0:
                continue
                
            # Simulate execution bar by bar
            # Bar 0 Stop Loss: ref_bar open or body midpoint, or fixed sl_pct
            ref_bar = c_df.iloc[entry_idx]
            bar0_mid = (ref_bar['open'] + ref_bar['close']) / 2.0
            
            # Effective Stop Loss price
            sl_price = max(entry_p * (1.0 - sl_pct), min(entry_p * 0.99, bar0_mid))
            tp_price = entry_p * (1.0 + tp_pct)
            
            hwm = entry_p
            pnl = None
            exit_reason = None
            bars_held = 0
            
            for b_idx, p_bar in post_bars.iterrows():
                bars_held += 1
                b_high = p_bar['high']
                b_low = p_bar['low']
                b_close = p_bar['close']
                
                if b_high > hwm:
                    hwm = b_high
                    
                # Trailing stop if TP reached
                if hwm >= tp_price:
                    trail_sl = hwm * (1.0 - trailing_buffer)
                    sl_price = max(sl_price, trail_sl)
                    
                # Check pessimistic SL first
                if b_low <= sl_price:
                    pnl = (sl_price - entry_p) / entry_p * 100.0 - 0.25
                    exit_reason = 'STOP_LOSS' if hwm < tp_price else 'TRAILING_STOP'
                    break
                    
                # Check TP if not trailing
                if b_high >= tp_price and hwm < tp_price:
                    # hit TP
                    pnl = (tp_price - entry_p) / entry_p * 100.0 - 0.25
                    exit_reason = 'TAKE_PROFIT'
                    break
                    
                # EOD hard timeout at 15:20
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
                'bars_held': bars_held,
                'surge_from_open': cand['surge_from_open'] * 100.0
            })
            
    tdf = pd.DataFrame(trades)
    if len(tdf) == 0:
        return {}
    wins = tdf[tdf['win'] == 1]
    losses = tdf[tdf['win'] == 0]
    win_pnl = wins['pnl'].sum()
    loss_pnl = abs(losses['pnl'].sum()) if len(losses) > 0 else 0.001
    pf = win_pnl / loss_pnl if loss_pnl > 0 else 999.0
    return {
        'count': len(tdf),
        'win_rate': round(tdf['win'].mean() * 100, 1),
        'total_pnl': round(tdf['pnl'].sum(), 2),
        'profit_factor': round(pf, 2),
        'avg_win': round(wins['pnl'].mean(), 2) if len(wins) > 0 else 0,
        'avg_loss': round(losses['pnl'].mean(), 2) if len(losses) > 0 else 0,
    }

print("Running parameter grid on Q1 2025...")
results = []
for max_surge in [0.045, 0.055, 0.070, 0.15]:
    for sl in [0.012, 0.015, 0.020, 0.035]:
        for tp in [0.025, 0.030, 0.035]:
            r = run_simulation(max_surge_pct=max_surge, sl_pct=sl, tp_pct=tp)
            r['max_surge'] = f"{int(max_surge*100)}%"
            r['sl'] = f"-{sl*100:.1f}%"
            r['tp'] = f"+{tp*100:.1f}%"
            results.append(r)

rdf = pd.DataFrame(results)
print("\nTop 10 Configurations by Profit Factor:")
print(rdf.sort_values('profit_factor', ascending=False).head(10).to_string())

print("\nCurrent Base Config (MaxSurge=15%, SL=-3.5%, TP=+3.0%):")
print(rdf[(rdf['max_surge'] == '15%') & (rdf['sl'] == '-3.5%') & (rdf['tp'] == '+3.0%')].to_string())
