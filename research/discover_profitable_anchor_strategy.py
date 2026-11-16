"""
Comprehensive Discovery Script: Finding the Truly Profitable Anchor Candle Strategy.
Tests:
1. Model 1: Daily Swing (D+1 Open/Pullback buy -> hold 1~5 days)
2. Model 2: Day-Trade Pullback to Key Support (Anchor 50% Midpoint / 38.2% Fibonacci)
3. Model 3: Mega Anchor (Turnover >= 1,000억 KRW, 100B) Fresh Breakout (D+1 to D+3 only)
4. Model 4: Anchor Candle Close Betting (종가베팅) -> Next Day Morning Exit
5. Model 5: 20-EMA Golden Squeeze Breakout (이평선 밀집 후 기준봉 돌파)
"""
from __future__ import annotations

import os
import glob
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
    
    # 20 EMA and 60 EMA
    df_daily['ema20'] = df_daily.groupby('symbol')['close'].transform(lambda x: x.ewm(span=20, adjust=False).mean())
    df_daily['ema5'] = df_daily.groupby('symbol')['close'].transform(lambda x: x.ewm(span=5, adjust=False).mean())
    
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


def run_model_1_daily_swing(df_daily, daily_by_symbol, all_dates):
    """
    Model 1: Daily Swing (기준봉 익일 종가/눌림 매수 -> 2~5일 스윙 홀딩).
    Condition:
      - Day 0: Mega Anchor Candle (+12%~+30%, Turnover >= 500억 KRW, turnover ratio >= 4x, upper shadow <= 0.25)
      - Day 1: Pullback day! Low reaches into anchor upper body, volume dries up (< 40% of Day 0).
      - Entry: Buy at Day 1 close (or Day 2 open).
      - Exit: Sell at Day 2~5 when price crosses Day 0 High (+5%~+10%) or stop loss at Day 0 Midpoint (-3%).
    """
    print("\n" + "="*50)
    print("Testing Model 1: Mega Anchor Daily Swing (1~5 Days)")
    print("="*50)
    
    anchors = df_daily[
        (df_daily['change_pct'] >= 12.0) &
        (df_daily['turnover'] >= 50e9) & # 500억 이상
        (df_daily['to_ratio'] >= 3.5) &
        (df_daily['upper_shadow'] <= 0.25) &
        (df_daily['close'] > df_daily['open']) &
        (df_daily['close'] > df_daily['ema20']) # 정배열
    ]
    
    trades = []
    for _, a in anchors.iterrows():
        sym = a['symbol']
        a_idx = a['date_idx']
        a_high = a['high']
        a_low = a['low']
        a_close = a['close']
        a_mid = (a_high + a_low) / 2.0
        a_to = a['turnover']
        
        sym_df = daily_by_symbol.get(sym)
        if sym_df is None:
            continue
            
        # Day 1 (first day after anchor): check for healthy pullback (거래량 감소 눌림목)
        d1_idx = a_idx + 1
        if d1_idx not in sym_df.index:
            continue
        d1 = sym_df.loc[d1_idx]
        
        # Pullback condition: low stayed above anchor mid, volume dried up (< 50% of anchor)
        if d1['low'] < a_mid or d1['turnover'] > a_to * 0.50:
            continue
            
        # Entry at D1 Close
        entry_p = d1['close']
        sl_p = a_mid * 0.985 # Stop loss: just below anchor mid (tight defense)
        tp_target = a_high * 1.02 # Target: breakout over anchor high
        
        # Hold for max 4 trading days (D2 to D5)
        pnl = None
        for hold_offset in range(2, 6):
            h_idx = a_idx + hold_offset
            if h_idx not in sym_df.index:
                break
            h_bar = sym_df.loc[h_idx]
            
            # Did it hit SL?
            if h_bar['low'] <= sl_p:
                exec_p = min(h_bar['open'], sl_p)
                pnl = (exec_p - entry_p) / entry_p * 100.0 - 0.25
                break
            # Did it hit TP?
            if h_bar['high'] >= tp_target:
                exec_p = max(h_bar['open'], tp_target)
                pnl = (exec_p - entry_p) / entry_p * 100.0 - 0.25
                break
                
        # Time exit at D5 close if not hit
        if pnl is None and (a_idx + 5) in sym_df.index:
            end_bar = sym_df.loc[a_idx + 5]
            pnl = (end_bar['close'] - entry_p) / entry_p * 100.0 - 0.25
            
        if pnl is not None:
            trades.append(pnl)
            
    res = pd.Series(trades)
    print_stats("Model 1: Mega Anchor Daily Swing", res)


