import os
import pandas as pd
import numpy as np

trades = pd.read_csv('anchor_q1_trades.csv')
daily_path = 'data/dataset_2y/daily'

enriched = []
for _, tr in trades.iterrows():
    sym = str(tr['symbol']).zfill(6)
    d_str = tr['entry_dt'][:10].replace('-', '')
    fp = os.path.join(daily_path, d_str, f'{sym}.parquet')
    if not os.path.exists(fp):
        continue
    df = pd.read_parquet(fp)
    # entry bar
    df['time_str'] = df['timestamp'].astype(str).str.slice(11, 16)
    e_time = tr['entry_dt'][11:16]
    e_bars = df[df['time_str'] == e_time]
    if len(e_bars) == 0:
        continue
    e_bar = e_bars.iloc[0]
    
    # Check total day turnover of this stock vs market
    df['cum_to'] = df['turnover'].cumsum()
    day_cum_to = df['turnover'].sum()
    bar_to = e_bar['turnover']
    bar_cum_to = df.loc[e_bars.index[0], 'cum_to']
    
    enriched.append({
        'symbol': sym,
        'date': d_str,
        'name': tr['name'],
        'ret': tr['return_pct'],
        'net_pnl': tr['net_pnl'],
        'win': 1 if tr['net_pnl'] > 0 else 0,
        'mae': tr['mae_pct'],
        'mfe': tr['mfe_pct'],
        'bar_to_100m': bar_to / 100_000_000.0,
        'bar_cum_to_100m': bar_cum_to / 100_000_000.0,
        'day_cum_to_100m': day_cum_to / 100_000_000.0,
    })

edf = pd.DataFrame(enriched)
print(f"Total analyzed: {len(edf)}")
print("\n--- 1분봉 거래대금(bar_to) 구간별 성과 ---")
edf['to_bin'] = pd.cut(edf['bar_to_100m'], bins=[0, 5.0, 10.0, 20.0, 1000.0], labels=['<5억', '5억~10억', '10억~20억', '>=20억'])
print(edf.groupby('to_bin', observed=False).agg(
    count=('symbol', 'count'),
    win_rate=('win', lambda x: x.mean() * 100),
    avg_ret=('ret', 'mean'),
    avg_mfe=('mfe', 'mean'),
    avg_mae=('mae', 'mean'),
    total_pnl=('net_pnl', 'sum')
))

print("\n--- 당일 총 거래대금(day_cum_to) 구간별 성과 ---")
edf['day_to_bin'] = pd.cut(edf['day_cum_to_100m'], bins=[0, 300.0, 1000.0, 3000.0, 100000.0], labels=['<300억', '300억~1000억', '1000억~3000억', '>=3000억'])
print(edf.groupby('day_to_bin', observed=False).agg(
    count=('symbol', 'count'),
    win_rate=('win', lambda x: x.mean() * 100),
    avg_ret=('ret', 'mean'),
    avg_mfe=('mfe', 'mean'),
    avg_mae=('mae', 'mean'),
    total_pnl=('net_pnl', 'sum')
))
