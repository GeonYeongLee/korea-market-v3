"""
Exhaustive Optimization & Robustness Validation for Model 2:
Anchor Candle Close Betting -> D+1 Smart Step-Up Morning Exit.
Grid across:
- Turnover: 50B, 70B, 100B, 150B KRW
- Gain: 10%, 12%, 15%, 18%
- Upper shadow: 0.10, 0.15, 0.20, 0.25
- Exit TP: +2.5%, +3.0%, +4.0%, +5.0%
- Exit Trailing Buffer: 1.0%, 1.5%
- Stop Loss: -2.0%, -2.5%, -3.0%
"""
import os
import pandas as pd
import numpy as np

def load_data():
    print("Loading daily dataset...")
    df_daily = pd.read_parquet("data/dataset_2y/daily_bars_2y.parquet")
    df_daily = df_daily.sort_values(['symbol', 'date']).reset_index(drop=True)
    df_daily['symbol'] = df_daily['symbol'].astype(str).str.zfill(6)
    df_daily['change_pct'] = pd.to_numeric(df_daily['change_pct'], errors='coerce')
    df_daily['turnover'] = pd.to_numeric(df_daily['turnover'], errors='coerce')
    df_daily['adto20'] = pd.to_numeric(df_daily['adto20'], errors='coerce')
    df_daily['to_ratio'] = df_daily['turnover'] / df_daily['adto20'].replace(0, np.nan)
    
    candle_range = df_daily['high'] - df_daily['low']
    valid_range = candle_range > 0
    upper_shadow = np.zeros(len(df_daily))
    upper_shadow[valid_range] = (
        df_daily.loc[valid_range, 'high'] - df_daily.loc[valid_range, ['open', 'close']].max(axis=1)
    ) / candle_range[valid_range]
    df_daily['upper_shadow'] = upper_shadow
    
    all_dates = sorted([pd.to_datetime(d).strftime('%Y-%m-%d') for d in df_daily['date'].unique()])
    date_map = {d: i for i, d in enumerate(all_dates)}
    df_daily['date_str'] = pd.to_datetime(df_daily['date']).dt.strftime('%Y-%m-%d')
    df_daily['date_idx'] = df_daily['date_str'].map(date_map)
    daily_by_symbol = {sym: g.set_index('date_idx') for sym, g in df_daily.groupby('symbol')}
    
    return df_daily, daily_by_symbol, all_dates

daily_path = "data/dataset_2y/daily"

def test_grid(df_daily, daily_by_symbol, all_dates,
              min_to_b=70.0, min_gain=15.0, max_ushad=0.15,
              tp_target_pct=0.030, sl_pct=-0.025, trailing_buffer=0.010):
    
    min_to = min_to_b * 1e9
    anchors = df_daily[
        (df_daily['change_pct'] >= min_gain) &
        (df_daily['turnover'] >= min_to) &
        (df_daily['to_ratio'] >= 3.5) &
        (df_daily['upper_shadow'] <= max_ushad) &
        (df_daily['close'] > df_daily['open'])
    ]
    
    trades = []
    
    for _, a in anchors.iterrows():
        sym = a['symbol']
        a_idx = a['date_idx']
        d1_idx = a_idx + 1
        if d1_idx >= len(all_dates):
            continue
        d1_date = all_dates[d1_idx]
        sym_df = daily_by_symbol.get(sym)
        if sym_df is None or d1_idx not in sym_df.index:
            continue
            
        entry_p = a['close'] # Buy at Day 0 15:20
        d1_daily = sym_df.loc[d1_idx]
        
        # Check D+1 1-min parquet
        d_dir = d1_date.replace('-', '')
        fp = os.path.join(daily_path, d_dir, f"{sym}.parquet")
        if not os.path.exists(fp):
            # Fallback to daily bar
            pnl = (d1_daily['open'] - entry_p) / entry_p * 100.0 - 0.25
            trades.append({'date': d1_date, 'symbol': sym, 'pnl': pnl})
            continue
            
        try:
            mdf = pd.read_parquet(fp)
        except Exception:
            continue
        if len(mdf) < 15:
            continue
            
        mdf['time_str'] = mdf['timestamp'].astype(str).str.slice(11, 16)
        
        # Smart Step-Up Morning Exit (09:00~09:30)
        tp_target = entry_p * (1.0 + tp_target_pct)
        sl_p = entry_p * (1.0 + sl_pct)
        hwm = entry_p
        locked_tp = False
        pnl = None
        
        morning = mdf[mdf['time_str'] <= '09:30']
        for _, bar in morning.iterrows():
            if bar['high'] > hwm:
                hwm = bar['high']
            if hwm >= tp_target:
                locked_tp = True
                trail_sl = hwm * (1.0 - trailing_buffer)
                sl_p = max(sl_p, tp_target, trail_sl)
            if bar['low'] <= sl_p:
                exec_p = min(bar['open'], sl_p) if bar['open'] < sl_p else sl_p
                pnl = (exec_p - entry_p) / entry_p * 100.0 - 0.25
                break
                
        # If still holding at 09:30, exit at 09:30 close
        if pnl is None and len(morning) > 0:
            pnl = (morning.iloc[-1]['close'] - entry_p) / entry_p * 100.0 - 0.25
            
        if pnl is not None:
            trades.append({'date': d1_date, 'symbol': sym, 'pnl': pnl})
            
    df_t = pd.DataFrame(trades)
    if len(df_t) == 0:
        return None
        
    s = df_t['pnl']
    wins = s[s > 0]
    losses = s[s <= 0]
    wr = len(wins) / len(s) * 100.0
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else 999.0
    
    return {
        'to_b': min_to_b,
        'gain': min_gain,
        'ushad': max_ushad,
        'tp': tp_target_pct,
        'sl': sl_pct,
        'buf': trailing_buffer,
        'trades': len(s),
        'wr': wr,
        'pf': pf,
        'net_pnl': s.sum(),
        'avg_win': wins.mean() if len(wins) > 0 else 0,
        'avg_loss': losses.mean() if len(losses) > 0 else 0,
        'trades_df': df_t
    }

if __name__ == "__main__":
    df_daily, daily_by_symbol, all_dates = load_data()
    
    print("\n--- Running Grid Search on Parameters ---")
    results = []
    
    for to_b in [50.0, 70.0, 100.0]:
        for gain in [12.0, 15.0, 18.0]:
            for ushad in [0.15, 0.25]:
                for tp in [0.030, 0.040]:
                    r = test_grid(df_daily, daily_by_symbol, all_dates,
                                  min_to_b=to_b, min_gain=gain, max_ushad=ushad,
                                  tp_target_pct=tp, sl_pct=-0.025, trailing_buffer=0.010)
                    if r:
                        results.append(r)
                        print(f"TO>={to_b}B | Gain>={gain}% | Shadow<={ushad} | TP=+{tp*100:.0f}% -> "
                              f"Trades: {r['trades']} | WR: {r['wr']:.1f}% | PF: {r['pf']:.2f} | Net: {r['net_pnl']:+.1f}% | Win/Loss: {r['avg_win']:+.1f}% / {r['avg_loss']:.1f}%")
                        
    res_df = pd.DataFrame([{k: v for k, v in r.items() if k != 'trades_df'} for r in results])
    res_df = res_df.sort_values('pf', reverse=True)
    print("\n--- TOP 5 CONFIGURATIONS BY PROFIT FACTOR ---")
    print(res_df.head(5).to_string())
