"""
Quantitative Investigation: Oversold Negative Close to Next-day Open Gap Rebound
================================================================================
Goal: Identify archetypes and features of stocks that close negative today
      but reliably gap up at market open tomorrow.
Period: 2020 ~ 2026 (Korean Equity Market)
"""
import pandas as pd
import numpy as np

print("=" * 80)
print("🔬 [낙폭과대 반등 종가베팅 실증 연구: 음봉/하락 마감 ➔ 익일 시초가 갭상승 종목 탐색]")
print("=" * 80)

print("1. 데이터셋 로드 (2020 ~ 2026)...")
df = pd.read_parquet("data/dataset_2y/daily_bars_all.parquet")
df["date_dt"] = pd.to_datetime(df["date"])
df = df[df["date_dt"] >= "2020-01-01"].copy()
df = df.sort_values(["symbol", "date"]).reset_index(drop=True)

# Next-day open return (gross & net of 0.28% roundtrip tax & fee)
df["next_open"] = df.groupby("symbol")["open"].shift(-1)
df["gap_gross"] = (df["next_open"] - df["close"]) / df["close"] * 100.0
df["gap_net"] = df["gap_gross"] - 0.28

# Turnover rank per day
df["to_rank"] = df.groupby("date")["turnover"].rank(ascending=False, method="min")

# Candle morphology
candle_range = df["high"] - df["low"]
df["lower_shadow"] = np.where(candle_range > 0, (np.minimum(df["open"], df["close"]) - df["low"]) / candle_range, 0.0)
df["upper_shadow"] = np.where(candle_range > 0, (df["high"] - np.maximum(df["open"], df["close"])) / candle_range, 0.0)
df["close_loc"] = np.where(candle_range > 0, (df["close"] - df["low"]) / candle_range, 0.5)
df["body_ret"] = (df["close"] - df["open"]) / df["open"] * 100.0

# Trend & Volume features
df["above_ma20"] = df["close"] > df["ma20"]
df["above_ma60"] = df["close"] > df["ma60"]
df["perfect_uptrend"] = (df["close"] > df["ma20"]) & (df["ma20"] > df["ma60"])
df["vol_surge"] = df["volume"] / (df["adto20"] / df["close"].replace(0, np.nan))

# Base population: All stocks closing negative (change_pct < 0) with valid next open and minimum liquidity
base = df[(df["change_pct"] < 0) & (df["next_open"].notna()) & (df["turnover"] >= 10e8)].copy()

print(f"총 분석 하락 마감 표본 수: {len(base):,}건")
base_wr = (base["gap_net"] > 0).mean() * 100.0
base_avg = base["gap_net"].mean()
print(f"전체 평균 베이스라인: 순승률(Net WR) = {base_wr:.2f}% | 건당 평균 순손익 = {base_avg:+.3f}%\n")

# ============================================================================
# [실험 1] 거래대금 순위별 (대형 주도주 투매 vs 개별 소형주 투매)
# ============================================================================
print("=" * 80)
print("📊 [실험 1] 거래대금 순위별 익일 시가 갭 반등 확률")
print("=" * 80)
tiers = [
    ("Top 1 ~ 10위 (초대형)", base[base["to_rank"] <= 10]),
    ("Top 11 ~ 20위 (대형)", base[(base["to_rank"] > 10) & (base["to_rank"] <= 20)]),
    ("Top 21 ~ 30위 (중대형)", base[(base["to_rank"] > 20) & (base["to_rank"] <= 30)]),
    ("Top 31 ~ 50위 (중형)", base[(base["to_rank"] > 30) & (base["to_rank"] <= 50)]),
    ("Top 51 ~ 100위 (중소형)", base[(base["to_rank"] > 50) & (base["to_rank"] <= 100)]),
    ("100위 밖 (소형/잡주)", base[base["to_rank"] > 100]),
]
for name, sub in tiers:
    wr = (sub["gap_net"] > 0).mean() * 100.0
    avg_p = sub["gap_net"].mean()
    pf = sub[sub["gap_net"] > 0]["gap_net"].sum() / abs(sub[sub["gap_net"] <= 0]["gap_net"].sum()) if len(sub) > 0 else 0
    print(f"{name:<20} | 표본: {len(sub):>7,}건 | 승률: {wr:5.1f}% | 평균손익: {avg_p:+.3f}% | PF: {pf:.2f}")

