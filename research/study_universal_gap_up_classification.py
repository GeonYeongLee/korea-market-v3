"""
Universal Study: Negative Close to Positive Open Gap Rebound Classification
=============================================================================
1. Evaluate optimal turnover rank cutoff for Market Panic Capitulation
2. Investigate and classify ALL daily bars with Close < PrevClose & NextOpen > Close
"""
import pandas as pd
import numpy as np

print("=" * 90)
print("🔬 [종가 음봉 ➔ 익일 시가 갭상승 일봉 전수 조사 및 카테고리 분류 연구]")
print("=" * 90)

print("1. 데이터셋 로드 (2020 ~ 2026)...")
df = pd.read_parquet("data/dataset_2y/daily_bars_all.parquet")
df["date_dt"] = pd.to_datetime(df["date"])
df = df[df["date_dt"] >= "2020-01-01"].copy()
df = df.sort_values(["symbol", "date"]).reset_index(drop=True)

# Next-day open and high return
df["next_open"] = df.groupby("symbol")["open"].shift(-1)
df["next_high"] = df.groupby("symbol")["high"].shift(-1)
df["gap_gross"] = (df["next_open"] - df["close"]) / df["close"] * 100.0
df["gap_net"] = df["gap_gross"] - 0.28
df["next_high_net"] = (df["next_high"] - df["close"]) / df["close"] * 100.0 - 0.28

# Turnover rank & absolute turnover
df["to_rank"] = df.groupby("date")["turnover"].rank(ascending=False, method="min")

# Candle morphology
candle_range = df["high"] - df["low"]
df["lower_shadow"] = np.where(candle_range > 0, (np.minimum(df["open"], df["close"]) - df["low"]) / candle_range, 0.0)
df["upper_shadow"] = np.where(candle_range > 0, (df["high"] - np.maximum(df["open"], df["close"])) / candle_range, 0.0)
df["body_pct"] = (df["close"] - df["open"]) / df["open"] * 100.0
df["open_gap"] = (df["open"] - df["prev_close"]) / df["prev_close"] * 100.0

# Preceding indicators
df["prev_chg"] = df.groupby("symbol")["change_pct"].shift(1)
df["disp5"] = (df["close"] / df["ma5"] - 1.0) * 100.0
df["disp20"] = (df["close"] / df["ma20"] - 1.0) * 100.0
df["disp60"] = (df["close"] / df["ma60"] - 1.0) * 100.0
df["ret_3d"] = (df["close"] / df.groupby("symbol")["close"].shift(3) - 1.0) * 100.0
df["perfect_uptrend"] = (df["close"] > df["ma20"]) & (df["ma20"] > df["ma60"])

# Market daily average return
market_daily = df.groupby("date")["change_pct"].mean().rename("mkt_avg_chg")
df = df.merge(market_daily, on="date", how="left")

# Exclude lower limits (-29% or lower)
df = df[df["change_pct"] > -29.0].copy()

# Base negative close with valid next_open and turnover >= 10억
base_neg = df[(df["change_pct"] < 0) & (df["next_open"].notna()) & (df["turnover"] >= 10e8)].copy()

total_neg = len(base_neg)
gross_gap_up = base_neg[base_neg["gap_gross"] > 0].copy()
net_gap_up = base_neg[base_neg["gap_net"] > 0].copy()

print(f"총 음봉(하락 마감) 표본 수: {total_neg:,}건")
print(f"  • 단순 시가 플러스(Gross Gap > 0%): {len(gross_gap_up):,}건 (전체의 {len(gross_gap_up)/total_neg*100:.2f}%)")
print(f"  • 실전 순수익 시가 플러스(Net Gap > 0%): {len(net_gap_up):,}건 (전체의 {len(net_gap_up)/total_neg*100:.2f}%)")

# ==============================================================================
# PART 1: 시장 패닉 투매형에서 거래대금 순위 최적화
# ==============================================================================
print("\n" + "=" * 90)
print("📊 [PART 1] '시장 패닉 투매형'에서 거래대금 TOP 몇 위까지가 가장 적절한가?")
print("=" * 90)

panic_base = base_neg[(base_neg["mkt_avg_chg"] <= -2.0) & (base_neg["change_pct"] <= -5.0)].copy()
print(f"시장 패닉 투매 종목 총수: {len(panic_base):,}건 (시장 평균 -2% 이하 & 당일 -5% 이하 급락)")

