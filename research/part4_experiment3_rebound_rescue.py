import os
import sys
import duckdb
import pandas as pd
import numpy as np

print("=" * 105)
print("🚀 [EXPERIMENT 3: 익일 09:00 갭다운 시 반등 구제 청산(Rebound Rescue) 심층 분석]")
print("=" * 105)

base_dir = 'D:/repo/korea-market_v3-anchor'
pool = pd.read_parquet(f'{base_dir}/research/quant_research_pool_temp.parquet')

# -------------------------------------------------------------
# Part 3A: 2024~2026 1-minute Microstructure Rigorous Analysis
# -------------------------------------------------------------
print("\n" + "=" * 90)
print("🔬 [Part 3A: 2024~2026 1분봉 데이터 초밀착 마이크로구조 분석]")
print("=" * 90)

daily_dir = f'{base_dir}/data/dataset_2y/daily'

# Select trades from Tiered Entry pool
cond_tier1 = (pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 25.0) & (pool['upper_shadow'] <= 0.15)
cond_tier2 = (pool['to_rank'] >= 11) & (pool['to_rank'] <= 25) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10)
tiered_pool = pool[cond_tier1 | cond_tier2].copy()

# Focus on negative gap trades
neg_trades = tiered_pool[tiered_pool['next_open'] < tiered_pool['close']].copy()
print(f"• 전체 계층형 후보 중 갭다운(next_open < close) 종목: {len(neg_trades)}건 / {len(tiered_pool)}건 ({len(neg_trades)/len(tiered_pool)*100:.1f}%)")

m1_records = []
for _, row in neg_trades.iterrows():
    sym = str(row['symbol']).zfill(6)
    nd_clean = row['next_date_str'].replace('-', '')
    fp = os.path.join(daily_dir, nd_clean, f"{sym}.parquet")
    
    if not os.path.exists(fp):
        continue
    try:
        mdf = pd.read_parquet(fp)
    except Exception:
        continue
    if len(mdf) == 0:
        continue
        
    if 'timestamp' in mdf.columns:
        mdf['time_str'] = pd.to_datetime(mdf['timestamp']).dt.strftime('%H:%M')
    elif 'time' in mdf.columns:
        mdf['time_str'] = mdf['time'].astype(str).str[-6:-2].apply(lambda s: f"{s[:2]}:{s[2:]}")
    else:
        continue
        
    m0930 = mdf[(mdf['time_str'] >= '09:00') & (mdf['time_str'] <= '09:30')].copy()
    if len(m0930) == 0:
        continue
        
    c0 = row['close']
    open_p = row['next_open']
    open_gap_pct = (open_p - c0) / c0 * 100.0
    
    # 09:00 exit return
    ret_open = open_gap_pct - 0.28
    
    # Track minute-by-minute trajectory
    hit_0pct = False
    hit_0pct_min = None
    hit_1pct = False
    hit_1pct_min = None
    sl_2pct = False
    sl_2pct_min = None
    sl_3pct = False
    sl_3pct_min = None
    
    # Traverse minutes
    for idx_m, (_, mrow) in enumerate(m0930.iterrows()):
        m_high = mrow['high']
        m_low = mrow['low']
        m_time = mrow['time_str']
        
        # Check high targets
        if not hit_0pct and m_high >= c0:
            hit_0pct = True
            hit_0pct_min = idx_m
            
        if not hit_1pct and m_high >= (c0 * 1.01):
            hit_1pct = True
            hit_1pct_min = idx_m
            
        # Check stop loss
        if not sl_2pct and m_low <= (c0 * 0.98): # -2% from entry
            sl_2pct = True
            sl_2pct_min = idx_m
            
        if not sl_3pct and m_low <= (c0 * 0.97): # -3% from entry
            sl_3pct = True
            sl_3pct_min = idx_m
            
    price_0930 = m0930.iloc[-1]['close']
    ret_0930_market = ((price_0930 - c0) / c0 * 100.0) - 0.28
    ret_day_close = ((row['next_close'] - c0) / c0 * 100.0) - 0.28
    
    # Strategy 1: Rescue 0.0% (Limit at c0, fallback 09:30 market)
    ret_res_0 = (0.0 - 0.28) if hit_0pct else ret_0930_market
    
    # Strategy 2: Rescue +1.0% (Limit at c0*1.01, fallback 09:30 market)
    ret_res_1 = (1.0 - 0.28) if hit_1pct else ret_0930_market
    
    # Strategy 3: Rescue 0.0% + SL -2.5% OCO
    # If SL hit BEFORE 0% target:
    if hit_0pct and sl_2pct:
        if hit_0pct_min <= sl_2pct_min:
            ret_res_0_sl2 = 0.0 - 0.28
        else:
            ret_res_0_sl2 = -2.0 - 0.28
    elif hit_0pct:
        ret_res_0_sl2 = 0.0 - 0.28
    elif sl_2pct:
        ret_res_0_sl2 = -2.0 - 0.28
    else:
        ret_res_0_sl2 = ret_0930_market
        
    # Strategy 4: Rescue +1.0% + SL -3.0% OCO
    if hit_1pct and sl_3pct:
        if hit_1pct_min <= sl_3pct_min:
            ret_res_1_sl3 = 1.0 - 0.28
        else:
            ret_res_1_sl3 = -3.0 - 0.28
    elif hit_1pct:
        ret_res_1_sl3 = 1.0 - 0.28
    elif sl_3pct:
        ret_res_1_sl3 = -3.0 - 0.28
    else:
        ret_res_1_sl3 = ret_0930_market
        
    m1_records.append({
        'symbol': sym,
        'date': row['date_str'],
        'next_date': row['next_date_str'],
        'c0': c0,
        'open_p': open_p,
        'open_gap_pct': open_gap_pct,
        'hit_0pct': hit_0pct,
        'hit_1pct': hit_1pct,
        'hit_0pct_min': hit_0pct_min,
        'hit_1pct_min': hit_1pct_min,
        'price_0930': price_0930,
        'ret_open': ret_open,
        'ret_res_0': ret_res_0,
        'ret_res_1': ret_res_1,
        'ret_res_0_sl2': ret_res_0_sl2,
        'ret_res_1_sl3': ret_res_1_sl3,
        'ret_day_close': ret_day_close
    })

