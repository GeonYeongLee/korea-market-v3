import pandas as pd
import numpy as np

# Load candidate pool
df_cand = pd.read_parquet("research/round2_candidate_pool.parquet")
champ_b = df_cand[(df_cand["univ"] == 1) & (df_cand["rule"] == "Micro")].copy()
champ_b["date_str"] = champ_b["date"].astype(str)
champ_b = champ_b.sort_values(["date_str", "score_alpha1"], ascending=[True, False]).reset_index(drop=True)

# Champion B trades (max 2 slots per day)
cb_trades = champ_b.groupby("date_str").head(2).copy()
cb_trades["is_cb"] = True
cb_panic_days = set(cb_trades["date_str"].unique())

# Load Daily Bars for Crisis Rebound
df = pd.read_parquet("data/dataset_2y/daily_bars_all.parquet")
df["date_str"] = df["date"].astype(str)
df = df[df["date_str"] >= "2020-01-01"].copy()
df = df.sort_values(["symbol", "date_str"]).reset_index(drop=True)
df["next_open"] = df.groupby("symbol")["open"].shift(-1)
df["gap_net"] = (df["next_open"] - df["close"]) / df["close"] * 100.0 - 0.28
df["to_rank"] = df.groupby("date_str")["turnover"].rank(ascending=False, method="min")
df = df[df["change_pct"] > -29.0].copy()

# Load Index Data (KOSPI & KOSDAQ)
df_idx = pd.read_parquet("data/dataset_2y/index_daily_full.parquet")
df_idx["date_str"] = df_idx["date"].astype(str)
df_idx = df_idx.sort_values("date_str").reset_index(drop=True)
df_idx["ks_prev"] = df_idx["ks_close"].shift(1)
df_idx["kq_prev"] = df_idx["kq_close"].shift(1)
df_idx["ks_chg"] = (df_idx["ks_close"] - df_idx["ks_prev"]) / df_idx["ks_prev"] * 100.0
df_idx["kq_chg"] = (df_idx["kq_close"] - df_idx["kq_prev"]) / df_idx["kq_prev"] * 100.0

df = df.merge(df_idx[["date_str", "ks_chg", "kq_chg"]], on="date_str", how="left")
all_ew = df.groupby("date_str")["change_pct"].mean().rename("all_ew")
df = df.merge(all_ew, on="date_str", how="left")

valid = df[df["next_open"].notna()].copy()
valid["open_gap"] = (valid["open"] - valid["prev_close"]) / valid["prev_close"] * 100.0
valid["body_pct"] = (valid["close"] - valid["open"]) / valid["open"] * 100.0

# Compare Integration Performance
triggers = [
    ("1. [가상 지표] 전종목 EW <= -2.0% (실전 조회 불가 -> 괴리 발생)", valid["all_ew"] <= -2.0),
    ("2. [실전 100% 일치] 코스닥 지수 <= -2.0% (TR FHPUP02100000)", valid["kq_chg"] <= -2.0),
    ("3. [실전 100% 일치] 코스닥 지수 <= -1.8% (TR FHPUP02100000)", valid["kq_chg"] <= -1.8),
    ("4. [실전 100% 일치] 양대지수 중 하나라도 <= -2.0%", np.minimum(valid["ks_chg"], valid["kq_chg"]) <= -2.0),
]

print("=" * 115)
print("🚀 [Champion B + Crisis Rebound 결합 성과: 실전 조회 가능 지표 vs 가상 지표]")
print("=" * 115)
print(f"{'트리거 지표 (백테스트 == 실전 1:1 일치)':<45} | {'총거래':>5} | {'승률':>6} | {'건당순익':>8} | {'PF':>5} | {'순손익(천원)':>11} | {'MDD':>7}")
print("-" * 115)

# Baseline: Standalone Champion B
init_cap = 10_000_000.0
cap = init_cap
peak = cap
mdd = 0.0
for _, r in cb_trades.iterrows():
    ret = r["nxt_gap_ret"] / 100.0
    slot_cap = cap * 0.50
    pnl = slot_cap * ret
    cap += pnl
    peak = max(peak, cap)
    dd = (cap - peak) / peak * 100.0
    mdd = min(mdd, dd)

wr_b = (cb_trades["nxt_gap_ret"] > 0).mean() * 100.0
avg_b = cb_trades["nxt_gap_ret"].mean()
wins_b = cb_trades[cb_trades["nxt_gap_ret"] > 0]["nxt_gap_ret"].sum()
losses_b = abs(cb_trades[cb_trades["nxt_gap_ret"] <= 0]["nxt_gap_ret"].sum())
pf_b = wins_b / losses_b
print(f"{'0. [기준선] 단독 챔피언 B (평상시 2슬롯)':<45} | {len(cb_trades):>5}건 | {wr_b:5.1f}% | {avg_b:+7.3f}% | {pf_b:5.2f} | {int((cap - init_cap)/1000):>11,d} | {mdd:6.2f}%")

for name, mask in triggers:
    # Pattern C & Pattern A candidates
    c_cond = mask & (valid["change_pct"] < 0.0) & (valid["open_gap"] <= -1.0) & (valid["body_pct"] >= 0.5) & (valid["to_rank"] <= 20)
    a_cond = mask & (valid["change_pct"] <= -6.0) & (valid["to_rank"] <= 20)
    
    sub_c = valid[c_cond].copy()
    sub_c["pattern"] = "C"
    sub_c["priority"] = 1
    
    sub_a = valid[a_cond & (~c_cond)].copy()
    sub_a["pattern"] = "A"
    sub_a["priority"] = 2
    
    crisis_all = pd.concat([sub_c, sub_a], ignore_index=True)
    crisis_all = crisis_all.sort_values(["date_str", "priority", "to_rank"]).reset_index(drop=True)
    
    # Fallback simulation: On days where Champion B has 0 candidates, take top 2 crisis candidates
    crisis_fallback = crisis_all[~crisis_all["date_str"].isin(cb_panic_days)].copy()
    crisis_selected = crisis_fallback.groupby("date_str").head(2).copy()
    crisis_selected["nxt_gap_ret"] = crisis_selected["gap_net"]
    crisis_selected["is_cb"] = False
    
    combined = pd.concat([
        cb_trades[["date_str", "symbol", "nxt_gap_ret", "is_cb"]],
        crisis_selected[["date_str", "symbol", "nxt_gap_ret", "is_cb"]]
    ], ignore_index=True)
    combined = combined.sort_values(["date_str", "is_cb"], ascending=[True, False]).reset_index(drop=True)
    
    # Portfolio simulation
    cap = init_cap
    peak = cap
    mdd = 0.0
    for _, r in combined.iterrows():
        ret = r["nxt_gap_ret"] / 100.0
        slot_cap = cap * 0.50
        pnl = slot_cap * ret
        cap += pnl
        peak = max(peak, cap)
        dd = (cap - peak) / peak * 100.0
        mdd = min(mdd, dd)
        
    wr = (combined["nxt_gap_ret"] > 0).mean() * 100.0
    avg_p = combined["nxt_gap_ret"].mean()
    wins = combined[combined["nxt_gap_ret"] > 0]["nxt_gap_ret"].sum()
    losses = abs(combined[combined["nxt_gap_ret"] <= 0]["nxt_gap_ret"].sum())
    pf = wins / losses if losses > 0 else 0
    print(f"{name:<45} | {len(combined):>5}건 | {wr:5.1f}% | {avg_p:+7.3f}% | {pf:5.2f} | {int((cap - init_cap)/1000):>11,d} | {mdd:6.2f}%")

print("=" * 115)