print("\n1) 5위 단위 세부 구간(Bucket) 검증:")
print(f"{'구간':<15} | {'표본':>5} | {'단순승률':>6} | {'실전순승률':>6} | {'평균순수익':>8} | {'장중고가':>8} | {'PF':>5}")
print("-" * 75)
buckets = [
    ("Top 1 ~ 5위", panic_base[panic_base["to_rank"] <= 5]),
    ("Top 6 ~ 10위", panic_base[(panic_base["to_rank"] > 5) & (panic_base["to_rank"] <= 10)]),
    ("Top 11 ~ 15위", panic_base[(panic_base["to_rank"] > 10) & (panic_base["to_rank"] <= 15)]),
    ("Top 16 ~ 20위", panic_base[(panic_base["to_rank"] > 15) & (panic_base["to_rank"] <= 20)]),
    ("Top 21 ~ 25위", panic_base[(panic_base["to_rank"] > 20) & (panic_base["to_rank"] <= 25)]),
    ("Top 26 ~ 30위", panic_base[(panic_base["to_rank"] > 25) & (panic_base["to_rank"] <= 30)]),
    ("Top 31 ~ 40위", panic_base[(panic_base["to_rank"] > 30) & (panic_base["to_rank"] <= 40)]),
    ("Top 41 ~ 50위", panic_base[(panic_base["to_rank"] > 40) & (panic_base["to_rank"] <= 50)]),
    ("Top 51 ~ 70위", panic_base[(panic_base["to_rank"] > 50) & (panic_base["to_rank"] <= 70)]),
    ("Top 71 ~ 100위", panic_base[(panic_base["to_rank"] > 70) & (panic_base["to_rank"] <= 100)]),
    ("100위 밖 (소형)", panic_base[panic_base["to_rank"] > 100]),
]

for name, sub in buckets:
    if len(sub) == 0:
        continue
    g_wr = (sub["gap_gross"] > 0).mean() * 100.0
    n_wr = (sub["gap_net"] > 0).mean() * 100.0
    n_avg = sub["gap_net"].mean()
    h_avg = sub["next_high_net"].mean()
    wins = sub[sub["gap_net"] > 0]["gap_net"].sum()
    losses = abs(sub[sub["gap_net"] <= 0]["gap_net"].sum())
    pf = wins / losses if losses > 0 else 999.0
    print(f"{name:<15} | {len(sub):>5}건 | {g_wr:5.1f}% | {n_wr:5.1f}% | {n_avg:+7.3f}% | {h_avg:+7.2f}% | {pf:5.2f}")

print("\n2) 누적 컷오프(Cumulative Cutoff) 비교:")
print(f"{'컷오프':<15} | {'표본':>5} | {'단순승률':>6} | {'실전순승률':>6} | {'평균순수익':>8} | {'장중고가':>8} | {'PF':>5}")
print("-" * 75)
cum_cuts = [
    ("Top 5 이내", panic_base[panic_base["to_rank"] <= 5]),
    ("Top 10 이내", panic_base[panic_base["to_rank"] <= 10]),
    ("Top 15 이내", panic_base[panic_base["to_rank"] <= 15]),
    ("Top 20 이내", panic_base[panic_base["to_rank"] <= 20]),
    ("Top 25 이내", panic_base[panic_base["to_rank"] <= 25]),
    ("Top 30 이내", panic_base[panic_base["to_rank"] <= 30]),
    ("Top 40 이내", panic_base[panic_base["to_rank"] <= 40]),
    ("Top 50 이내", panic_base[panic_base["to_rank"] <= 50]),
    ("Top 100 이내", panic_base[panic_base["to_rank"] <= 100]),
]
for name, sub in cum_cuts:
    if len(sub) == 0:
        continue
    g_wr = (sub["gap_gross"] > 0).mean() * 100.0
    n_wr = (sub["gap_net"] > 0).mean() * 100.0
    n_avg = sub["gap_net"].mean()
    h_avg = sub["next_high_net"].mean()
    wins = sub[sub["gap_net"] > 0]["gap_net"].sum()
    losses = abs(sub[sub["gap_net"] <= 0]["gap_net"].sum())
    pf = wins / losses if losses > 0 else 999.0
    print(f"{name:<15} | {len(sub):>5}건 | {g_wr:5.1f}% | {n_wr:5.1f}% | {n_avg:+7.3f}% | {h_avg:+7.2f}% | {pf:5.2f}")

print("\n3) 절대 거래대금(Turnover) 규모별 검증:")
print(f"{'거래대금 규모':<25} | {'표본':>5} | {'순승률':>6} | {'평균순수익':>8} | {'장중고가':>8} | {'PF':>5}")
print("-" * 75)
to_tiers = [
    ("1,000억 이상 (초대형)", panic_base[panic_base["turnover"] >= 1000e8]),
    ("500억 ~ 1,000억 (대형)", panic_base[(panic_base["turnover"] >= 500e8) & (panic_base["turnover"] < 1000e8)]),
    ("300억 ~ 500억 (중대형)", panic_base[(panic_base["turnover"] >= 300e8) & (panic_base["turnover"] < 500e8)]),
    ("100억 ~ 300억 (중형)", panic_base[(panic_base["turnover"] >= 100e8) & (panic_base["turnover"] < 300e8)]),
    ("50억 ~ 100억 (중소형)", panic_base[(panic_base["turnover"] >= 50e8) & (panic_base["turnover"] < 100e8)]),
    ("10억 ~ 50억 (소형)", panic_base[(panic_base["turnover"] >= 10e8) & (panic_base["turnover"] < 50e8)]),
]
for name, sub in to_tiers:
    if len(sub) == 0:
        continue
    n_wr = (sub["gap_net"] > 0).mean() * 100.0
    n_avg = sub["gap_net"].mean()
    h_avg = sub["next_high_net"].mean()
    wins = sub[sub["gap_net"] > 0]["gap_net"].sum()
    losses = abs(sub[sub["gap_net"] <= 0]["gap_net"].sum())
    pf = wins / losses if losses > 0 else 999.0
    print(f"{name:<25} | {len(sub):>5}건 | {n_wr:5.1f}% | {n_avg:+7.3f}% | {h_avg:+7.2f}% | {pf:5.2f}")


