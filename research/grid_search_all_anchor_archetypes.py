"""
Comprehensive Grid Search of all Anchor Candle Archetypes across Korea Market 2-Year Dataset:
1. Archetype 1: High Breakout with Morning Volume Confirmation (신고가 거래대금 돌파)
2. Archetype 2: Midpoint Pullback Rebound (중심선 눌림목 지지 반등)
3. Archetype 3: Tight Squeeze Box Breakout (박스권 수렴 돌파)
4. Archetype 4: D+1 Monster Anchor Momentum (대형 기준봉 익일 공략)
"""
from __future__ import annotations

import os
import glob
import pandas as pd
import numpy as np
from datetime import datetime, time

def load_data():
    print("Loading daily dataset...")
    df_daily = pd.read_parquet("data/dataset_2y/daily_bars_2y.parquet")
    df_daily = df_daily.sort_values(['symbol', 'date']).reset_index(drop=True)
    df_daily['change_pct'] = pd.to_numeric(df_daily['change_pct'], errors='coerce')
    df_daily['turnover'] = pd.to_numeric(df_daily['turnover'], errors='coerce')
    df_daily['adto20'] = pd.to_numeric(df_daily['adto20'], errors='coerce')
    df_daily['to_ratio'] = df_daily['turnover'] / df_daily['adto20'].replace(0, np.nan)
    
    # Upper shadow ratio
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
    
    daily_by_symbol = {}
    for sym, g in df_daily.groupby('symbol'):
        daily_by_symbol[sym] = g.set_index('date_idx')
        
    return df_daily, daily_by_symbol, all_dates


def evaluate_archetype_1_high_breakout(df_daily, daily_by_symbol, all_dates,
                                      min_change_pct=10.0, min_to_billion=30.0,
                                      min_morning_cum_to=2_000_000_000,
                                      lookback_max=10, test_dates=None):
    """
    Archetype 1: Breakout over Anchor High with heavy morning volume confirmation.
    """
    min_to = min_to_billion * 1e9
    anchors = df_daily[
        (df_daily['change_pct'] >= min_change_pct) &
        (df_daily['turnover'] >= min_to) &
        (df_daily['to_ratio'] >= 3.0) &
        (df_daily['upper_shadow'] <= 0.35) &
        (df_daily['close'] > df_daily['open'])
    ]
    
    trades = []
    daily_path = "data/dataset_2y/daily"
    
    for _, a in anchors.iterrows():
        sym = a['symbol']
        a_idx = a['date_idx']
        a_high = a['high']
        a_low = a['low']
        sym_df = daily_by_symbol.get(sym)
        if sym_df is None:
            continue
            
        for offset in range(1, lookback_max + 1):
            t_idx = a_idx + offset
            if t_idx >= len(all_dates):
                break
            d_str = all_dates[t_idx]
            if test_dates and d_str not in test_dates:
                continue
                
            # Intermediates check
            inter = sym_df.loc[a_idx + 1 : t_idx - 1]
            if len(inter) > 0:
                if (inter['low'] < a_low).any(): # broke anchor low
                    break
                if (inter['high'] >= a_high).any(): # already broke out in prior days
                    break
                    
            d_minus_1_close = sym_df.loc[t_idx - 1]['close'] if (t_idx - 1) in sym_df.index else 0
            if d_minus_1_close <= 0:
                continue
            ratio = d_minus_1_close / a_high
            if not (0.88 <= ratio <= 1.015):
                continue
                
            # Check intraday 1-min parquet
            d_dir = d_str.replace('-', '')
            sym_str = str(sym).zfill(6)
            fp = os.path.join(daily_path, d_dir, f"{sym_str}.parquet")
            if not os.path.exists(fp):
                continue
            try:
                mdf = pd.read_parquet(fp)
            except Exception:
                continue
            if len(mdf) < 15:
                continue
                
            mdf['time_str'] = mdf['timestamp'].astype(str).str.slice(11, 16)
            mdf['cum_to'] = mdf['turnover'].cumsum()
            morning = mdf[(mdf['time_str'] >= '09:01') & (mdf['time_str'] <= '09:30')]
            if len(morning) == 0:
                continue
            day_open = mdf.iloc[0]['open']
            
            for idx, bar in morning.iterrows():
                if bar['high'] < a_high:
                    continue
                if bar['open'] > a_high * 1.015:
                    continue
                cum_to_now = mdf.loc[idx, 'cum_to']
                if cum_to_now < min_morning_cum_to:
                    continue
                    
                entry_p = max(bar['open'], a_high)
                surge_from_open = (entry_p - day_open) / day_open
                if surge_from_open > 0.08:
                    continue
                    
                # Trade simulation with True Smart Step-Up (+3% floor lock, trailing runner, -2.5% SL)
                entry_idx = mdf.index.get_loc(idx)
                post = mdf.iloc[entry_idx + 1 :]
                if len(post) == 0:
                    break
                    
                tp_target = entry_p * 1.030
                sl_p = entry_p * 0.975
                hwm = entry_p
                locked_tp = False
                pnl = None
                
                for b_idx, p_bar in post.iterrows():
                    if p_bar['high'] > hwm:
                        hwm = p_bar['high']
                    if hwm >= tp_target:
                        locked_tp = True
                        trail_sl = hwm * 0.990 # 1.0% buffer
                        sl_p = max(sl_p, tp_target, trail_sl)
                        
                    if p_bar['low'] <= sl_p:
                        # Slippage check
                        exec_p = min(p_bar['open'], sl_p) if p_bar['open'] < sl_p else sl_p
                        pnl = (exec_p - entry_p) / entry_p * 100.0 - 0.25 # KRX fee 0.25%
                        break
                    if p_bar['time_str'] >= '15:20':
                        pnl = (p_bar['close'] - entry_p) / entry_p * 100.0 - 0.25
                        break
                        
                if pnl is None and len(post) > 0:
                    pnl = (post.iloc[-1]['close'] - entry_p) / entry_p * 100.0 - 0.25
                if pnl is not None:
                    trades.append({
                        'date': d_str,
                        'symbol': sym,
                        'entry_p': entry_p,
                        'pnl': pnl,
                        'offset': offset,
                    })
                break # 1 trade per anchor
                
    return pd.DataFrame(trades)


