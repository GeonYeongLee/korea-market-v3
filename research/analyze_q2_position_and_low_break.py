"""
Research Script: Q2 Price Position & Anchor Low Break Recovery Analysis
Tracks behavior of stocks on D+1 to D+5 after Anchor Candle:
- Where does price trade (Above High, 50% Mid, Below Low)?
- If Anchor Low is broken, what % ever recover to High or Mid?
- MFE/MAE comparison: Low-broken stocks vs Non-broken stocks.
"""
import pandas as pd
import numpy as np

def run_q2_analysis():
    print("Loading daily bars for Q2 analysis...")
    df = pd.read_parquet("D:/repo/korea-market_v2/data/dataset_2y/daily_bars_2y.parquet")
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values(['symbol', 'date']).reset_index(drop=True)

    # Calculate candle attributes
    candle_range = df['high'] - df['low']
    valid = candle_range > 0
    upper_shadow = np.zeros(len(df))
    upper_shadow[valid] = (df.loc[valid, 'high'] - df.loc[valid, ['open', 'close']].max(axis=1)) / candle_range[valid]

    turnover_ratio = df['turnover'] / (df['adto20'] + 1e-6)

    # Base Anchor condition: >= +15%, turnover >= 5x, upper shadow <= 25%, turnover >= 10B KRW
    is_anchor = (
        (df['change_pct'] >= 15.0) &
        (turnover_ratio >= 5.0) &
        (upper_shadow <= 0.25) &
        (df['close'] > df['open']) &
        (df['turnover'] >= 10_000_000_000)
    )

    anchor_indices = df[is_anchor].index.tolist()
    print(f"Total Anchor Candle Events: {len(anchor_indices)}")

    records = []

    # Map symbol to indices for quick lookup
    symbol_groups = {sym: group.index.values for sym, group in df.groupby('symbol')}

    for idx in anchor_indices:
        row = df.iloc[idx]
        sym = row['symbol']
        anchor_date = row['date']
        anchor_high = row['high']
        anchor_low = row['low']
        anchor_open = row['open']
        anchor_close = row['close']
        anchor_mid = (anchor_high + anchor_low) / 2.0 # Midpoint of range
        anchor_body_mid = (anchor_open + anchor_close) / 2.0

        sym_indices = symbol_groups[sym]
        pos_in_group = np.searchsorted(sym_indices, idx)

        # Track next 5 trading days
        future_indices = sym_indices[pos_in_group + 1 : pos_in_group + 6]
        if len(future_indices) == 0:
            continue

        future_df = df.iloc[future_indices]
        
        # Check if anchor low was broken at any point in next 5 days
        low_broken = (future_df['low'] < anchor_low).any()
        
        # When was it broken?
        first_break_day = None
        if low_broken:
            broken_mask = (future_df['low'] < anchor_low)
            first_break_day = np.where(broken_mask)[0][0] + 1 # 1-indexed (Day 1..5)

        # Max high achieved in next 5 days
        max_future_high = future_df['high'].max()
        min_future_low = future_df['low'].min()

        # Did it break Anchor High (State A)?
        broke_high = (max_future_high > anchor_high)
        # Did it touch/trade above Midpoint?
        traded_above_mid = (max_future_high >= anchor_mid)

        # If low was broken, did it SUBSEQUENTLY recover above anchor high or mid?
        recovered_above_mid_after_break = False
        recovered_above_high_after_break = False
        if low_broken and first_break_day is not None:
            # Days after break
            after_break_df = future_df.iloc[first_break_day - 1 :]
            recovered_above_mid_after_break = (after_break_df['high'] >= anchor_mid).any()
            recovered_above_high_after_break = (after_break_df['high'] >= anchor_high).any()

        # Maximum gain from anchor close
        max_gain_pct = (max_future_high - anchor_close) / anchor_close * 100.0
        max_loss_pct = (min_future_low - anchor_close) / anchor_close * 100.0

        records.append({
            'symbol': sym,
            'date': anchor_date,
            'anchor_close': anchor_close,
            'anchor_high': anchor_high,
            'anchor_low': anchor_low,
            'low_broken': low_broken,
            'first_break_day': first_break_day,
            'broke_high': broke_high,
            'traded_above_mid': traded_above_mid,
            'recovered_above_mid_after_break': recovered_above_mid_after_break,
            'recovered_above_high_after_break': recovered_above_high_after_break,
            'max_gain_pct': max_gain_pct,
            'max_loss_pct': max_loss_pct,
        })

    res = pd.DataFrame(records)
    print("\n=== SUMMARY: ANCHOR CANDLE D+1 ~ D+5 BEHAVIOR ===")
    print(f"Total events tracked: {len(res)}")
    print(f"Events where Low was NEVER broken in 5 days: {(~res['low_broken']).sum()} ({((~res['low_broken']).mean()*100):.1f}%)")
    print(f"Events where Low WAS broken in 5 days: {res['low_broken'].sum()} ({res['low_broken'].mean()*100:.1f}%)")

    non_broken = res[~res['low_broken']]
    broken = res[res['low_broken']]

    print("\n[NON-BROKEN LOW GROUP (저가 미이탈 우량군)]")
    print(f"  - High Breakout Rate: {(non_broken['broke_high'].mean()*100):.1f}%")
    print(f"  - Avg Max Gain (MFE): +{non_broken['max_gain_pct'].mean():.2f}%")
    print(f"  - Avg Max Loss (MAE): {non_broken['max_loss_pct'].mean():.2f}%")
    print(f"  - Cases with >= +5% gain: {(non_broken['max_gain_pct'] >= 5.0).mean()*100:.1f}%")
    print(f"  - Cases with >= +10% gain: {(non_broken['max_gain_pct'] >= 10.0).mean()*100:.1f}%")

    print("\n[BROKEN LOW GROUP (저가 이탈군)]")
    print(f"  - Recovered above Mid (50%) after break: {(broken['recovered_above_mid_after_break'].mean()*100):.1f}%")
    print(f"  - Recovered above High after break: {(broken['recovered_above_high_after_break'].mean()*100):.1f}%")
    print(f"  - Avg Max Gain (MFE): +{broken['max_gain_pct'].mean():.2f}%")
    print(f"  - Avg Max Loss (MAE): {broken['max_loss_pct'].mean():.2f}%")
    print(f"  - Cases with >= +5% gain after break: {(broken['max_gain_pct'] >= 5.0).mean()*100:.1f}%")

    res.to_csv("D:/repo/korea-market_v3-anchor/research/q2_position_low_break.csv", index=False)
    print("\nSaved Q2 details to research/q2_position_low_break.csv")

if __name__ == "__main__":
    run_q2_analysis()