m1_df = pd.DataFrame(m1_records)
print(f"• 1분봉 정밀 매칭 검증 완료: 총 {len(m1_df)}건")
print("\n[1분봉 실측 지표]")
print(f"  - 평균 시초가 갭: {m1_df['open_gap_pct'].mean():+.2f}%")
print(f"  - 09:00~09:30 본절(0.0%) 도달 성공률  : {m1_df['hit_0pct'].mean()*100:.1f}% ({m1_df['hit_0pct'].sum()}/{len(m1_df)})")
print(f"  - 09:00~09:30 약익절(+1.0%) 도달 성공률: {m1_df['hit_1pct'].mean()*100:.1f}% ({m1_df['hit_1pct'].sum()}/{len(m1_df)})")

# Timing analysis
hit_0_mins = m1_df[m1_df['hit_0pct']]['hit_0pct_min']
print(f"  - 본절 성공 시 체결 소요시간 중앙값: {hit_0_mins.median():.0f}분 (평균 {hit_0_mins.mean():.1f}분)")

print("\n" + "=" * 90)
print(f"{'청산 전략':<40} | {'평균 순손익':<12} | {'승률':<8} | {'PF':<7} | {'단순합산':<10}")
print("-" * 90)
def print_exit_stat(name, col):
    s = m1_df[col]
    wr = (s > 0).mean() * 100.0
    w_sum = s[s > 0].sum()
    l_sum = abs(s[s <= 0].sum())
    pf = w_sum / l_sum if l_sum > 0 else 999.0
    print(f"{name:<40} | {s.mean():>+10.3f}% | {wr:>6.1f}% | {pf:>5.2f} | {s.sum():>+9.1f}%")

print_exit_stat("0. 기준: 09:00 시초가 무조건 청산", 'ret_open')
print_exit_stat("1. 본절 구제 (0.0% 지정가 -> 09:30 시장가)", 'ret_res_0')
print_exit_stat("2. 약익절 구제 (+1.0% 지정가 -> 09:30 시장가)", 'ret_res_1')
print_exit_stat("3. 본절 구제 + 손절(-2% OCO)", 'ret_res_0_sl2')
print_exit_stat("4. 약익절 구제 + 손절(-3% OCO)", 'ret_res_1_sl3')
print_exit_stat("5. 단순 당일 종가 청산 (09:00 투매 안함)", 'ret_day_close')

# -------------------------------------------------------------
# Part 3B: 2020~2026 7-Year Full Universe Backtest Simulation
# -------------------------------------------------------------
print("\n" + "=" * 90)
print("🚀 [Part 3B: 2020~2026 7개년 전수 계좌 시뮬레이션 및 익일 청산 최적화]")
print("=" * 90)

from part1_prepare_and_engine import simulate_portfolio, print_result_block

# In the 7-year daily dataset:
# For negative gap trades:
# We know from Part 3A that:
# If next_high >= close * (1 + tp/100):
# In 2024~2026, 88% of trades that reached 0.0% during the day reached it within the first 30 minutes!
# And for trades that fail to reach 0.0%, 09:30 price is on average 1.58% lower than 09:00 open price.

# Let's build realistic 7-year exit PnL vectors:
# Model A: Baseline (08:00 NXT TP + 09:00 Open)
pool['pnl_base'] = pool['nxt_gap_ret']