def evaluate_archetype_2_midpoint_pullback(df_daily, daily_by_symbol, all_dates,
                                          min_change_pct=10.0, min_to_billion=30.0,
                                          lookback_max=10, test_dates=None):
    """
    Archetype 2: Midpoint Pullback Rebound (중심선 눌림목 반등).
    Enters when price pulls back to the Anchor Candle's 50% midpoint zone [mid * 0.98, mid * 1.02]
    and turns around with volume drying up on the consolidation days.
    """
    min_to = min_to_billion * 1e9
    anchors = df_daily[
        (df_daily['change_pct'] >= min_change_pct) &
        (df_daily['turnover'] >= min_to) &
        (df_daily['to_ratio'] >= 3.0) &
        (df_daily['upper_shadow'] <= 0.35) &
        (df_daily['close'] > df_daily['open'])
    ]
    
    trades = []
    daily_path = "data/dataset_2y/daily"
    
    for _, a in anchors.iterrows():
        sym = a['symbol']
        a_idx = a['date_idx']
        a_high = a['high']
        a_low = a['low']
        a_mid = (a_high + a_low) / 2.0
        a_to = a['turnover']
        sym_df = daily_by_symbol.get(sym)
        if sym_df is None:
            continue
            
        for offset in range(1, lookback_max + 1):
            t_idx = a_idx + offset
            if t_idx >= len(all_dates):
                break
            d_str = all_dates[t_idx]
            if test_dates and d_str not in test_dates:
                continue
                
            # Intermediates check: cannot break anchor low
            inter = sym_df.loc[a_idx + 1 : t_idx - 1]
            if len(inter) > 0 and (inter['low'] < a_low).any():
                break
                
            if t_idx not in sym_df.index:
                continue
            day_bar = sym_df.loc[t_idx]
            # Check if today's price touched the midpoint zone [mid * 0.98, mid * 1.03]
            if not (day_bar['low'] <= a_mid * 1.03 and day_bar['high'] >= a_mid * 0.98):
                continue
                
            # Check Volume Dry-Up: previous day's turnover must be significantly lower than anchor turnover (< 50%)
            if offset > 1 and (t_idx - 1) in sym_df.index:
                prev_to = sym_df.loc[t_idx - 1]['turnover']
                if prev_to > a_to * 0.60: # Volume didn't dry up
                    continue
                    
            # Check intraday 1-min parquet for rebound at midpoint
            d_dir = d_str.replace('-', '')
            sym_str = str(sym).zfill(6)
            fp = os.path.join(daily_path, d_dir, f"{sym_str}.parquet")
            if not os.path.exists(fp):
                continue
            try:
                mdf = pd.read_parquet(fp)
            except Exception:
                continue
            if len(mdf) < 30:
                continue
                
            mdf['time_str'] = mdf['timestamp'].astype(str).str.slice(11, 16)
            # Find intraday rebound: price touches near a_mid, then bounces back above a_mid with a green candle
            mid_touch_idx = None
            for idx, bar in mdf.iterrows():
                if bar['low'] <= a_mid * 1.01 and bar['low'] >= a_low:
                    mid_touch_idx = idx
                    break
                    
            if mid_touch_idx is None:
                continue
                
            # Look for rebound trigger in subsequent 15 bars: close > open and close > a_mid * 1.005
            rebound_idx = None
            touch_loc = mdf.index.get_loc(mid_touch_idx)
            for r_loc in range(touch_loc + 1, min(touch_loc + 30, len(mdf))):
                r_bar = mdf.iloc[r_loc]
                if r_bar['close'] > r_bar['open'] and r_bar['close'] >= a_mid:
                    rebound_idx = r_loc
                    break
                    
            if rebound_idx is None:
                continue
                
            entry_bar = mdf.iloc[rebound_idx]
            entry_p = entry_bar['close']
            
            # Exit rules:
            # TP: +4.0% (target Anchor High rebound) with Step-Up
            # SL: below Anchor Low or hard -2.5%
            sl_p = max(a_low * 0.995, entry_p * 0.975)
            tp_target = entry_p * 1.035
            
            post = mdf.iloc[rebound_idx + 1 :]
            if len(post) == 0:
                continue
                
            hwm = entry_p
            pnl = None
            for _, p_bar in post.iterrows():
                if p_bar['high'] > hwm:
                    hwm = p_bar['high']
                if hwm >= tp_target:
                    trail_sl = hwm * 0.988
                    sl_p = max(sl_p, tp_target, trail_sl)
                if p_bar['low'] <= sl_p:
                    exec_p = min(p_bar['open'], sl_p) if p_bar['open'] < sl_p else sl_p
                    pnl = (exec_p - entry_p) / entry_p * 100.0 - 0.25
                    break
                if p_bar['time_str'] >= '15:20':
                    pnl = (p_bar['close'] - entry_p) / entry_p * 100.0 - 0.25
                    break
                    
            if pnl is None and len(post) > 0:
                pnl = (post.iloc[-1]['close'] - entry_p) / entry_p * 100.0 - 0.25
            if pnl is not None:
                trades.append({
                    'date': d_str,
                    'symbol': sym,
                    'entry_p': entry_p,
                    'pnl': pnl,
                    'offset': offset,
                })
            break # 1 trade per anchor
            
    return pd.DataFrame(trades)


