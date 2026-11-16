import os
import pandas as pd
import numpy as np

daily_path = "D:/repo/korea-market_v3-anchor/data/dataset_2y/daily"
anchors = pd.read_csv("D:/repo/korea-market_v3-anchor/research/q2_position_low_break.csv")
anchors['date_str'] = pd.to_datetime(anchors['date']).dt.strftime('%Y%m%d')

all_dates = sorted([d for d in os.listdir(daily_path) if d.isdigit()])
date_to_idx = {d: i for i, d in enumerate(all_dates)}
test_dates = [d for d in all_dates if '20250102' <= d <= '20250331']

def evaluate_filters(min_to=300_000_000, max_surge=0.06, sl_mode="bar0_mid", tp_pct=0.030):
    trades = []
    
    for d_str in test_dates:
        d_idx = date_to_idx.get(d_str)
        if not d_idx:
            continue
            
        past_dates = [k for k, v in date_to_idx.items() if d_idx - 3 <= v < d_idx]
        day_anchors = anchors[(anchors['date_str'].isin(past_dates)) & (~anchors['low_broken'])]
        
        day_candidates = []
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
            
            for idx, bar in morning.iterrows():
                if bar['high'] < a_high:
                    continue
                if bar['open'] > a_high * 1.015:
                    continue
                if bar['turnover'] < min_to:
                    continue
                
                entry_p = max(bar['open'], a_high)
                surge_from_open = (entry_p - day_open) / day_open
                if surge_from_open > max_surge:
                    continue
                if surge_from_open < 0.005:
                    continue
                    
                day_candidates.append({
                    'symbol': sym,
                    'bar_idx': df.index.get_loc(idx),
                    'time_str': bar['time_str'],
                    'entry_p': entry_p,
                    'turnover': bar['turnover'],
                    'surge_from_open': surge_from_open,
                    'bar': bar,
                    'df': df
                })
                break
                
        day_candidates.sort(key=lambda x: (x['time_str'], -x['turnover']))
        selected = day_candidates[:2]
        
        for cand in selected:
            c_df = cand['df']
            entry_idx = cand['bar_idx']
            entry_p = cand['entry_p']
            ref_bar = cand['bar']
            post_bars = c_df.iloc[entry_idx + 1 :]
            if len(post_bars) == 0:
                continue
                
            if sl_mode == "bar0_mid":
                bar0_mid = (ref_bar['open'] + ref_bar['close']) / 2.0
                # Clamp Bar 0 Midpoint SL between -1.0% and -1.8%
                sl_price = max(entry_p * 0.982, min(entry_p * 0.990, bar0_mid))
            elif sl_mode == "fixed_15":
                sl_price = entry_p * 0.985
            else: # anchor_mid / wide
                sl_price = entry_p * 0.965
                
            tp_price = entry_p * (1.0 + tp_pct)
            
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
                    trail_sl = hwm * 0.988  # -1.2% trailing stop
                    sl_price = max(sl_price, trail_sl)
                    
                # Pessimistic check
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
            })
            
    tdf = pd.DataFrame(trades)
    if len(tdf) == 0:
        return "No trades"
    wins = tdf[tdf['win'] == 1]
    losses = tdf[tdf['win'] == 0]
    win_pnl = wins['pnl'].sum()
    loss_pnl = abs(losses['pnl'].sum()) if len(losses) > 0 else 0.001
    pf = win_pnl / loss_pnl if loss_pnl > 0 else 999.0
    return {
        'count': len(tdf),
        'win_rate': f"{tdf['win'].mean() * 100:.1f}%",
        'net_pnl': f"{tdf['pnl'].sum():.2f}%",
        'pf': f"{pf:.2f}",
        'avg_win': f"+{wins['pnl'].mean():.2f}%" if len(wins) > 0 else "0%",
        'avg_loss': f"{losses['pnl'].mean():.2f}%" if len(losses) > 0 else "0%",
    }

print("=== 비교 1: 기존 사양 (Turnover 1억, MaxSurge 무제한, 와이드 SL -3.5%) ===")
print(evaluate_filters(min_to=100_000_000, max_surge=0.20, sl_mode="wide", tp_pct=0.030))

print("\n=== 비교 2: SL만 Bar 0 Midpoint(-1.0%~-1.8%)로 개선 ===")
print(evaluate_filters(min_to=100_000_000, max_surge=0.20, sl_mode="bar0_mid", tp_pct=0.030))

print("\n=== 비교 3: SL Bar 0 Mid + Exhaustion Filter(시가대비 +6% 이내만) ===")
print(evaluate_filters(min_to=100_000_000, max_surge=0.06, sl_mode="bar0_mid", tp_pct=0.030))

print("\n=== 비교 4: SL Bar 0 Mid + Exhaustion Filter(+6%) + 거래대금 3억 이상 ===")
print(evaluate_filters(min_to=300_000_000, max_surge=0.06, sl_mode="bar0_mid", tp_pct=0.030))

print("\n=== 비교 5: SL Bar 0 Mid + Exhaustion Filter(+6%) + 거래대금 5억 이상 ===")
print(evaluate_filters(min_to=500_000_000, max_surge=0.06, sl_mode="bar0_mid", tp_pct=0.030))
