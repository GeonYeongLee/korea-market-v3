"""
Study: Frequency and Distribution of Pattern A & Pattern C
===========================================================
Pattern A: Market Panic Capitulation
Pattern C: Panic Day Gap-Down then Green Rebound
"""
import pandas as pd
import numpy as np

print("Loading dataset...")
df = pd.read_parquet("data/dataset_2y/daily_bars_all.parquet")
df["date_dt"] = pd.to_datetime(df["date"])
df = df[df["date_dt"] >= "2020-01-01"].copy()
df["year"] = df["date_dt"].dt.year
df["month"] = df["date_dt"].dt.to_period("M")
df = df.sort_values(["symbol", "date"]).reset_index(drop=True)

# Next open and high
df["next_open"] = df.groupby("symbol")["open"].shift(-1)
df["next_high"] = df.groupby("symbol")["high"].shift(-1)
df["gap_gross"] = (df["next_open"] - df["close"]) / df["close"] * 100.0
df["gap_net"] = df["gap_gross"] - 0.28
df["next_high_net"] = (df["next_high"] - df["close"]) / df["close"] * 100.0 - 0.28

# Turnover rank & market average
df["to_rank"] = df.groupby("date")["turnover"].rank(ascending=False, method="min")
market_daily = df.groupby("date")["change_pct"].mean().rename("mkt_avg_chg")
df = df.merge(market_daily, on="date", how="left")

# Exclude lower limits
df = df[df["change_pct"] > -29.0].copy()

# Candle morphology
df["open_gap"] = (df["open"] - df["prev_close"]) / df["prev_close"] * 100.0
df["body_pct"] = (df["close"] - df["open"]) / df["open"] * 100.0

# Pattern A: Market Panic Capitulation (mkt <= -2.0%, change <= -5.0%)
cond_a_top20 = (df["mkt_avg_chg"] <= -2.0) & (df["change_pct"] <= -5.0) & (df["to_rank"] <= 20) & df["next_open"].notna()
cond_a_top30 = (df["mkt_avg_chg"] <= -2.0) & (df["change_pct"] <= -5.0) & (df["to_rank"] <= 30) & df["next_open"].notna()

# Pattern C: Panic Day Gap-Down then Green Rebound (mkt <= -2.0%, change < 0, open_gap <= -1.0%, body_pct >= 0)
cond_c_top20 = (df["mkt_avg_chg"] <= -2.0) & (df["change_pct"] < 0.0) & (df["open_gap"] <= -1.0) & (df["body_pct"] >= 0.0) & (df["to_rank"] <= 20) & df["next_open"].notna()
cond_c_top30 = (df["mkt_avg_chg"] <= -2.0) & (df["change_pct"] < 0.0) & (df["open_gap"] <= -1.0) & (df["body_pct"] >= 0.0) & (df["to_rank"] <= 30) & df["next_open"].notna()

# Union A or C
cond_union_top20 = cond_a_top20 | cond_c_top20
cond_union_top30 = cond_a_top30 | cond_c_top30

def analyze_frequency(mask, title):
    sub = df[mask].copy()
    unique_dates = sub["date"].nunique()
    total_trades = len(sub)
    total_trading_days = df["date"].nunique()
    total_months = df["month"].nunique()
    
    print("=" * 85)
    print(f"📌 [{title}] 빈도 및 실전 성과 분석")
    print("=" * 85)
    print(f"총 발생 거래 수: {total_trades:,}건 | 총 신호 발생 일수: {unique_dates}일 (전체 거래일 {total_trading_days}일 중 {unique_dates/total_trading_days*100:.1f}%)")
    print(f"월평균 발생 일수: {unique_dates/total_months:.2f}일/월 | 월평균 거래 건수: {total_trades/total_months:.2f}건/월")
    print(f"신호 발생일 당 평균 종목 수: {total_trades/unique_dates:.1f}종목/일")
    
    wr_net = (sub["gap_net"] > 0).mean() * 100.0
    avg_net = sub["gap_net"].mean()
    high_net = sub["next_high_net"].mean()
    wins = sub[sub["gap_net"] > 0]["gap_net"].sum()
    losses = abs(sub[sub["gap_net"] <= 0]["gap_net"].sum())
    pf = wins / losses if losses > 0 else 0
    print(f"실전 성과: 순승률 = {wr_net:.1f}% | 건당 순손익 = {avg_net:+.3f}% | 익일 장중고가 = {high_net:+.2f}% | PF = {pf:.2f}")
    
    print("\n[연도별 발생 일수 및 거래 건수]")
    print(f"{'연도':<6} | {'발생일수':>6} | {'거래건수':>6} | {'일평균건수':>7} | {'순승률':>6} | {'평균순손익':>8} | {'PF':>5}")
    print("-" * 65)
    for yr in sorted(df["year"].unique()):
        sub_yr = sub[sub["year"] == yr]
        yr_dates = sub_yr["date"].nunique()
        yr_trades = len(sub_yr)
        if yr_trades == 0:
            print(f"{yr}년   |    0일 |    0건 |    0.0개 |    0.0% |   +0.000% |  0.00")
            continue
        y_wr = (sub_yr["gap_net"] > 0).mean() * 100.0
        y_avg = sub_yr["gap_net"].mean()
        y_wins = sub_yr[sub_yr["gap_net"] > 0]["gap_net"].sum()
        y_losses = abs(sub_yr[sub_yr["gap_net"] <= 0]["gap_net"].sum())
        y_pf = y_wins / y_losses if y_losses > 0 else 999.0
        print(f"{yr}년   | {yr_dates:>4}일 | {yr_trades:>4}건 | {yr_trades/yr_dates:>6.1f}개 | {y_wr:5.1f}% | {y_avg:+7.3f}% | {y_pf:5.2f}")
    print("\n")

analyze_frequency(cond_a_top20, "패턴 A: 시장 패닉 투매형 (Top 20)")
analyze_frequency(cond_c_top20, "패턴 C: 패닉일 갭하락 후 양봉 반등 (Top 20)")
analyze_frequency(cond_union_top20, "패턴 A + C 결합 모델 (Top 20)")
analyze_frequency(cond_union_top30, "패턴 A + C 결합 모델 (Top 30)")
