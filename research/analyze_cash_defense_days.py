import pandas as pd
import numpy as np
import math

print("1. Loading datasets...")
df_trades = pd.read_csv('research/golden_25_decoupled_trades_2020_2026.csv')
trade_dates = set(df_trades['date'].astype(str).unique())

pool = pd.read_parquet('research/round2_candidate_pool.parquet')
all_days = sorted(pool['date_str'].astype(str).unique())
all_days = [d for d in all_days if d >= '2020-01-01']

cash_days = sorted([d for d in all_days if d not in trade_dates])
trade_days_sorted = sorted(list(trade_dates))

print(f"Total trading days (2020~2026): {len(all_days)}")
print(f"Active trade days: {len(trade_dates)} ({len(trade_dates)/len(all_days)*100:.1f}%)")
print(f"Cash defense days: {len(cash_days)} ({len(cash_days)/len(all_days)*100:.1f}%)")

# Load index data
df_idx = pd.read_parquet('data/dataset_2y/index_daily_full.parquet')
df_idx['date_str'] = pd.to_datetime(df_idx['date']).dt.strftime('%Y-%m-%d')
df_idx = df_idx.sort_values('date_str').reset_index(drop=True)
if 'kq_close' in df_idx.columns:
    df_idx['kq_prev'] = df_idx['kq_close'].shift(1)
    df_idx['kq_chg'] = (df_idx['kq_close'] - df_idx['kq_prev']) / df_idx['kq_prev'] * 100.0
if 'ks_close' in df_idx.columns:
    df_idx['ks_prev'] = df_idx['ks_close'].shift(1)
    df_idx['ks_chg'] = (df_idx['ks_close'] - df_idx['ks_prev']) / df_idx['ks_prev'] * 100.0

idx_map = df_idx.set_index('date_str')[['kq_chg', 'ks_chg']].to_dict('index')

# Index statistics on Trade Days vs Cash Days
trade_kq = [idx_map[d]['kq_chg'] for d in trade_days_sorted if d in idx_map and pd.notna(idx_map[d]['kq_chg'])]
cash_kq = [idx_map[d]['kq_chg'] for d in cash_days if d in idx_map and pd.notna(idx_map[d]['kq_chg'])]

print("\n" + "="*80)
print("📊 [1. 지수 환경 비교: 거래 발생일 vs 관망일(Cash Defense Days)]")
print("="*80)
print(f"거래 발생일 (951일) KOSDAQ 평균 등락률: {np.mean(trade_kq):+.2f}% (표준편차: {np.std(trade_kq):.2f}%)")
print(f"관망일 (722일)     KOSDAQ 평균 등락률: {np.mean(cash_kq):+.2f}% (표준편차: {np.std(cash_kq):.2f}%)")
print(f"관망일 KOSDAQ 상승일: {sum(x > 0 for x in cash_kq)}일 ({sum(x > 0 for x in cash_kq)/len(cash_kq)*100:.1f}%)")
print(f"관망일 KOSDAQ 하락일: {sum(x < 0 for x in cash_kq)}일 ({sum(x < 0 for x in cash_kq)/len(cash_kq)*100:.1f}%)")
print(f"관망일 KOSDAQ 보합권(-1.0% ~ +1.0%): {sum(-1.0 <= x <= 1.0 for x in cash_kq)}일 ({sum(-1.0 <= x <= 1.0 for x in cash_kq)/len(cash_kq)*100:.1f}%)")

# Load daily bars for 2020~2026
print("\n2. Loading daily bars (2020~2026)...")
df_daily = pd.read_parquet('data/dataset_2y/daily_bars_all.parquet')
df_daily['date_str'] = pd.to_datetime(df_daily['date']).dt.strftime('%Y-%m-%d')
df_daily = df_daily[df_daily['date_str'] >= '2020-01-01'].copy()
df_daily['symbol'] = df_daily['symbol'].astype(str).str.zfill(6)
df_daily = df_daily.sort_values(['symbol', 'date_str']).reset_index(drop=True)

# Shift next open for forward return
df_daily['next_open'] = df_daily.groupby('symbol')['open'].shift(-1)
df_daily['fwd_ret_net'] = (df_daily['next_open'] - df_daily['close']) / df_daily['close'] * 100.0 - 0.28

# Filter out lower limit or invalid bars
df_daily = df_daily[df_daily['change_pct'] > -29.0].copy()
df_daily = df_daily[df_daily['next_open'].notna()].copy()

# Features
df_daily['open_gap'] = (df_daily['open'] - df_daily['prev_close']) / df_daily['prev_close'] * 100.0
df_daily['body_pct'] = (df_daily['close'] - df_daily['open']) / df_daily['open'] * 100.0
tr = df_daily['high'] - df_daily['low']
df_daily['upper_shadow'] = np.where(tr > 0, (df_daily['high'] - np.maximum(df_daily['open'], df_daily['close'])) / tr, 0.0)
df_daily['lower_shadow'] = np.where(tr > 0, (np.minimum(df_daily['open'], df_daily['close']) - df_daily['low']) / tr, 0.0)

