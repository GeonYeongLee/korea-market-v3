"""
Research Script: Q1 Anchor Candle Definition Analysis
Analyzes 1.2M daily bars to find optimal criteria for anchor candle:
- Change pct threshold
- Body ratio & Upper shadow ratio
- Turnover surge relative to 20-day ADTO
- Daily active watchlist size vs KIS WebSocket limit (40 symbols)
"""
import pandas as pd
import numpy as np

def run_q1_analysis():
    print("Loading daily bars data...")
    df = pd.read_parquet("D:/repo/korea-market_v2/data/dataset_2y/daily_bars_2y.parquet")
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values(['symbol', 'date']).reset_index(drop=True)

    # Calculate candle geometry
    df['candle_range'] = df['high'] - df['low']
    # Avoid zero division
    valid_range = df['candle_range'] > 0
    df['body_ratio'] = 0.0
    df.loc[valid_range, 'body_ratio'] = (df.loc[valid_range, 'close'] - df.loc[valid_range, 'open']) / df.loc[valid_range, 'candle_range']
    
    df['upper_shadow_ratio'] = 0.0
    df.loc[valid_range, 'upper_shadow_ratio'] = (df.loc[valid_range, 'high'] - df.loc[valid_range, ['open', 'close']].max(axis=1)) / df.loc[valid_range, 'candle_range']

    df['turnover_ratio'] = df['turnover'] / (df['adto20'] + 1e-6)

    # Test parameter grids (change_pct is in percent, e.g. 15.0 = +15%)
    change_thresholds = [10.0, 15.0, 20.0, 25.0]
    turnover_ratios = [3.0, 5.0, 10.0]
    shadow_caps = [0.20, 0.30]

    results = []

    for ct in change_thresholds:
        for tr in turnover_ratios:
            for sc in shadow_caps:
                is_anchor = (
                    (df['change_pct'] >= ct) &
                    (df['turnover_ratio'] >= tr) &
                    (df['upper_shadow_ratio'] <= sc) &
                    (df['close'] > df['open']) &
                    (df['turnover'] >= 10_000_000_000) # Minimum 10 billion KRW turnover to prevent illiquid penny stocks
                )
                
                # Check candidate pool size across lookback windows: 3 days, 5 days, 10 days, 20 days
                # Count daily occurrences
                df['is_anchor'] = is_anchor
                daily_counts = df.groupby('date')['is_anchor'].sum()
                
                # For lookback windows L = 3, 5, 10, 20
                for L in [3, 5, 10, 20]:
                    # Rolling sum of active candidates
                    rolling_active = daily_counts.rolling(L).sum()
                    avg_active = rolling_active.mean()
                    p90_active = rolling_active.quantile(0.90)
                    max_active = rolling_active.max()

                    results.append({
                        'change_pct': f"+{int(ct)}%",
                        'turnover_ratio': f"{int(tr)}x",
                        'upper_shadow_cap': f"{int(sc*100)}%",
                        'lookback_days': L,
                        'total_anchor_events': int(is_anchor.sum()),
                        'avg_daily_candidates': round(avg_active, 1),
                        'p90_daily_candidates': round(p90_active, 1),
                        'max_daily_candidates': int(max_active)
                    })

    res_df = pd.DataFrame(results)
    print("\n=== Top configurations fitting KIS WebSocket Limit (10 ~ 40 symbols) ===")
    fitting = res_df[(res_df['avg_daily_candidates'] >= 8) & (res_df['p90_daily_candidates'] <= 40)]
    print(fitting.to_string(index=False))

    res_df.to_csv("D:/repo/korea-market_v3-anchor/research/q1_anchor_grid.csv", index=False)
    print("\nSaved full results to research/q1_anchor_grid.csv")

if __name__ == "__main__":
    run_q1_analysis()
