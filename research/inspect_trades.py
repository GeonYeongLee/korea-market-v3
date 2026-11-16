import pandas as pd
import numpy as np

df = pd.read_csv('anchor_q1_trades.csv')
df['entry_time'] = df['entry_dt'].str.slice(11, 16)
df['win'] = df['net_pnl'] > 0

print('=== 1. OVERALL STATS ===')
print(f'Total Trades: {len(df)}')
print(f"Wins: {df['win'].sum()} ({df['win'].mean()*100:.1f}%)")
print(f"Losses: {(~df['win']).sum()} ({(~df['win']).mean()*100:.1f}%)")
print(f"Net PnL: {df['net_pnl'].sum():,} KRW")
win_pnl = df[df['win']]['net_pnl'].sum()
loss_pnl = abs(df[~df['win']]['net_pnl'].sum())
print(f'Profit Factor: {win_pnl / loss_pnl:.2f}')
print(f"Avg Win: {df[df['win']]['return_pct'].mean():.2f}% | Avg Loss: {df[~df['win']]['return_pct'].mean():.2f}%")
print(f"Avg Win PnL: {df[df['win']]['net_pnl'].mean():,.0f} KRW | Avg Loss PnL: {df[~df['win']]['net_pnl'].mean():,.0f} KRW")

print('\n=== 2. EXIT REASONS ===')
exit_stats = df.groupby('exit_reason').agg(
    count=('trade_id', 'count'),
    win_rate=('win', 'mean'),
    avg_return=('return_pct', 'mean'),
    avg_holding=('holding_min', 'mean'),
    total_pnl=('net_pnl', 'sum'),
    avg_mfe=('mfe_pct', 'mean'),
    avg_mae=('mae_pct', 'mean')
)
exit_stats['win_rate'] = (exit_stats['win_rate'] * 100).round(1)
print(exit_stats)

print('\n=== 3. ENTRY TIME BUCKETS ===')
df['time_min'] = df['entry_time'].apply(lambda t: int(t.split(':')[0])*60 + int(t.split(':')[1]))
df['time_bucket'] = pd.cut(
    df['time_min'],
    bins=[9*60-1, 9*60+5, 9*60+10, 9*60+20, 9*60+31],
    labels=['09:00-09:05', '09:06-09:10', '09:11-09:20', '09:21-09:30']
)
time_stats = df.groupby('time_bucket', observed=False).agg(
    count=('trade_id', 'count'),
    win_rate=('win', 'mean'),
    avg_return=('return_pct', 'mean'),
    total_pnl=('net_pnl', 'sum'),
    avg_mfe=('mfe_pct', 'mean'),
    avg_mae=('mae_pct', 'mean')
)
time_stats['win_rate'] = (time_stats['win_rate'] * 100).round(1)
print(time_stats)

print('\n=== 4. HOLDING TIME DISTRIBUTION ===')
print('Winners holding min: median=', df[df['win']]['holding_min'].median(), 'mean=', round(df[df['win']]['holding_min'].mean(), 1))
print('Losers holding min: median=', df[~df['win']]['holding_min'].median(), 'mean=', round(df[~df['win']]['holding_min'].mean(), 1))

print('\n=== 5. MFE vs MAE INSIGHTS ===')
print('Winners Avg MFE:', round(df[df['win']]['mfe_pct'].mean(), 2), '% | Avg MAE:', round(df[df['win']]['mae_pct'].mean(), 2), '%')
print('Losers Avg MFE:', round(df[~df['win']]['mfe_pct'].mean(), 2), '% | Avg MAE:', round(df[~df['win']]['mae_pct'].mean(), 2), '%')

losers_with_mfe_over_2 = df[(~df['win']) & (df['mfe_pct'] >= 2.0)]
print(f"Losers that reached MFE >= +2.0%: {len(losers_with_mfe_over_2)} out of {len(df[~df['win']])} ({len(losers_with_mfe_over_2)/len(df[~df['win']])*100:.1f}%)")
losers_with_mfe_over_1 = df[(~df['win']) & (df['mfe_pct'] >= 1.0)]
print(f"Losers that reached MFE >= +1.0%: {len(losers_with_mfe_over_1)} out of {len(df[~df['win']])} ({len(losers_with_mfe_over_1)/len(df[~df['win']])*100:.1f}%)")

print('\n=== 6. WORST 10 LOSERS ===')
print(df[~df['win']].sort_values('net_pnl').head(10)[['symbol', 'name', 'entry_dt', 'exit_reason', 'holding_min', 'return_pct', 'net_pnl', 'mfe_pct', 'mae_pct']])

print('\n=== 7. BEST 10 WINNERS ===')
print(df[df['win']].sort_values('net_pnl', ascending=False).head(10)[['symbol', 'name', 'entry_dt', 'exit_reason', 'holding_min', 'return_pct', 'net_pnl', 'mfe_pct', 'mae_pct']])
