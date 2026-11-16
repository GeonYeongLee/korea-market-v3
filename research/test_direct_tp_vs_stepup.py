import pandas as pd
import numpy as np

# Load original Q1 trades where direct TP was active
df = pd.read_csv('anchor_q1_trades.csv')
print(f"Total trades: {len(df)}")
# In original trades, TP was direct target at +3.0%
# 37 trades hit TAKE_PROFIT_TARGET1 (+3.82% avg return!)
# 16 trades hit TRAILING_STOP (+0.0% avg return)
# 45 trades hit STOP_LOSS (-3.86% avg return)

print("\n--- SIMULATION: Direct TP (+3.0%) with Different Stop-Loss Depths ---")
for sl in [-1.8, -2.0, -2.2, -2.5, -3.0]:
    # If mae <= sl, trade gets sl - 0.25
    # Elif mfe >= 3.0, trade gets 3.0 - 0.25 = +2.75
    # Else eod close
    rets = []
    for _, tr in df.iterrows():
        mae = tr['mae_pct']
        mfe = tr['mfe_pct']
        if mae <= sl:
            rets.append(sl - 0.25)
        elif mfe >= 3.0:
            rets.append(2.75)
        else:
            rets.append(tr['return_pct'])
    s = pd.Series(rets)
    w = s[s > 0]
    l = s[s <= 0]
    pf = w.sum() / abs(l.sum()) if len(l) > 0 and l.sum() != 0 else 0
    print(f"SL {sl}%: WinRate={len(w)/len(s)*100:.1f}%, NetPnL={s.sum():.2f}%, PF={pf:.2f}, AvgWin=+{w.mean():.2f}%, AvgLoss={l.mean():.2f}%")