# Turnover ranks
df_daily['to_rank'] = df_daily.groupby('date_str')['turnover'].rank(ascending=False, method='min')

# Tag cash days
df_cash_bars = df_daily[df_daily['date_str'].isin(cash_days)].copy()

print("\n" + "="*80)
print("📊 [2. 관망일(722일) 거래대금 TOP 20 종목들의 특성 분석]")
print("="*80)
top20_cash = df_cash_bars[df_cash_bars['to_rank'] <= 20].copy()
print(f"관망일 TOP 20 종목 총 표본 수: {len(top20_cash)}건")
print(f"평균 당일 등락률 (change_pct): {top20_cash['change_pct'].mean():+.2f}%")
print(f"평균 몸통 수익률 (body_pct):   {top20_cash['body_pct'].mean():+.2f}%")
print(f"평균 윗꼬리 비율 (upper_shadow): {top20_cash['upper_shadow'].mean()*100:.1f}%")
print(f"평균 익일 시초 갭 순수익률 (fwd_ret_net): {top20_cash['fwd_ret_net'].mean():+.3f}% (승률: {(top20_cash['fwd_ret_net'] > 0).mean()*100:.1f}%)")

# Why didn't Champion B trigger?
cands_champ_like = top20_cash[top20_cash['change_pct'] >= 7.0]
print(f"\n- 당일 +7% 이상 급등 종목 수: {len(cands_champ_like)}건 (전체 TOP 20 중 {len(cands_champ_like)/len(top20_cash)*100:.1f}%)")
if len(cands_champ_like) > 0:
    print(f"  * 이 중 몸통 < 5.5% 비율: {(cands_champ_like['body_pct'] < 5.5).mean()*100:.1f}% (시초 갭상승 후 멈춤)")
    print(f"  * 이 중 윗꼬리 > 40% 비율: {(cands_champ_like['upper_shadow'] > 0.40).mean()*100:.1f}% (장중 급등 후 윗꼬리 매물 출회)")

# Evaluate Multiple Strategic Hypotheses on Cash Days
def test_hypothesis(name, condition_df, sort_col='turnover', ascending=False, max_per_day=2):
    sub = condition_df.sort_values(['date_str', sort_col], ascending=[True, ascending]).copy()
    picks = sub.groupby('date_str').head(max_per_day).copy()
    
    n = len(picks)
    if n == 0:
        return {'name': name, 'trades': 0, 'wr': 0, 'avg_ret': 0, 'pf': 0, 't_stat': 0, 'p_val': 1.0, 'total_pnl': 0}
    
    wr = (picks['fwd_ret_net'] > 0).mean() * 100.0
    avg_ret = picks['fwd_ret_net'].mean()
    std_ret = picks['fwd_ret_net'].std()
    
    wins = picks[picks['fwd_ret_net'] > 0]['fwd_ret_net'].sum()
    losses = abs(picks[picks['fwd_ret_net'] < 0]['fwd_ret_net'].sum())
    pf = wins / losses if losses > 0 else 999.0
    
    t_stat = avg_ret / (std_ret / math.sqrt(n)) if std_ret > 0 and n > 1 else 0.0
    # Approx two-tailed p-value
    p_val = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t_stat) / math.sqrt(2.0))))
    
    # 5,000,000 KRW fixed slot PnL
    total_pnl = (picks['fwd_ret_net'] / 100.0 * 5_000_000.0).sum()
    
    return {
        'name': name,
        'trades': n,
        'wr': wr,
        'avg_ret': avg_ret,
        'pf': pf,
        't_stat': t_stat,
        'p_val': p_val,
        'total_pnl': total_pnl
    }

print("\n" + "="*80)
print("🔬 [3. 관망일(722일) 전용 신규 진입 가설 체계적 검증 (전수 시뮬레이션)]")
print("="*80)

hypotheses = []

# H1: 준-기준봉 (Body 3.5%~5.5%, Change 5%~15%, Small Upper Shadow <= 0.25)
cond_h1 = (
    (top20_cash['to_rank'] <= 20) &
    (top20_cash['body_pct'] >= 3.5) &
    (top20_cash['body_pct'] < 5.5) &
    (top20_cash['change_pct'] >= 5.0) &
    (top20_cash['change_pct'] <= 15.0) &
    (top20_cash['upper_shadow'] <= 0.25)
)
hypotheses.append(test_hypothesis("H1: 준-기준봉 (Body 3.5~5.5%, Change 5~15%, 윗꼬리<=25%)", top20_cash[cond_h1]))