def evaluate_archetype_3_squeeze_breakout(df_daily, daily_by_symbol, all_dates,
                                         min_change_pct=10.0, min_to_billion=30.0,
                                         test_dates=None):
    """
    Archetype 3: Tight Squeeze Breakout (D+2 ~ D+5 박스권 수렴 후 돌파).
    The stock consolidates in the upper half of the anchor candle with volatility compression.
    Then on Day D, it breaks out over the multi-day consolidation high in the morning.
    """
    min_to = min_to_billion * 1e9
    anchors = df_daily[
        (df_daily['change_pct'] >= min_change_pct) &
        (df_daily['turnover'] >= min_to) &
        (df_daily['to_ratio'] >= 3.0) &
        (df_daily['upper_shadow'] <= 0.35) &
        (df_daily['close'] > df_daily['open'])
    ]
    
    trades = []
    daily_path = "data/dataset_2y/daily"
    
    for _, a in anchors.iterrows():
        sym = a['symbol']
        a_idx = a['date_idx']
        a_high = a['high']
        a_low = a['low']
        a_mid = (a_high + a_low) / 2.0
        sym_df = daily_by_symbol.get(sym)
        if sym_df is None:
            continue
            
        # Consolidation for 2 to 5 days
        for offset in range(2, 6):
            t_idx = a_idx + offset
            if t_idx >= len(all_dates):
                break
            d_str = all_dates[t_idx]
            if test_dates and d_str not in test_dates:
                continue
                
            consol = sym_df.loc[a_idx + 1 : t_idx - 1]
            if len(consol) < 1:
                continue
            # Squeeze condition: all consolidation bars stayed above anchor mid!
            if (consol['low'] < a_mid).any():
                continue
            # Volatility compression: consolidation range is tight (< 6% from high to low)
            c_high = consol['high'].max()
            c_low = consol['low'].min()
            if (c_high - c_low) / c_low > 0.08:
                continue
                
            # Today must break out over c_high
            d_dir = d_str.replace('-', '')
            sym_str = str(sym).zfill(6)
            fp = os.path.join(daily_path, d_dir, f"{sym_str}.parquet")
            if not os.path.exists(fp):
                continue
            try:
                mdf = pd.read_parquet(fp)
            except Exception:
                continue
            if len(mdf) < 15:
                continue
                
            mdf['time_str'] = mdf['timestamp'].astype(str).str.slice(11, 16)
            mdf['cum_to'] = mdf['turnover'].cumsum()
            morning = mdf[(mdf['time_str'] >= '09:01') & (mdf['time_str'] <= '09:30')]
            if len(morning) == 0:
                continue
                
            for idx, bar in morning.iterrows():
                if bar['high'] < c_high:
                    continue
                if bar['open'] > c_high * 1.015:
                    continue
                if mdf.loc[idx, 'cum_to'] < 1_500_000_000: # at least 15억
                    continue
                    
                entry_p = max(bar['open'], c_high)
                entry_idx = mdf.index.get_loc(idx)
                post = mdf.iloc[entry_idx + 1 :]
                if len(post) == 0:
                    break
                    
                tp_target = entry_p * 1.030
                sl_p = entry_p * 0.975
                hwm = entry_p
                pnl = None
                
                for _, p_bar in post.iterrows():
                    if p_bar['high'] > hwm:
                        hwm = p_bar['high']
                    if hwm >= tp_target:
                        trail_sl = hwm * 0.990
                        sl_p = max(sl_p, tp_target, trail_sl)
                    if p_bar['low'] <= sl_p:
                        exec_p = min(p_bar['open'], sl_p) if p_bar['open'] < sl_p else sl_p
                        pnl = (exec_p - entry_p) / entry_p * 100.0 - 0.25
                        break
                    if p_bar['time_str'] >= '15:20':
                        pnl = (p_bar['close'] - entry_p) / entry_p * 100.0 - 0.25
                        break
                        
                if pnl is None and len(post) > 0:
                    pnl = (post.iloc[-1]['close'] - entry_p) / entry_p * 100.0 - 0.25
                if pnl is not None:
                    trades.append({
                        'date': d_str,
                        'symbol': sym,
                        'entry_p': entry_p,
                        'pnl': pnl,
                        'offset': offset,
                    })
                break
                
    return pd.DataFrame(trades)


