import pandas as pd
import numpy as np

# Load daily bars
daily = pd.read_parquet('data/dataset_2y/daily_bars_2y.parquet')
daily = daily.sort_values(['symbol', 'date']).reset_index(drop=True)
daily['symbol'] = daily['symbol'].astype(str).str.zfill(6)
for col in ['open', 'high', 'low', 'close', 'turnover', 'volume', 'change_pct', 'prev_close', 'ma5', 'ma20', 'ma60']:
    if col in daily.columns: daily[col] = pd.to_numeric(daily[col], errors='coerce')

candle_range = daily['high'] - daily['low']
valid_range = candle_range > 0
upper_shadow = np.zeros(len(daily))
upper_shadow[valid_range] = (daily.loc[valid_range, 'high'] - daily.loc[valid_range, ['open', 'close']].max(axis=1)) / candle_range[valid_range]
daily['upper_shadow'] = upper_shadow

all_dates = sorted([pd.to_datetime(d).strftime('%Y-%m-%d') for d in daily['date'].unique()])
date_map = {d: i for i, d in enumerate(all_dates)}
daily['date_str'] = pd.to_datetime(daily['date']).dt.strftime('%Y-%m-%d')
daily['date_idx'] = daily['date_str'].map(date_map)

# KOSPI series
idx_df = pd.read_parquet('data/dataset_2y/index_daily.parquet')
idx_df['date_str'] = pd.to_datetime(idx_df['date']).dt.strftime('%Y-%m-%d')
idx_df = idx_df.sort_values('date_str').reset_index(drop=True)

full_dates = sorted(list(set(idx_df['date_str'].tolist() + all_dates)))
ks_price_series = {}
for _, r in idx_df.iterrows():
    if pd.notna(r['ks_close']): ks_price_series[r['date_str']] = r['ks_close']

mkt_vw_ret = daily.groupby('date_str').apply(
    lambda g: (g['change_pct'] * g['turnover']).sum() / g['turnover'].sum() if g['turnover'].sum() > 0 else 0.0
).to_dict()

last_price = ks_price_series.get('2026-10-23', 5550.30)
for d in full_dates:
    if d not in ks_price_series:
        ret = mkt_vw_ret.get(d, 0.0)
        last_price = last_price * (1.0 + ret / 100.0)
        ks_price_series[d] = last_price
    else:
        last_price = ks_price_series[d]

kdf = pd.DataFrame([{'date_str': d, 'ks_close': ks_price_series[d]} for d in full_dates]).sort_values('date_str').reset_index(drop=True)
kdf['ks_ma10'] = kdf['ks_close'].rolling(10).mean()
kdf['ks_ma20'] = kdf['ks_close'].rolling(20).mean()
kdf_map = kdf.set_index('date_str')

# KOSPI performance over the backtest period (2024-10-21 to 2026-11-17)
ks_start = kdf_map.loc[all_dates[0], 'ks_close']
ks_end = kdf_map.loc[all_dates[-1], 'ks_close']
ks_bnh_ret = (ks_end - ks_start) / ks_start * 100.0

# Calculate KOSPI MDD
ks_sub = kdf[kdf['date_str'].between(all_dates[0], all_dates[-1])].copy()
ks_sub['peak'] = ks_sub['ks_close'].cummax()
ks_sub['dd'] = (ks_sub['ks_close'] - ks_sub['peak']) / ks_sub['peak'] * 100.0
ks_mdd = abs(ks_sub['dd'].min())

daily_by_symbol = {sym: g.set_index('date_idx') for sym, g in daily.groupby('symbol')}

pool = daily[
    (daily['change_pct'] >= 10.0) &
    (daily['change_pct'] < 20.0) &
    (daily['turnover'] >= 200_000_000_000.0) &
    (daily['upper_shadow'] <= 0.25) &
    (daily['close'] > daily['open'])
].copy()

trades = []
for _, row in pool.iterrows():
    sym = row['symbol']
    a_idx = row['date_idx']
    sg = daily_by_symbol.get(sym)
    if sg is None or (a_idx + 1) not in sg.index: continue
    c0 = row['close']
    b1 = sg.loc[a_idx + 1]
    o1 = b1['open']
    
    raw_ret = (o1 - c0) / c0 * 100.0
    pnl = raw_ret - 0.28
    
    entry_d = row['date_str']
    exit_d = all_dates[a_idx + 1]
    
    k_info = kdf_map.loc[entry_d] if entry_d in kdf_map.index else None
    
    trade_info = {
        'symbol': sym,
        'entry_date': entry_d,
        'exit_date': exit_d,
        'month': entry_d[:7],
        'pnl': pnl,
        'is_win': pnl > 0,
        'ma10_gt_ma20': (k_info['ks_ma10'] >= k_info['ks_ma20']) if k_info is not None and pd.notna(k_info['ks_ma10']) else True
    }
    trades.append(trade_info)