def run_model_2_close_betting(df_daily, daily_by_symbol, all_dates):
    """
    Model 2: Anchor Candle Close Betting (기준봉 종가베팅 -> 익일 시초가/장초반 매도).
    Condition:
      - Day 0: Super Monster Anchor Candle (+15%~+29.9%, Turnover >= 700억 KRW, 거래대금 폭발, 윗꼬리 <= 0.15 극상단 마감).
      - Entry: Buy at 15:20 (Day 0 Close).
      - Exit: Sell on Day 1 morning (09:00~09:10 gap-up/surge peak, trailing stop or +3.0% TP).
    """
    print("\n" + "="*50)
    print("Testing Model 2: Anchor Day Close Betting (종가베팅 -> D+1 시초가 매도)")
    print("="*50)
    
    anchors = df_daily[
        (df_daily['change_pct'] >= 15.0) &
        (df_daily['turnover'] >= 70e9) & # 700억 이상
        (df_daily['to_ratio'] >= 4.0) &
        (df_daily['upper_shadow'] <= 0.15) & # 윗꼬리 15% 이하 (꽉 찬 장대양봉)
        (df_daily['close'] > df_daily['open'])
    ]
    
    trades = []
    daily_path = "data/dataset_2y/daily"
    
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
            
        entry_p = a['close'] # Buy at Day 0 close
        
        # Check D+1 intraday 1-min parquet
        d_dir = d1_date.replace('-', '')
        sym_str = str(sym).zfill(6)
        fp = os.path.join(daily_path, d_dir, f"{sym_str}.parquet")
        if not os.path.exists(fp):
            # Fallback to daily bar
            d1_bar = sym_df.loc[d1_idx]
            pnl = (d1_bar['open'] - entry_p) / entry_p * 100.0 - 0.25
            trades.append(pnl)
            continue
            
        try:
            mdf = pd.read_parquet(fp)
        except Exception:
            continue
        if len(mdf) < 15:
            continue
            
        mdf['time_str'] = mdf['timestamp'].astype(str).str.slice(11, 16)
        # Sell in morning 09:00~09:30 with Smart Step-Up:
        # TP: +3.0% with trailing stop, SL: -2.0%
        tp_target = entry_p * 1.030
        sl_p = entry_p * 0.980
        hwm = entry_p
        pnl = None
        
        morning = mdf[mdf['time_str'] <= '09:30']
        for _, bar in morning.iterrows():
            if bar['high'] > hwm:
                hwm = bar['high']
            if hwm >= tp_target:
                trail_sl = hwm * 0.990
                sl_p = max(sl_p, tp_target, trail_sl)
            if bar['low'] <= sl_p:
                exec_p = min(bar['open'], sl_p) if bar['open'] < sl_p else sl_p
                pnl = (exec_p - entry_p) / entry_p * 100.0 - 0.25
                break
                
        # If not exited by 09:30, sell at 09:30 close
        if pnl is None and len(morning) > 0:
            pnl = (morning.iloc[-1]['close'] - entry_p) / entry_p * 100.0 - 0.25
        if pnl is not None:
            trades.append(pnl)
            
    res = pd.Series(trades)
    print_stats("Model 2: Anchor Day Close Betting -> D+1 Morning Exit", res)