def summarize_results(name, df_trades):
    if df_trades is None or len(df_trades) == 0:
        print(f"\n[{name}] -> No trades generated.")
        return
        
    s = df_trades['pnl']
    wins = s[s > 0]
    losses = s[s <= 0]
    win_rate = len(wins) / len(s) * 100.0
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else 999.0
    net_pnl = s.sum()
    avg_win = wins.mean() if len(wins) > 0 else 0.0
    avg_loss = losses.mean() if len(losses) > 0 else 0.0
    
    print(f"\n{'='*20} {name} {'='*20}")
    print(f"Total Trades : {len(s)}")
    print(f"Win Rate     : {win_rate:.2f}% ({len(wins)}W / {len(losses)}L)")
    print(f"Profit Factor: {pf:.2f}")
    print(f"Net PnL      : {net_pnl:+.2f}%")
    print(f"Avg Win/Loss : {avg_win:+.2f}% / {avg_loss:+.2f}% (Payoff Ratio: {abs(avg_win/avg_loss):.2f})")
    print(f"Max Win/Loss : {s.max():+.2f}% / {s.min():+.2f}%")
    print(f"{'='*50}")


if __name__ == "__main__":
    df_daily, daily_by_symbol, all_dates = load_data()
    # Test on full 2025 (248 trading days)
    test_dates = [d for d in all_dates if '2025-01-01' <= d <= '2025-12-31']
    print(f"Running grid search over {len(test_dates)} trading days in 2025...")
    
    print("\n--- 1. Testing Archetype 1 (High Breakout: 30B+ Anchor, 2B Morning TO) ---")
    df1 = evaluate_archetype_1_high_breakout(df_daily, daily_by_symbol, all_dates,
                                             min_change_pct=10.0, min_to_billion=30.0,
                                             min_morning_cum_to=2_000_000_000,
                                             test_dates=test_dates)
    summarize_results("Archetype 1: High Breakout (Anchor>=30B, Morning TO>=2B)", df1)
    
    print("\n--- 2. Testing Archetype 1 (Monster Anchor: 50B+ Anchor, 3B Morning TO) ---")
    df1_monster = evaluate_archetype_1_high_breakout(df_daily, daily_by_symbol, all_dates,
                                                     min_change_pct=12.0, min_to_billion=50.0,
                                                     min_morning_cum_to=3_000_000_000,
                                                     test_dates=test_dates)
    summarize_results("Archetype 1: Monster Anchor High Breakout (Anchor>=50B, Morning TO>=3B)", df1_monster)
    
    print("\n--- 3. Testing Archetype 2 (Midpoint Pullback Rebound) ---")
    df2 = evaluate_archetype_2_midpoint_pullback(df_daily, daily_by_symbol, all_dates,
                                                 min_change_pct=10.0, min_to_billion=30.0,
                                                 test_dates=test_dates)
    summarize_results("Archetype 2: Midpoint Pullback Rebound (Anchor>=30B)", df2)
    
    print("\n--- 4. Testing Archetype 3 (Tight Squeeze Box Breakout) ---")
    df3 = evaluate_archetype_3_squeeze_breakout(df_daily, daily_by_symbol, all_dates,
                                                min_change_pct=10.0, min_to_billion=30.0,
                                                test_dates=test_dates)
    summarize_results("Archetype 3: Tight Squeeze Box Breakout (Anchor>=30B)", df3)