# ==============================================================================
# PART 2: 종가 음봉 ➔ 익일 시가 갭상승 일봉 전수 조사 및 카테고리 분류
# ==============================================================================
print("\n" + "=" * 90)
print("🏷️ [PART 2] 종가 마이너스 ➔ 다음날 시가 플러스 일봉 전수 조사 및 실증 카테고리 분류")
print("=" * 90)

# Categories
# 1. 시장 패닉 투매 반등형 (Market Panic Capitulation)
mask_panic = (base_neg["mkt_avg_chg"] <= -1.8) & (base_neg["change_pct"] <= -4.0) & (base_neg["to_rank"] <= 50)

# 2. 주도주/대장주 첫 음봉 눌림목 (Leader Anchor Pullback)
mask_leader = (~mask_panic) & (base_neg["prev_chg"] >= 12.0) & (base_neg["to_rank"] <= 30) & (base_neg["change_pct"] >= -7.0)

# 3. 단기 극단 이격 과매도 (Extreme Disparity Oversold)
mask_oversold = (~mask_panic) & (~mask_leader) & (base_neg["disp5"] <= -8.0) & (base_neg["to_rank"] <= 50)

# 4. 정배열 상승추세 건전한 숨고르기 (Uptrend Healthy Pause)
mask_uptrend = (~mask_panic) & (~mask_leader) & (~mask_oversold) & (base_neg["perfect_uptrend"]) & (base_neg["change_pct"] >= -4.0) & (base_neg["to_rank"] <= 50)

# 5. 거래대금 상위 일반 음봉 (Top 50 Ordinary Red Candle)
mask_top50_ord = (~mask_panic) & (~mask_leader) & (~mask_oversold) & (~mask_uptrend) & (base_neg["to_rank"] <= 50)

# 6. 중형주 일반 음봉 (Rank 51 ~ 100)
mask_mid = (~mask_panic) & (~mask_leader) & (~mask_oversold) & (~mask_uptrend) & (~mask_top50_ord) & (base_neg["to_rank"] <= 100)

# 7. 소형/잡주 무작위 요동 노이즈 (Small-cap Noise: Rank > 100)
mask_small = (~mask_panic) & (~mask_leader) & (~mask_oversold) & (~mask_uptrend) & (~mask_top50_ord) & (~mask_mid)

categories = [
    ("1. 시장 패닉 투매 반등형", mask_panic, "지수 폭락(-1.8%이하) + 투매(-4%이하) + 유동성 상위"),
    ("2. 주도주 첫 눌림목형", mask_leader, "전일 대장주 급등(+12%이상) + 당일 첫 약음봉 + Top 30"),
    ("3. 단기 이격 과매도형", mask_oversold, "5일선 이격 -8%이하 단기 급락 클라이맥스 + Top 50"),
    ("4. 정배열 건전 숨고르기형", mask_uptrend, "이평선 정배열 추세 + -4%이내 미세 음봉 + Top 50"),
    ("5. 대형주 일반 음봉형", mask_top50_ord, "Top 50위 이내이나 특이 시그널 없는 일반 음봉"),
    ("6. 중형주 일반 음봉형", mask_mid, "51~100위 중형주 일반 음봉"),
    ("7. 소형/잡주 무작위 노이즈", mask_small, "100위 밖 소형주 (단순 변동성 노이즈)"),
]

print(f"{'카테고리 명칭':<22} | {'전체음봉':>8} | {'시가+건수':>8} | {'단순승률':>6} | {'실전순승률':>6} | {'평균순수익':>8} | {'PF':>5} | {'성공점유율':>6}")
print("-" * 95)

for name, mask, desc in categories:
    sub = base_neg[mask]
    n_total = len(sub)
    n_gross = (sub["gap_gross"] > 0).sum()
    n_net = (sub["gap_net"] > 0).sum()
    g_wr = n_gross / n_total * 100.0 if n_total > 0 else 0
    n_wr = n_net / n_total * 100.0 if n_total > 0 else 0
    n_avg = sub["gap_net"].mean() if n_total > 0 else 0
    wins = sub[sub["gap_net"] > 0]["gap_net"].sum()
    losses = abs(sub[sub["gap_net"] <= 0]["gap_net"].sum())
    pf = wins / losses if losses > 0 else 0
    share = n_net / len(net_gap_up) * 100.0
    print(f"{name:<22} | {n_total:>8,} | {n_net:>8,} | {g_wr:5.1f}% | {n_wr:5.1f}% | {n_avg:+7.3f}% | {pf:5.2f} | {share:5.1f}%")

print("=" * 95)
