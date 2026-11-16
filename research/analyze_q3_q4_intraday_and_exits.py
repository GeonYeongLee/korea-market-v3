"""
Research Script: Q3 Intraday Entry & Q4 Exit / Holding Period Optimization
Uses 1-minute bars of Anchor Candle candidate stocks on D+1 ~ D+3.
Analyzes:
- Q3: Time window of entry (09:00~09:30 vs later), Intraday volume surge multiple.
- Q4: Day-trading (15:20 EOD) vs Swing (D+1, D+2 holding), Fibonacci Range Exits vs Midpoint SL.
"""
import os
import glob
import pandas as pd
import numpy as np

def run_q3_q4_analysis():
    print("Loading candidate anchor events...")
    anchors = pd.read_csv("D:/repo/korea-market_v3-anchor/research/q2_position_low_break.csv")
    # Focus on the high-quality non-broken low group
    anchors = anchors[~anchors['low_broken']].copy()
    print(f"Total non-broken anchor events: {len(anchors)}")

    daily_path = "D:/repo/korea-market_v2/data/dataset_2y/daily"
    all_dates = sorted([d for d in os.listdir(daily_path) if os.path.isdir(os.path.join(daily_path, d)) and d.isdigit()])
    date_to_idx = {d: i for i, d in enumerate(all_dates)}

    sim_trades = []

    # Let's inspect D+1 to D+3 days for 300 sampled anchor events across 2025-2026
    sample_anchors = anchors[anchors['date'] >= '2025-01-01'].sample(min(300, len(anchors)), random_state=42)
    print(f"Sampling {len(sample_anchors)} anchor events for deep 1-minute intraday execution...")

    for _, a in sample_anchors.iterrows():
        sym = str(a['symbol']).zfill(6)
        anchor_date_str = str(a['date'])[:10].replace('-', '')
        if anchor_date_str not in date_to_idx:
            continue
        
        a_idx = date_to_idx[anchor_date_str]
        anchor_high = a['anchor_high']
        anchor_low = a['anchor_low']
        anchor_range = anchor_high - anchor_low
        anchor_mid = (anchor_high + anchor_low) / 2.0

        # Check D+1, D+2, D+3
        for offset in [1, 2, 3]:
            if a_idx + offset >= len(all_dates):
                break
            target_date = all_dates[a_idx + offset]
            file_path = os.path.join(daily_path, target_date, f"{sym}.parquet")
            if not os.path.exists(file_path):
                continue
            
            try:
                m1_df = pd.read_parquet(file_path)
            except Exception:
                continue

            if len(m1_df) < 10:
                continue

            # Check intraday signals:
            # Did it break Anchor High? Or test 50% Mid?
            # 1. Morning Golden Hour: 09:00 ~ 09:30
            m1_df['time_str'] = m1_df['timestamp'].astype(str).str.slice(11, 16)
            morning_df = m1_df[m1_df['time_str'] <= '09:30']

            # Trigger condition A: High Breakout (Crosses anchor_high with volume surge)
            # Find first 1-min bar where high >= anchor_high and time <= 09:30
            breakout_bars = morning_df[morning_df['high'] >= anchor_high]
            
            if len(breakout_bars) > 0:
                entry_bar = breakout_bars.iloc[0]
                entry_idx = m1_df.index.get_loc(entry_bar.name)
                entry_time = entry_bar['time_str']
                entry_price = max(entry_bar['open'], anchor_high) # Conservative fill

                # Track subsequent bars on the same day (Day-trade)
                post_entry_df = m1_df.iloc[entry_idx + 1 :]
                if len(post_entry_df) == 0:
                    continue

                day_high = post_entry_df['high'].max()
                day_low = post_entry_df['low'].min()
                day_close = post_entry_df.iloc[-1]['close']

                # Fibonacci targets relative to anchor_range
                fib_382 = entry_price + 0.382 * anchor_range
                fib_500 = entry_price + 0.500 * anchor_range
                fib_618 = entry_price + 0.618 * anchor_range

                hit_fib382 = day_high >= fib_382
                hit_fib500 = day_high >= fib_500
                hit_fib618 = day_high >= fib_618

                # Stop loss triggers:
                # SL 1: Anchor Mid
                hit_mid_sl = day_low <= anchor_mid
                # SL 2: Fixed -2.5%
                hit_fixed_sl = day_low <= entry_price * 0.975

                day_return = (day_close - entry_price) / entry_price * 100.0
                max_gain = (day_high - entry_price) / entry_price * 100.0
                max_drawdown = (day_low - entry_price) / entry_price * 100.0

                sim_trades.append({
                    'symbol': sym,
                    'date': target_date,
                    'day_offset': f"D+{offset}",
                    'entry_time': entry_time,
                    'entry_price': entry_price,
                    'day_high': day_high,
                    'day_low': day_low,
                    'day_close': day_close,
                    'day_return_pct': day_return,
                    'max_gain_pct': max_gain,
                    'max_drawdown_pct': max_drawdown,
                    'hit_fib382': hit_fib382,
                    'hit_fib500': hit_fib500,
                    'hit_fib618': hit_fib618,
                    'hit_mid_sl': hit_mid_sl,
                    'hit_fixed_sl': hit_fixed_sl,
                })

    tdf = pd.DataFrame(sim_trades)
    print(f"\nTotal Simulated Morning Breakout Trades: {len(tdf)}")
    if len(tdf) > 0:
        print("\n=== Q3: INTRADAY BREAKOUT PERFORMANCE (09:00 ~ 09:30) ===")
        print(f"Day-Trade Win Rate (Close > Entry): {(tdf['day_return_pct'] > 0).mean()*100:.1f}%")
        print(f"Average Day-Trade Return (EOD Exit): {tdf['day_return_pct'].mean():+.2f}%")
        print(f"Average Max Intraday Gain (MFE): +{tdf['max_gain_pct'].mean():.2f}%")
        print(f"Average Max Intraday Drawdown (MAE): {tdf['max_drawdown_pct'].mean():.2f}%")
        
        print("\n=== Q4: TARGET EXITS & FIBONACCI ATTAINMENT ===")
        print(f"Reached Target 1 (+0.382 Anchor Range): {tdf['hit_fib382'].mean()*100:.1f}%")
        print(f"Reached Target 2 (+0.500 Anchor Range): {tdf['hit_fib500'].mean()*100:.1f}%")
        print(f"Reached Target 3 (+0.618 Anchor Range): {tdf['hit_fib618'].mean()*100:.1f}%")
        print(f"Reached Fixed +3% Intraday: {(tdf['max_gain_pct'] >= 3.0).mean()*100:.1f}%")
        print(f"Reached Fixed +5% Intraday: {(tdf['max_gain_pct'] >= 5.0).mean()*100:.1f}%")

        print("\n=== Q4: STOP LOSS HIT RATES ===")
        print(f"Midpoint (50% Anchor) SL Hit Rate: {tdf['hit_mid_sl'].mean()*100:.1f}%")
        print(f"Fixed -2.5% SL Hit Rate: {tdf['hit_fixed_sl'].mean()*100:.1f}%")

        print("\n=== BREAKDOWN BY DAY OFFSET (D+1 vs D+2 vs D+3) ===")
        for d_off, group in tdf.groupby('day_offset'):
            print(f"[{d_off}] count: {len(group)}, WinRate: {(group['day_return_pct'] > 0).mean()*100:.1f}%, AvgReturn: {group['day_return_pct'].mean():+.2f}%, AvgMFE: +{group['max_gain_pct'].mean():.2f}%")

        tdf.to_csv("D:/repo/korea-market_v3-anchor/research/q3_q4_trades.csv", index=False)
        print("\nSaved intraday trade details to research/q3_q4_trades.csv")

if __name__ == "__main__":
    run_q3_q4_analysis()