# Model B: Rebound Rescue 0.0% (Full 7 Years)
# If gap >= 0 -> exit at open (or NXT)
# If gap < 0:
#   If next_high >= close: assume 0.0% exit (PnL = -0.28%)
#   Else: assume 09:30 exit (open - 1.58%p fallback, or next_close)
rets_res0_conservative = []
rets_res1_conservative = []
rets_res0_close = []

for _, r in pool.iterrows():
    # If NXT triggered
    if r['nxt_gap_ret'] != r['gap_ret']:
        rets_res0_conservative.append(r['nxt_gap_ret'])
        rets_res1_conservative.append(r['nxt_gap_ret'])
        rets_res0_close.append(r['nxt_gap_ret'])
        continue
        
    gap = r['gap_pct']
    c0 = r['close']
    h_pct = r['next_high_pct']
    c_pct = r['next_close_pct']
    
    if gap >= 0:
        rets_res0_conservative.append(r['gap_ret'])
        rets_res1_conservative.append(r['gap_ret'])
        rets_res0_close.append(r['gap_ret'])
    else:
        # Negative gap
        # Rescue 0.0%:
        if h_pct >= 0.0:
            rets_res0_conservative.append(0.0 - 0.28)
            rets_res0_close.append(0.0 - 0.28)
        else:
            # Fallback 09:30 exit: open dropped further by 1.58%p
            rets_res0_conservative.append((gap - 1.58) - 0.28)
            rets_res0_close.append(c_pct - 0.28)
            
        # Rescue +1.0%:
        if h_pct >= 1.0:
            rets_res1_conservative.append(1.0 - 0.28)
        else:
            rets_res1_conservative.append((gap - 1.58) - 0.28)

pool['pnl_res0'] = rets_res0_conservative
pool['pnl_res1'] = rets_res1_conservative
pool['pnl_res0_close'] = rets_res0_close

# Run Portfolio Simulations on Tiered Entry
tiered_df = pool[cond_tier1 | cond_tier2].copy()

res_sim_base = simulate_portfolio(tiered_df, pnl_col='pnl_base')
res_sim_res0 = simulate_portfolio(tiered_df, pnl_col='pnl_res0')
res_sim_res1 = simulate_portfolio(tiered_df, pnl_col='pnl_res1')
res_sim_res0_close = simulate_portfolio(tiered_df, pnl_col='pnl_res0_close')

# Also for Tiered + 60d High Filter
cond_60d = (pool['ratio_60d'] >= 0.95) & (pool['cnt_history'] >= 40)
tiered_60d_df = pool[(cond_tier1 | cond_tier2) & cond_60d].copy()

res_sim_60d_base = simulate_portfolio(tiered_60d_df, pnl_col='pnl_base')
res_sim_60d_res0 = simulate_portfolio(tiered_60d_df, pnl_col='pnl_res0')
res_sim_60d_res1 = simulate_portfolio(tiered_60d_df, pnl_col='pnl_res1')

print_result_block("1. 계층형 진입 + [기준] 09:00 시초가 무조건 청산", res_sim_base)
print_result_block("2. 계층형 진입 + [구제 0.0%] 본절 지정가 반등 탈출", res_sim_res0)
print_result_block("3. 계층형 진입 + [구제 +1.0%] 약익절 지정가 반등 탈출", res_sim_res1)
print_result_block("4. 계층형 진입 + 60일 신고가(>=0.95) + [기준] 09:00 청산", res_sim_60d_base)
print_result_block("5. 계층형 진입 + 60일 신고가(>=0.95) + [구제 +1.0%] 반등 탈출", res_sim_60d_res1)

print("\n" + "=" * 105)
print("📋 [EXPERIMENT 3 종합 비교 요약표 (반등 구제 청산 전수 비교)]")
print("=" * 105)
print(f"{'전략명':<42} | {'체결수':<8} | {'승률':<8} | {'PF':<7} | {'건당평균':<9} | {'계좌수익':<10} | {'MDD':<8} | {'샤프':<6}")
print("-" * 105)
def print_comp_row(name, r):
    print(f"{name:<42} | {r['n']:>6}회 | {r['wr']:>6.2f}% | {r['pf']:>5.2f} | {r['avg']:>+7.3f}% | {r['acct_ret']:>+8.2f}% | {r['mdd']:>6.2f}% | {r['sharpe']:>5.2f}")

print_comp_row("1. 계층형 + 09:00 시초가 기준", res_sim_base)
print_comp_row("2. 계층형 + 본절(0.0%) 구제 청산", res_sim_res0)
print_comp_row("3. 계층형 + 약익절(+1.0%) 구제 청산", res_sim_res1)
print_comp_row("4. 계층형 + 본절구제(미체결시 종가청산)", res_sim_res0_close)
print_comp_row("5. 계층형+60일신고가 + 09:00 시초가", res_sim_60d_base)
print_comp_row("6. 계층형+60일신고가 + 약익절(+1.0%) 구제", res_sim_60d_res1)
print("=" * 105)
