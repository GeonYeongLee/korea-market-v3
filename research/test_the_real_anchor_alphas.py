"""
Quantitative Research: Testing the Real Anchor Candle Alphas in Korean Market:
1. Strategy Alpha 1: D+1 Morning Momentum on Monster Anchor (1000억+ 대형 기준봉 익일 공략)
2. Strategy Alpha 2: 2nd Wave Re-Ignition (거래량 급감 눌림 후 당일 거래대금 재폭발 돌파)
3. Strategy Alpha 3: Morning Leader + Anchor Breakout Alignment (당일 주도주 x 기준봉 돌파 결합)
"""
import os
import glob
import pandas as pd
import numpy as np

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

daily_path = "data/dataset_2y/daily"


def test_alpha_1_d_plus_1(df_daily, daily_by_symbol, all_dates, min_turnover_billion=50.0):
    """
    Alpha 1: Monster Anchor Candle D+1 Morning Momentum.
    Day 0: Massive Anchor Candle (+10%~+30%, turnover >= 50B KRW, upper shadow <= 0.25).
    Day 1 (D+1): Trade morning continuation (open gap <= +3.5%, buy on morning breakout over Day 0 close or 09:05 high).
    """
    print(f"\n--- Testing Alpha 1: D+1 Monster Anchor Momentum (Turnover >= {min_turnover_billion}B) ---")
    min_to = min_turnover_billion * 1e9
    anchors = df_daily[
        (df_daily['change_pct'] >= 10.0) &
        (df_daily['turnover'] >= min_to) &
        (df_daily['to_ratio'] >= 3.0) &
        (df_daily['upper_shadow'] <= 0.30) &
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
        if not ('2025-01-01' <= d1_date <= '2025-12-31'):
            continue
            
        sym_df = daily_by_symbol.get(sym)
        if sym_df is None or d1_idx not in sym_df.index:
            continue
            
        d1_daily = sym_df.loc[d1_idx]
        d0_close = a['close']
        d0_high = a['high']
        
        # Open gap filter: open between -1.0% and +3.5%
        open_gap = (d1_daily['open'] - d0_close) / d0_close
        if open_gap < -0.02 or open_gap > 0.04:
            continue
            
        d_dir = d1_date.replace('-', '')
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
        morning = mdf[(mdf['time_str'] >= '09:01') & (mdf['time_str'] <= '09:30')]
        if len(morning) == 0:
            continue
            
        # Entry trigger: price crosses max(Day 0 High, Day 1 Open * 1.01) with morning volume
        trigger_p = max(d0_close * 1.01, d0_high)
        for idx, bar in morning.iterrows():
            if bar['high'] < trigger_p:
                continue
            if bar['open'] > trigger_p * 1.015:
                continue
                
            entry_p = max(bar['open'], trigger_p)
            entry_idx = mdf.index.get_loc(idx)
            post = mdf.iloc[entry_idx + 1 :]
            if len(post) == 0:
                break
                
            # Smart Step-Up exit: +3.0% floor lock, trailing stop, -2.5% SL
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
    print_stats("Alpha 1 (D+1 Monster Momentum)", res)


def test_alpha_2_reignition(df_daily, daily_by_symbol, all_dates, min_turnover_billion=50.0):
    """
    Alpha 2: 2nd Wave Re-Ignition (기준봉 2차 파동 재폭발).
    - Anchor Candle: Day 0, Turnover >= 50B KRW, Gain >= +12%
    - Days 1~5: Volume dries up (< 35% of Anchor turnover), price holds above Anchor Midpoint (50% Fibonacci).
    - Day T (D+2 ~ D+6): Morning (09:00~09:30) volume spikes to >= 3.0 billion KRW and breaks out over Yesterday's High!
    """
    print(f"\n--- Testing Alpha 2: 2nd Wave Re-Ignition (Turnover >= {min_turnover_billion}B) ---")
    min_to = min_turnover_billion * 1e9
    anchors = df_daily[
        (df_daily['change_pct'] >= 12.0) &
        (df_daily['turnover'] >= min_to) &
        (df_daily['to_ratio'] >= 3.0) &
        (df_daily['upper_shadow'] <= 0.30) &
        (df_daily['close'] > df_daily['open'])
    ]
    
    trades = []
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
            
        for offset in range(2, 7):
            t_idx = a_idx + offset
            if t_idx >= len(all_dates):
                break
            d_str = all_dates[t_idx]
            if not ('2025-01-01' <= d_str <= '2025-12-31'):
                continue
                
            consol = sym_df.loc[a_idx + 1 : t_idx - 1]
            if len(consol) == 0:
                continue
            # 1. Price held above Anchor Midpoint!
            if (consol['low'] < a_mid).any():
                continue
            # 2. Volume dried up on consolidation days! (average turnover < 40% of anchor)
            if consol['turnover'].mean() > a_to * 0.40:
                continue
                
            prev_day_high = consol.iloc[-1]['high']
            
            # 3. Check today's 1-min parquet: volume explosion over prev_day_high
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
                if bar['high'] < prev_day_high:
                    continue
                if bar['open'] > prev_day_high * 1.015:
                    continue
                cum_to_now = mdf.loc[idx, 'cum_to']
                # Heavy re-ignition turnover: at least 2 billion in first 30 mins
                if cum_to_now < 2_000_000_000:
                    continue
                    
                entry_p = max(bar['open'], prev_day_high)
                entry_idx = mdf.index.get_loc(idx)
                post = mdf.iloc[entry_idx + 1 :]
                if len(post) == 0:
                    break
                    
                tp_target = entry_p * 1.035
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
    print_stats("Alpha 2 (2nd Wave Re-Ignition)", res)


def test_alpha_3_leader_anchor_alignment(df_daily, daily_by_symbol, all_dates):
    """
    Alpha 3: Morning Market Leader + Anchor Structural Alignment.
    Take the actual top morning leaders of the day (first 15~30 mins):
    Filter for leaders that are breaking out of a recent (D-1 to D-10) Anchor Candle (+10%, 30B+).
    """
    print(f"\n--- Testing Alpha 3: Morning Leader x Anchor Breakout Alignment ---")
    min_to = 30e9
    anchors = df_daily[
        (df_daily['change_pct'] >= 10.0) &
        (df_daily['turnover'] >= min_to) &
        (df_daily['to_ratio'] >= 3.0) &
        (df_daily['close'] > df_daily['open'])
    ]
    # Build fast lookup: (symbol, target_date_idx) -> list of valid anchor_highs
    anchor_lookup = {}
    for _, a in anchors.iterrows():
        sym = a['symbol']
        a_idx = a['date_idx']
        a_high = a['high']
        a_low = a['low']
        sym_df = daily_by_symbol.get(sym)
        if sym_df is None:
            continue
        for offset in range(1, 11):
            t_idx = a_idx + offset
            if t_idx >= len(all_dates):
                break
            inter = sym_df.loc[a_idx + 1 : t_idx - 1]
            if len(inter) > 0 and (inter['low'] < a_low).any():
                break
            key = (sym, t_idx)
            if key not in anchor_lookup:
                anchor_lookup[key] = []
            anchor_lookup[key].append(a_high)
            
    # For all trading days in 2025:
    test_dates = [d for d in all_dates if '2025-01-01' <= d <= '2025-12-31']
    trades = []
    
    for d_str in test_dates:
        d_dir = d_str.replace('-', '')
        d_idx = all_dates.index(d_str)
        day_dir = os.path.join(daily_path, d_dir)
        if not os.path.exists(day_dir):
            continue
            
        # Read parquet files for that day, find top morning turnover stocks at 09:15
        day_candidates = []
        for p_file in glob.glob(os.path.join(day_dir, "*.parquet")):
            sym_str = os.path.splitext(os.path.basename(p_file))[0]
            try:
                sym_int = int(sym_str)
            except ValueError:
                continue
            key = (sym_int, d_idx)
            if key not in anchor_lookup:
                continue
            valid_highs = anchor_lookup[key]
            
            try:
                mdf = pd.read_parquet(p_file)
            except Exception:
                continue
            if len(mdf) < 15:
                continue
            mdf['time_str'] = mdf['timestamp'].astype(str).str.slice(11, 16)
            mdf['cum_to'] = mdf['turnover'].cumsum()
            m15 = mdf[mdf['time_str'] <= '09:15']
            if len(m15) == 0:
                continue
            to_0915 = m15.iloc[-1]['cum_to']
            if to_0915 < 3_000_000_000: # must have at least 30억 in first 15 mins (True Leader!)
                continue
            day_open = mdf.iloc[0]['open']
            
            # Check if breaking any anchor high
            for a_high in valid_highs:
                for idx, bar in m15.iterrows():
                    if bar['high'] >= a_high and bar['open'] <= a_high * 1.015:
                        entry_p = max(bar['open'], a_high)
                        surge = (entry_p - day_open) / day_open
                        if 0.01 <= surge <= 0.08:
                            day_candidates.append({
                                'symbol': sym_int,
                                'to_0915': to_0915,
                                'entry_p': entry_p,
                                'idx': idx,
                                'mdf': mdf,
                            })
                            break
                if len(day_candidates) > 0 and day_candidates[-1]['symbol'] == sym_int:
                    break
                    
        # Pick top 2 leaders with highest 09:15 turnover
        if len(day_candidates) == 0:
            continue
        day_candidates.sort(key=lambda x: x['to_0915'], reverse=True)
        selected = day_candidates[:2]
        
        for cand in selected:
            mdf = cand['mdf']
            entry_idx = mdf.index.get_loc(cand['idx'])
            entry_p = cand['entry_p']
            post = mdf.iloc[entry_idx + 1 :]
            if len(post) == 0:
                continue
                
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
                
    res = pd.Series(trades)
    print_stats("Alpha 3 (Morning Leader x Anchor Breakout)", res)


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
    test_alpha_1_d_plus_1(df_daily, daily_by_symbol, all_dates, min_turnover_billion=50.0)
    test_alpha_2_reignition(df_daily, daily_by_symbol, all_dates, min_turnover_billion=50.0)
    test_alpha_3_leader_anchor_alignment(df_daily, daily_by_symbol, all_dates)
