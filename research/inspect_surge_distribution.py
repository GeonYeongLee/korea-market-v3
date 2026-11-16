import pandas as pd
import os

trades = pd.read_csv('anchor_q1_trades.csv')
daily_path = 'data/dataset_2y/daily'

results = []
for _, tr in trades.iterrows():
    sym = str(tr['symbol']).zfill(6)
    date_str = tr['entry_dt'][:10].replace('-', '')
    fp = os.path.join(daily_path, date_str, f'{sym}.parquet')
    if not os.path.exists(fp):
        continue
    df = pd.read_parquet(fp)
    day_open = df.iloc[0]['open']
    entry_p = tr['entry_price']
    ret = tr['return_pct']
    bar0 = df.iloc[0]
    surge_from_open = (entry_p - day_open) / day_open * 100.0
    results.append({
        'symbol': sym,
        'name': tr['name'],
        'entry_dt': tr['entry_dt'],
        'day_open': day_open,
        'entry_p': entry_p,
        'surge_from_open': surge_from_open,
        'ret': ret,
        'win': 1 if tr['net_pnl'] > 0 else 0,
        'mae': tr['mae_pct'],
        'mfe': tr['mfe_pct'],
    })

res_df = pd.DataFrame(results)
print('--- 진입 시점 당일 시가 대비 상승률 (Surge from Open) 분포 ---')
print(res_df['surge_from_open'].describe())

res_df['surge_bucket'] = pd.cut(
    res_df['surge_from_open'],
    bins=[-10, 2.0, 4.0, 6.0, 8.0, 15.0],
    labels=['<2%', '2%~4%', '4%~6%', '6%~8%', '>8%']
)
print('\n--- 시가 대비 상승률 구간별 성과 ---')
surge_stats = res_df.groupby('surge_bucket', observed=False).agg(
    count=('symbol', 'count'),
    win_rate=('win', 'mean'),
    avg_return=('ret', 'mean'),
    avg_mfe=('mfe', 'mean'),
    avg_mae=('mae', 'mean')
)
surge_stats['win_rate'] = (surge_stats['win_rate'] * 100).round(1)
print(surge_stats)