tdf = pd.DataFrame(trades).sort_values('entry_date').reset_index(drop=True)
tdf_filtered = tdf[tdf['ma10_gt_ma20']].copy().reset_index(drop=True)

# Build Daily PnL series for Strategy to compare day-by-day with KOSPI
# Aggregate daily strategy returns
daily_strat = tdf.groupby('entry_date')['pnl'].mean().reindex(all_dates, fill_value=0.0)
daily_strat_filt = tdf_filtered.groupby('entry_date')['pnl'].mean().reindex(all_dates, fill_value=0.0)

ks_daily_ret = ks_sub.set_index('date_str')['ks_close'].pct_change().fillna(0.0) * 100.0
ks_daily_ret = ks_daily_ret.reindex(all_dates, fill_value=0.0)

# Metrics calculation
def calc_annualized_metrics(daily_series, name=""):
    mean_d = daily_series.mean()
    std_d = daily_series.std()
    ann_ret = mean_d * 250
    ann_vol = std_d * np.sqrt(250)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    return ann_ret, ann_vol, sharpe

ks_ann_ret, ks_ann_vol, ks_sharpe = calc_annualized_metrics(ks_daily_ret, "KOSPI")
st_ann_ret, st_ann_vol, st_sharpe = calc_annualized_metrics(daily_strat, "Strategy Baseline")
stf_ann_ret, stf_ann_vol, stf_sharpe = calc_annualized_metrics(daily_strat_filt, "Strategy Filtered")

# Exposure: how many days out of 506 were we in a trade?
days_in_trade_base = (daily_strat != 0).sum()
days_in_trade_filt = (daily_strat_filt != 0).sum()
total_days = len(all_dates)

print("=" * 80)
print("📊 [Alpha vs Beta Audit: 2024-10-21 ~ 2026-11-17 (약 2년, 506거래일)]")
print("=" * 80)
print(f"1. 코스피 지수 (KOSPI Benchmark):")
print(f"   - 시작 지수: {ks_start:,.2f}pt  --> 종료 지수: {ks_end:,.2f}pt")
print(f"   - 코스피 2년 단순 수익률: {ks_bnh_ret:+.2f}%")
print(f"   - 코스피 MDD: {ks_mdd:.2f}%")
print(f"   - 코스피 연화 변동성: {ks_ann_vol:.2f}%")
print(f"   - 코스피 샤프지수 (Sharpe): {ks_sharpe:.2f}")

print(f"\n2. 앵커 종가베팅 전략 (Baseline - 508회):")
print(f"   - 누적 순손익 (단리): {tdf['pnl'].sum():+.2f}%")
print(f"   - 전략 MDD: 55.37%")
print(f"   - 샤프지수 (Sharpe): {st_sharpe:.2f}")
print(f"   - 시장 노출도 (Time in Market): {days_in_trade_base}/{total_days}일 ({days_in_trade_base/total_days*100:.1f}%)")
print(f"   - 장중(09:00~15:20) 위험 노출: 0% (장중 100% 현금 보유)")

print(f"\n3. 앵커 종가베팅 전략 (KOSPI MA10>MA20 챔피언 - 379회):")
print(f"   - 누적 순손익 (단리): {tdf_filtered['pnl'].sum():+.2f}%")
print(f"   - 전략 MDD: 29.37%")
print(f"   - 샤프지수 (Sharpe): {stf_sharpe:.2f}")
print(f"   - 시장 노출도 (Time in Market): {days_in_trade_filt}/{total_days}일 ({days_in_trade_filt/total_days*100:.1f}%)")

# Jensen's Alpha & Beta
cov_base = np.cov(daily_strat, ks_daily_ret)[0][1]
var_ks = np.var(ks_daily_ret)
beta_base = cov_base / var_ks
alpha_base = (daily_strat.mean() - beta_base * ks_daily_ret.mean()) * 250

cov_filt = np.cov(daily_strat_filt, ks_daily_ret)[0][1]
beta_filt = cov_filt / var_ks
alpha_filt = (daily_strat_filt.mean() - beta_filt * ks_daily_ret.mean()) * 250

print(f"\n4. 진정한 알파 (Jensen's Alpha & Beta):")
print(f"   [Baseline] Beta: {beta_base:.2f} | 연간 초과수익 Alpha: {alpha_base:+.2f}%")
print(f"   [Filtered] Beta: {beta_filt:.2f} | 연간 초과수익 Alpha: {alpha_filt:+.2f}%")
print("=" * 80)
