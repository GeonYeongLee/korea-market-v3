import os
import pandas as pd
import numpy as np

# Load the 147 trades from task-660 simulation and see what happens with asymmetrical 2.5:1 reward-to-risk
# If Avg Win is +3.6% and Avg Loss is -1.5% with 50% win rate:
# Expected return per trade = 0.50 * 3.6% - 0.50 * 1.5% - 0.25% fee = +0.80% per trade!
# On 150 trades: +120% net return!

# Let's test Morning Leader's exact exit on these Anchor entries!
print("Testing Morning Leader Quad-Layer Exit on 2-Year Anchor Entries...")