# H2: 기준봉 문턱 완화 (Body >= 4.0%, Change >= 6.0%, 윗꼬리 <= 0.30)
cond_h2 = (
    (top20_cash['to_rank'] <= 20) &
    (top20_cash['body_pct'] >= 4.0) &
    (top20_cash['change_pct'] >= 6.0) &
    (top20_cash['upper_shadow'] <= 0.30)
)
hypotheses.append(test_hypothesis("H2: 기준봉 문턱 완화 (Body>=4.0%, Change>=6.0%, 윗꼬리<=30%)", top20_cash[cond_h2]))

# H3: 유니버스 확장 (거래대금 TOP 21~50위 중 Champion B 충족 종목)
top50_cash = df_cash_bars[(df_cash_bars['to_rank'] > 20) & (df_cash_bars['to_rank'] <= 50)].copy()
cond_h3 = (
    (top50_cash['change_pct'] >= 8.0) &
    (top50_cash['change_pct'] <= 22.0) &
    (top50_cash['body_pct'] >= 5.5) &
    (top50_cash['upper_shadow'] <= 0.20)
)
hypotheses.append(test_hypothesis("H3: 유니버스 확장 TOP 21~50위 중 기준봉 (Change 8~22%, Body>=5.5%)", top50_cash[cond_h3]))

# H4: 거래대금 최상위(TOP 10) 십자도지/눌림목 (Change -1.5% ~ +1.5%, Body <= 1.5%)
cond_h4 = (
    (top20_cash['to_rank'] <= 10) &
    (top20_cash['change_pct'] >= -1.5) &
    (top20_cash['change_pct'] <= 1.5) &
    (top20_cash['body_pct'].abs() <= 1.5)
)
hypotheses.append(test_hypothesis("H4: TOP 10 십자도지/숨고르기 (Change -1.5~+1.5%, |Body|<=1.5%)", top20_cash[cond_h4]))

# H5: 거래대금 상위(TOP 20) 완만한 음봉 조정 매수 (Change -2.0% ~ -5.0%)
cond_h5 = (
    (top20_cash['to_rank'] <= 20) &
    (top20_cash['change_pct'] >= -5.0) &
    (top20_cash['change_pct'] <= -2.0)
)
hypotheses.append(test_hypothesis("H5: TOP 20 완만한 음봉 조정 (Change -2.0% ~ -5.0%)", top20_cash[cond_h5]))

# H6: 갭상승 유지형 (Gap >= +1.5%, Body >= +1.0%, Change >= +3.0%)
cond_h6 = (
    (top20_cash['to_rank'] <= 20) &
    (top20_cash['open_gap'] >= 1.5) &
    (top20_cash['body_pct'] >= 1.0) &
    (top20_cash['change_pct'] >= 3.0)
)
hypotheses.append(test_hypothesis("H6: 갭상승 유지형 양봉 (Gap>=1.5%, Body>=1.0%, Change>=3%)", top20_cash[cond_h6]))

# H7: 윗꼬리 매물 소화형 (High >= +10%, Close +4% ~ +8%, Upper Shadow 0.30~0.50)
cond_h7 = (
    (top20_cash['to_rank'] <= 20) &
    (top20_cash['high_pct'] >= 10.0) &
    (top20_cash['change_pct'] >= 4.0) &
    (top20_cash['change_pct'] <= 8.0) &
    (top20_cash['upper_shadow'] >= 0.30) &
    (top20_cash['upper_shadow'] <= 0.50)
)
hypotheses.append(test_hypothesis("H7: 윗꼬리 매물 소화형 (High>=10%, Close 4~8%, 윗꼬리 30~50%)", top20_cash[cond_h7]))

# H8: TOP 10의 순수 양봉 (Body >= +1.5%, Change > 0)
cond_h8 = (
    (top20_cash['to_rank'] <= 10) &
    (top20_cash['body_pct'] >= 1.5) &
    (top20_cash['change_pct'] > 0.0)
)
hypotheses.append(test_hypothesis("H8: TOP 10 순수 양봉 (Body>=1.5%, Change>0)", top20_cash[cond_h8]))

# Print results table
print(f"{'가설 번호 및 조건':<52} | {'거래수':<6} | {'승률':<6} | {'건당수익':<9} | {'PF':<5} | {'t-stat':<7} | {'p-value':<9} | {'누적손익(원)':<12}")
print("-" * 125)
for h in hypotheses:
    print(f"{h['name']:<50} | {h['trades']:<6} | {h['wr']:>5.1f}% | {h['avg_ret']:>+7.3f}% | {h['pf']:>4.2f} | {h['t_stat']:>+6.2f} | {h['p_val']:>8.4f} | {h['total_pnl']:>+12,.0f}원")