def run_model_3_mega_anchor_fresh_breakout(df_daily, daily_by_symbol, all_dates):
    """
    Model 3: Mega Anchor (1,000억+ KRW) Fresh High Breakout (D+1 to D+3 only).
    Key differences from naive breakout:
      1. Anchor turnover must be >= 1,000억 KRW (100 Billion KRW) - ONLY true institutional market movers.
      2. Must be FRESH: breakout must occur within D+1 to D+3 (no stale 15-day consolidations).
      3. Morning turnover at breakout must be >= 30억 KRW (3 Billion).
      4. True Smart Step-Up exit.
    """
    print("\n" + "="*50)
    print("Testing Model 3: Mega Anchor (1,000억+) Fresh Breakout (D+1~D+3)")
    print("="*50)
    
    anchors = df_daily[
        (df_daily['change_pct'] >= 10.0) &
        (df_daily['turnover'] >= 100e9) & # 1000억 이상!
        (df_daily['to_ratio'] >= 3.0) &
        (df_daily['upper_shadow'] <= 0.30) &
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
            
        for offset in range(1, 4): # D+1 to D+3 only!
            t_idx = a_idx + offset
            if t_idx >= len(all_dates):
                break
            d_str = all_dates[t_idx]
            
            inter = sym_df.loc[a_idx + 1 : t_idx - 1]
            if len(inter) > 0:
                if (inter['low'] < a_low).any():
                    break
                if (inter['high'] >= a_high).any():
                    break
                    
            d_minus_1_close = sym_df.loc[t_idx - 1]['close'] if (t_idx - 1) in sym_df.index else 0
            if d_minus_1_close <= 0 or (d_minus_1_close / a_high < 0.90):
                continue
                
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
                if mdf.loc[idx, 'cum_to'] < 2_500_000_000: # 25억 이상
                    continue
                    
                entry_p = max(bar['open'], a_high)
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
                    trades.append(pnl)
                break
                
    res = pd.Series(trades)
    print_stats("Model 3: Mega Anchor Fresh Breakout (100B+, D+1~D+3)", res)


def run_model_4_vwap_pullback_leader(df_daily, daily_by_symbol, all_dates):
    """
    Model 4: Anchor Leader Morning Dip Buy (기준봉 출현 후 당일 시초가 눌림목 매수).
    Instead of buying at the top when price breaks Anchor High:
    When a fresh Anchor stock opens with a slight gap (+0.5% ~ +2.5%) and dips to Day Open / VWAP:
    Buy the dip at Open with SL at Day Low (-1.5%) and target +3.5%!
    """
    print("\n" + "="*50)
    print("Testing Model 4: Anchor Leader Morning Dip Buy (시초가 눌림 반등)")
    print("="*50)
    
    anchors = df_daily[
        (df_daily['change_pct'] >= 10.0) &
        (df_daily['turnover'] >= 50e9) &
        (df_daily['to_ratio'] >= 3.0) &
        (df_daily['upper_shadow'] <= 0.30) &
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
            
        # Fresh days: D+1 or D+2
        for offset in [1, 2]:
            t_idx = a_idx + offset
            if t_idx >= len(all_dates):
                break
            d_str = all_dates[t_idx]
            
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
            day_open = mdf.iloc[0]['open']
            prev_close = a['close'] if offset == 1 else (sym_df.loc[t_idx - 1]['close'] if (t_idx - 1) in sym_df.index else 0)
            if prev_close <= 0:
                continue
            open_gap = (day_open - prev_close) / prev_close
            # Positive gap between 0% and +3.5%
            if not (0.00 <= open_gap <= 0.035):
                continue
                
            # Look for morning dip between 09:03 and 09:20:
            # Price dips to day_open * 0.995 ~ 1.005, then bounces with a green bar
            m20 = mdf[(mdf['time_str'] >= '09:03') & (mdf['time_str'] <= '09:20')]
            dip_idx = None
            for idx, bar in m20.iterrows():
                # Dips near open and prints green bar with close > open
                if bar['low'] <= day_open * 1.005 and bar['close'] > bar['open'] and bar['close'] >= day_open:
                    dip_idx = idx
                    break
                    
            if dip_idx is None:
                continue
                
            entry_p = mdf.loc[dip_idx, 'close']
            entry_loc = mdf.index.get_loc(dip_idx)
            post = mdf.iloc[entry_loc + 1 :]
            if len(post) == 0:
                continue
                
            # Exit: SL -2.0%, TP +3.5% with trailing
            sl_p = entry_p * 0.980 # -2.0% tight SL
            tp_target = entry_p * 1.030
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
                trades.append(pnl)
            break
            
    res = pd.Series(trades)
    print_stats("Model 4: Anchor Leader Morning Dip Buy", res)


def print_stats(name, s):
    if len(s) == 0:
        print(f"[{name}] No trades.")
        return
    wins = s[s > 0]
    losses = s[s <= 0]
    wr = len(wins) / len(s) * 100.0
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else 999.0
    print(f"\n{'='*20} {name} {'='*20}")
    print(f"Trades: {len(s)} | Win Rate: {wr:.1f}% ({len(wins)}W / {len(losses)}L)")
    print(f"Profit Factor: {pf:.2f} | Net PnL: {s.sum():+.2f}%")
    print(f"Avg Win: +{wins.mean():.2f}% | Avg Loss: {losses.mean():.2f}%")
    print(f"{'='*50}")


if __name__ == "__main__":
    df_daily, daily_by_symbol, all_dates = load_data()
    run_model_1_daily_swing(df_daily, daily_by_symbol, all_dates)
    run_model_2_close_betting(df_daily, daily_by_symbol, all_dates)
    run_model_3_mega_anchor_fresh_breakout(df_daily, daily_by_symbol, all_dates)
    run_model_4_vwap_pullback_leader(df_daily, daily_by_symbol, all_dates)
