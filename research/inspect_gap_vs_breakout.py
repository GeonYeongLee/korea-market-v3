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
    results.append({
        'symbol': sym,
        'entry_dt': tr['entry_dt'],
        'day_open': day_open,
        'entry_p': entry_p,
        'open_ratio': (day_open / entry_p - 1.0) * 100,
        'bar0_open': bar0['open'],
        'bar0_high': bar0['high'],
        'bar0_low': bar0['low'],
        'bar0_close': bar0['close'],
        'bar0_to_krw': bar0['turnover'],
        'ret': ret,
        'win': 1 if tr['net_pnl'] > 0 else 0,
        'mae': tr['mae_pct'],
        'mfe': tr['mfe_pct'],
    })

res_df = pd.DataFrame(results)
print('Total analyzed:', len(res_df))
gap_entries = res_df[res_df['entry_p'] == res_df['day_open']]
intra_entries = res_df[res_df['entry_p'] > res_df['day_open']]

print(f"Entry == Day Open (갭 상승 시가 진입): {len(gap_entries)}건")
if len(gap_entries) > 0:
    print(f"  - Win Rate: {gap_entries['win'].mean()*100:.1f}%")
    print(f"  - Avg Return: {gap_entries['ret'].mean():.2f}%")
    print(f"  - Avg MFE: {gap_entries['mfe'].mean():.2f}% | Avg MAE: {gap_entries['mae'].mean():.2f}%")

print(f"Entry > Day Open (장중 상승 돌파 진입): {len(intra_entries)}건")
if len(intra_entries) > 0:
    print(f"  - Win Rate: {intra_entries['win'].mean()*100:.1f}%")
    print(f"  - Avg Return: {intra_entries['ret'].mean():.2f}%")
    print(f"  - Avg MFE: {intra_entries['mfe'].mean():.2f}% | Avg MAE: {intra_entries['mae'].mean():.2f}%")

print("\n--- 거래대금(1분봉 Turnover) 분포와 성과 ---")
res_df['to_100m'] = res_df['bar0_to_krw'] / 100_000_000.0
print("전체 진입 봉 평균 거래대금: ", round(res_df['to_100m'].mean(), 1), "억원 (중앙값: ", round(res_df['to_100m'].median(), 1), "억원)")
high_to = res_df[res_df['to_100m'] >= 10.0]
low_to = res_df[res_df['to_100m'] < 10.0]
print(f"거래대금 >= 10억 (강한 수급): {len(high_to)}건, 승률: {high_to['win'].mean()*100:.1f}%, 평균수익: {high_to['ret'].mean():.2f}%")
print(f"거래대금 < 10억 (약한 수급): {len(low_to)}건, 승률: {low_to['win'].mean()*100:.1f}%, 평균수익: {low_to['ret'].mean():.2f}%")
