"""
Research Script: State A (High Breakout) vs State B (1/2 Midpoint Pullback Rebound)
Directly answers the user's specific core question:
"현재가가 기준봉보다 높거나(A), 혹은 1/2 수준이거나(B), 아니면 저가 이탈(C)"
Tests both setups across the 2-year dataset.
"""
import os
import pandas as pd
import numpy as np

def run_comparison():
    print("Loading anchor events...")
    anchors = pd.read_csv("D:/repo/korea-market_v3-anchor/research/q2_position_low_break.csv")
    anchors['date_str'] = pd.to_datetime(anchors['date']).dt.strftime('%Y%m%d')

    daily_path = "D:/repo/korea-market_v3-anchor/data/dataset_2y/daily"
    all_dates = sorted([d for d in os.listdir(daily_path) if d.isdigit()])
    date_to_idx = {d: i for i, d in enumerate(all_dates)}

    # Filter to 2025 (representative year)
    test_dates = [d for d in all_dates if '20250102' <= d <= '20251231']

    trades_state_a = [] # Breakout over Anchor High
    trades_state_b = [] # Pullback to 1/2 Midpoint + Morning Rebound

    for d_str in test_dates:
        d_idx = date_to_idx.get(d_str)
        if not d_idx:
            continue

        # Look for anchors within past 1..3 days that did NOT break low
        past_dates = [k for k, v in date_to_idx.items() if d_idx - 3 <= v < d_idx]
        day_anchors = anchors[(anchors['date_str'].isin(past_dates)) & (~anchors['low_broken'])]

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
            a_low = a['anchor_low']
            a_mid = (a_high + a_low) / 2.0
            a_range = a_high - a_low

            df['time_str'] = df['timestamp'].astype(str).str.slice(11, 16)
            morning = df[(df['time_str'] >= '09:01') & (df['time_str'] <= '09:30')]
            if len(morning) == 0:
                continue

            day_open = df.iloc[0]['open']

            # ----------------------------------------------------
            # STATE A: Breakout over Anchor High
            # ----------------------------------------------------
            # Stock was below anchor high, then breaks out in morning with volume
            if day_open <= a_high * 1.015:
                brk_bars = morning[morning['high'] >= a_high]
                if len(brk_bars) > 0:
                    b_bar = brk_bars.iloc[0]
                    entry_p = max(b_bar['open'], a_high)
                    entry_idx = df.index.get_loc(b_bar.name)
                    post_df = df.iloc[entry_idx + 1 :]
                    if len(post_df) > 0:
                        day_h = post_df['high'].max()
                        day_l = post_df['low'].min()
                        day_c = post_df.iloc[-1]['close']
                        sl_p = entry_p * 0.965 # -3.5% SL
                        tp_p = entry_p * 1.035 # +3.5% TP
                        
                        hit_sl = day_l <= sl_p
                        hit_tp = day_h >= tp_p
                        if hit_sl and hit_tp:
                            pnl = (sl_p - entry_p) / entry_p * 100 - 0.25
                        elif hit_sl:
                            pnl = (sl_p - entry_p) / entry_p * 100 - 0.25
                        elif hit_tp:
                            pnl = (tp_p - entry_p) / entry_p * 100 - 0.25
                        else:
                            pnl = (day_c - entry_p) / entry_p * 100 - 0.25
                        trades_state_a.append({
                            'symbol': sym, 'date': d_str, 'pnl': pnl, 'win': 1 if pnl > 0 else 0,
                            'mfe': (day_h - entry_p) / entry_p * 100, 'mae': (day_l - entry_p) / entry_p * 100
                        })

            # ----------------------------------------------------
            # STATE B: 1/2 Midpoint Pullback Rebound (눌림목 반등)
            # ----------------------------------------------------
            # Current price is near Anchor Mid (a_mid +- 2.5%), and morning bar breaks up +2.5% from low
            if (a_mid * 0.96 <= day_open <= a_mid * 1.04):
                # Look for morning upward reversal: bar surges +2.5% from day open with volume >= 200M KRW
                rev_bars = morning[(morning['high'] >= day_open * 1.025) & (morning['turnover'] >= 200_000_000)]
                if len(rev_bars) > 0:
                    r_bar = rev_bars.iloc[0]
                    entry_p = max(r_bar['open'], day_open * 1.025)
                    entry_idx = df.index.get_loc(r_bar.name)
                    post_df = df.iloc[entry_idx + 1 :]
                    if len(post_df) > 0:
                        day_h = post_df['high'].max()
                        day_l = post_df['low'].min()
                        day_c = post_df.iloc[-1]['close']
                        # SL: if price breaks below anchor low or -3.5%
                        sl_p = max(a_low, entry_p * 0.965)
                        tp_p = entry_p * 1.040 # +4.0% TP (towards anchor high)

                        hit_sl = day_l <= sl_p
                        hit_tp = day_h >= tp_p
                        if hit_sl and hit_tp:
                            pnl = (sl_p - entry_p) / entry_p * 100 - 0.25
                        elif hit_sl:
                            pnl = (sl_p - entry_p) / entry_p * 100 - 0.25
                        elif hit_tp:
                            pnl = (tp_p - entry_p) / entry_p * 100 - 0.25
                        else:
                            pnl = (day_c - entry_p) / entry_p * 100 - 0.25
                        trades_state_b.append({
                            'symbol': sym, 'date': d_str, 'pnl': pnl, 'win': 1 if pnl > 0 else 0,
                            'mfe': (day_h - entry_p) / entry_p * 100, 'mae': (day_l - entry_p) / entry_p * 100
                        })

    df_a = pd.DataFrame(trades_state_a)
    df_b = pd.DataFrame(trades_state_b)

    print("\n=======================================================")
    print("   📊 COMPARISON: STATE A (고가 돌파) vs STATE B (1/2 중심선 반등)")
    print("=======================================================")
    print(f"[STATE A: 기준봉 고가 돌파 추격 (2025 전수)]")
    print(f"  - Total Trades: {len(df_a)}")
    print(f"  - Win Rate: {df_a['win'].mean()*100:.1f}%")
    print(f"  - Total Net PnL (sum %): {df_a['pnl'].sum():+.2f}%")
    print(f"  - Average PnL per trade: {df_a['pnl'].mean():+.2f}%")
    print(f"  - Average MFE: +{df_a['mfe'].mean():.2f}% | Average MAE: {df_a['mae'].mean():.2f}%")

    print(f"\n[STATE B: 기준봉 1/2 중심선 눌림목 반등 (2025 전수)]")
    print(f"  - Total Trades: {len(df_b)}")
    print(f"  - Win Rate: {df_b['win'].mean()*100:.1f}%")
    print(f"  - Total Net PnL (sum %): {df_b['pnl'].sum():+.2f}%")
    print(f"  - Average PnL per trade: {df_b['pnl'].mean():+.2f}%")
    print(f"  - Average MFE: +{df_b['mfe'].mean():.2f}% | Average MAE: {df_b['mae'].mean():.2f}%")

if __name__ == "__main__":
    run_comparison()