# ============================================================================
# [실험 2] 낙폭 크기별 (살짝 눌림 vs 폭락 투매)
# ============================================================================
print("\n" + "=" * 80)
print("📊 [실험 2] 당일 하락폭(change_pct) 크기별 익일 시가 갭 반등")
print("=" * 80)
drop_bins = [
    ("미세 조정 (-0% ~ -2%)", base[(base["change_pct"] >= -2.0) & (base["change_pct"] < 0.0)]),
    ("약한 눌림 (-2% ~ -4%)", base[(base["change_pct"] >= -4.0) & (base["change_pct"] < -2.0)]),
    ("중간 하락 (-4% ~ -7%)", base[(base["change_pct"] >= -7.0) & (base["change_pct"] < -4.0)]),
    ("급락 투매 (-7% ~ -12%)", base[(base["change_pct"] >= -12.0) & (base["change_pct"] < -7.0)]),
    ("폭락/패닉 (-12% 이하)", base[base["change_pct"] < -12.0]),
]
for name, sub in drop_bins:
    wr = (sub["gap_net"] > 0).mean() * 100.0
    avg_p = sub["gap_net"].mean()
    pf = sub[sub["gap_net"] > 0]["gap_net"].sum() / abs(sub[sub["gap_net"] <= 0]["gap_net"].sum()) if len(sub) > 0 else 0
    print(f"{name:<20} | 표본: {len(sub):>7,}건 | 승률: {wr:5.1f}% | 평균손익: {avg_p:+.3f}% | PF: {pf:.2f}")

# ============================================================================
# [실험 3] 아래꼬리 비율 (장중 저가 대비 매수세 유입 정도)
# ============================================================================
print("\n" + "=" * 80)
print("📊 [실험 3] 아래꼬리(Lower Shadow) 형성 여부별 반등 확률")
print("=" * 80)
shadow_bins = [
    ("아래꼬리 없음 (< 10%)", base[base["lower_shadow"] < 0.10]),
    ("약한 아래꼬리 (10% ~ 30%)", base[(base["lower_shadow"] >= 0.10) & (base["lower_shadow"] < 0.30)]),
    ("중간 아래꼬리 (30% ~ 50%)", base[(base["lower_shadow"] >= 0.30) & (base["lower_shadow"] < 0.50)]),
    ("강한 아래꼬리 망치형 (50% ~ 70%)", base[(base["lower_shadow"] >= 0.50) & (base["lower_shadow"] < 0.70)]),
    ("극단적 역전 아래꼬리 (>= 70%)", base[base["lower_shadow"] >= 0.70]),
]
for name, sub in shadow_bins:
    wr = (sub["gap_net"] > 0).mean() * 100.0
    avg_p = sub["gap_net"].mean()
    pf = sub[sub["gap_net"] > 0]["gap_net"].sum() / abs(sub[sub["gap_net"] <= 0]["gap_net"].sum()) if len(sub) > 0 else 0
    print(f"{name:<25} | 표본: {len(sub):>7,}건 | 승률: {wr:5.1f}% | 평균손익: {avg_p:+.3f}% | PF: {pf:.2f}")

# ============================================================================
# [실험 4] 중기 추세 (이평선 정배열 눌림 vs 역배열 칼날 잡기)
# ============================================================================
print("\n" + "=" * 80)
print("📊 [실험 4] 장기/중기 추세 필터별 반등 확률")
print("=" * 80)
trend_bins = [
    ("역배열 추세 하락 (Close < MA20 & Close < MA60)", base[(~base["above_ma20"]) & (~base["above_ma60"])]),
    ("MA60 지지선 위 (Close >= MA60)", base[base["above_ma60"]]),
    ("MA20 지지선 위 (Close >= MA20)", base[base["above_ma20"]]),
    ("완전 정배열 강세장 (Close > MA20 > MA60)", base[base["perfect_uptrend"]]),
]
for name, sub in trend_bins:
    wr = (sub["gap_net"] > 0).mean() * 100.0
    avg_p = sub["gap_net"].mean()
    pf = sub[sub["gap_net"] > 0]["gap_net"].sum() / abs(sub[sub["gap_net"] <= 0]["gap_net"].sum()) if len(sub) > 0 else 0
    print(f"{name:<45} | 표본: {len(sub):>7,}건 | 승률: {wr:5.1f}% | 평균손익: {avg_p:+.3f}% | PF: {pf:.2f}")
