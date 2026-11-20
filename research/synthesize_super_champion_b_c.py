import duckdb
import pandas as pd
import numpy as np

def run():
    base_dir = 'D:/repo/korea-market_v3-anchor'
    pool = pd.read_parquet(f'{base_dir}/research/round2_candidate_pool.parquet')
    bars_path = f'{base_dir}/data/dataset_2y/daily_bars_all.parquet'
    idx_path = f'{base_dir}/data/dataset_2y/index_daily_full.parquet'
    nxt_path = f'{base_dir}/research/nxt_trades_merged.parquet'
    con = duckdb.connect()

    all_trading_days = sorted(con.execute(f"SELECT DISTINCT strftime(date, '%Y-%m-%d') as d FROM '{bars_path}' WHERE date >= '2020-01-01'").df()['d'])

    # Regime map
    idx_df = con.execute(f"""
        SELECT date as date_str,
               ((ks_ma10 >= ks_ma20) AND (ks_close >= ks_ma60)) as is_bull_strong,
               (ks_ma10 >= ks_ma20) as is_uptrend
        FROM '{idx_path}'
        WHERE date >= '2020-01-01'
    """).df().set_index('date_str')
    regime_map = idx_df.to_dict(orient='index')

    # Load NXT
    nxt_df = pd.read_parquet(nxt_path)
    nxt_valid = nxt_df[nxt_df['pm_close'].notna()].copy()
    nxt_tp_maps = {}
    for tp_val in [2.0, 2.5, 3.0]:
        m = {}
        for (sym, nd), group in nxt_valid.groupby(['symbol', 'exit_date']):
            entry_p = group.iloc[0]['entry_price']
            first_open = group.iloc[0]['pm_open']
            max_high = group['pm_high'].max()
            target = entry_p * (1.0 + tp_val / 100.0)
            if max_high >= target:
                m[(sym, nd)] = max(first_open, target)
        nxt_tp_maps[tp_val] = m

    # next_date_str already exists in pool
    for tp_val in [2.0, 2.5, 3.0]:
        tp_m = nxt_tp_maps[tp_val]
        pnls = []
        for _, r in pool.iterrows():
            key = (r['symbol'], r['next_date_str'])
            if key in tp_m:
                exit_p = tp_m[key]
                p = ((exit_p - r['close']) / r['close'] * 100.0) - 0.28
            else:
                p = r['pnl_open']
            pnls.append(p)
        pool[f'pnl_nxt_{int(tp_val*10)}'] = pnls

    # Alpha Score
    pool['s_rank'] = (20.0 - pool['to_rank'].clip(1, 20)) / 19.0
    pool['s_shadow'] = (0.10 - pool['upper_shadow'].clip(0, 0.10)) / 0.10
    pool['s_60d'] = (pool['ratio_60d'].clip(0.95, 1.10) - 0.95) / 0.15
    pool['s_body'] = (pool['body_ret'].clip(5.0, 15.0) - 5.0) / 10.0
    pool['score_alpha1'] = pool['s_rank'] * 0.25 + pool['s_shadow'] * 0.35 + pool['s_60d'] * 0.20 + pool['s_body'] * 0.20

    initial_cap = 10_000_000.0

    def sim_engine(cands_df, pnl_col, bull_cap, bear_cap, reg_name='is_bull_strong', max_slots=2):
        sorted_sub = cands_df.sort_values(['date_str', 'score_alpha1'], ascending=[True, False])
        date_groups = {}
        for d, grp in sorted_sub.groupby('date_str'):
            date_groups[d] = grp[pnl_col].tolist()

        equity = initial_cap
        curve = np.zeros(len(all_trading_days), dtype=np.float64)
        tot_trades = 0
        trade_pnls = []
        yearly_pnl = {'2020': 0.0, '2021': 0.0, '2022': 0.0, '2023': 0.0, '2024': 0.0, '2025': 0.0, '2026': 0.0}

        for idx, d in enumerate(all_trading_days):
            is_bull = regime_map.get(d, {}).get(reg_name, False)
            cap = bull_cap if is_bull else bear_cap

            if d in date_groups:
                pnls = date_groups[d][:max_slots]
                k = len(pnls)
                if k > 0:
                    tot_trades += k
                    pct = min(cap, 1.0 / k)
                    alloc = initial_cap * pct
                    yr = d[:4]
                    day_pnl = 0.0
                    for pnl_val in pnls:
                        amt = alloc * (pnl_val / 100.0)
                        day_pnl += amt
                        trade_pnls.append(pnl_val)
                        if yr in yearly_pnl:
                            yearly_pnl[yr] += amt
                    equity += day_pnl
            curve[idx] = equity

        peaks = np.maximum.accumulate(curve)
        dds = (curve - peaks) / peaks * 100.0
        mdd = np.min(dds)
        profit = equity - initial_cap
        ret = profit / initial_cap * 100.0

        pnl_arr = np.array(trade_pnls)
        if len(pnl_arr) > 0:
            wr = np.mean(pnl_arr > 0) * 100.0
            pos = pnl_arr[pnl_arr > 0].sum()
            neg = np.abs(pnl_arr[pnl_arr <= 0].sum())
            pf = pos / neg if neg > 0 else 999.0
            avg_trade = np.mean(pnl_arr)
        else:
            wr, pf, avg_trade = 0.0, 0.0, 0.0

        daily_rets = np.diff(curve) / curve[:-1]
        std_ret = np.std(daily_rets)
        sharpe = (np.mean(daily_rets) / std_ret * np.sqrt(252)) if std_ret > 0 else 0.0
        losing_yrs = sum(1 for yr, pnl in yearly_pnl.items() if pnl < 0)

        return {
            'trades': tot_trades, 'wr': wr, 'pf': pf, 'avg_trade': avg_trade,
            'profit': profit, 'ret': ret, 'mdd': mdd, 'sharpe': sharpe,
            'losing_yrs': losing_yrs, 'yearly': yearly_pnl
        }

    # Test candidate universes:
    # 1. B_base: T10(22, sh<=10%) + T20(22, sh<=8%), body >= 5.5% (~622 trades)
    # 2. B_body50: T10(22, sh<=10%) + T20(22, sh<=8%), body >= 5.0% (~650~680 trades)
    # 3. B_body48: T10(22, sh<=10%) + T20(22, sh<=8.5%), body >= 4.8% (~700~730 trades)
    # 4. B_wide: T10(23, sh<=10.5%) + T20(22, sh<=8.5%), body >= 5.0% (~720~760 trades)
    # 5. B_ultra: T10(24, sh<=11%) + T20(22, sh<=9%), body >= 5.0% (~750~780 trades)
    universes = [
        ("Univ 1: Body5.5-Micro (622건)",
         ((pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10) & (pool['body_ret'] >= 5.5)) |
         ((pool['to_rank'] > 10) & (pool['to_rank'] <= 20) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.08) & (pool['body_ret'] >= 5.5))),
        
        ("Univ 2: Body5.0-Micro (660건)",
         ((pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10) & (pool['body_ret'] >= 5.0)) |
         ((pool['to_rank'] > 10) & (pool['to_rank'] <= 20) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.08) & (pool['body_ret'] >= 5.0))),

        ("Univ 3: Body4.8-Micro8.5 (705건)",
         ((pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.10) & (pool['body_ret'] >= 4.8)) |
         ((pool['to_rank'] > 10) & (pool['to_rank'] <= 20) & (pool['change_pct'] >= 10.0) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.085) & (pool['body_ret'] >= 4.8))),

        ("Univ 4: Body5.0-Balanced (740건)",
         ((pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 23.0) & (pool['upper_shadow'] <= 0.105) & (pool['body_ret'] >= 5.0)) |
         ((pool['to_rank'] > 10) & (pool['to_rank'] <= 20) & (pool['change_pct'] >= 9.5) & (pool['change_pct'] <= 22.0) & (pool['upper_shadow'] <= 0.085) & (pool['body_ret'] >= 5.0))),

        ("Univ 5: Body5.0-Wide770 (770건)",
         ((pool['to_rank'] <= 10) & (pool['change_pct'] >= 8.0) & (pool['change_pct'] <= 24.0) & (pool['upper_shadow'] <= 0.11) & (pool['body_ret'] >= 5.0)) |
         ((pool['to_rank'] > 10) & (pool['to_rank'] <= 20) & (pool['change_pct'] >= 9.0) & (pool['change_pct'] <= 22.5) & (pool['upper_shadow'] <= 0.09) & (pool['body_ret'] >= 5.0))),
    ]

    records = []
    for u_name, u_cond in universes:
        cands = pool[u_cond]
        for pnl_col in ['pnl_open', 'pnl_nxt_25', 'pnl_nxt_30', 'pnl_nxt_20']:
            # Test various cap schemes: Fixed vs Dynamic
            for b_cap, bear_cap, reg_key, cap_lbl in [
                (0.50, 0.50, 'is_bull_strong', '고정 50% Cap'),
                (0.55, 0.55, 'is_bull_strong', '고정 55% Cap'),
                (0.60, 0.60, 'is_bull_strong', '고정 60% Cap'),
                (0.65, 0.45, 'is_bull_strong', 'Bull 65% / Bear 45% (MA10>=20&C>=60)'),
                (0.70, 0.40, 'is_bull_strong', 'Bull 70% / Bear 40% (MA10>=20&C>=60)'),
                (0.70, 0.45, 'is_bull_strong', 'Bull 70% / Bear 45% (MA10>=20&C>=60)'),
                (0.72, 0.42, 'is_bull_strong', 'Bull 72% / Bear 42% (MA10>=20&C>=60)'),
                (0.65, 0.45, 'is_uptrend', 'Bull 65% / Bear 45% (MA10>=20)'),
                (0.70, 0.45, 'is_uptrend', 'Bull 70% / Bear 45% (MA10>=20)'),
            ]:
                res = sim_engine(cands, pnl_col, b_cap, bear_cap, reg_name=reg_key)
                records.append({
                    'universe': u_name,
                    'exit': pnl_col,
                    'cap_lbl': cap_lbl,
                    'bull_cap': b_cap,
                    'bear_cap': bear_cap,
                    **res
                })

    rdf = pd.DataFrame(records)
    rdf.to_csv('research/super_champion_b_c_synthesis.csv', index=False)

    print("=" * 135)
    print("🏆 [슈퍼 챔피언 B+C 합성 전수 결과: 체결 600~800건 / 순익 상위 15개]")
    print("=" * 135)
    top_p = rdf.sort_values('profit', ascending=False).head(15)
    for i, (_, r) in enumerate(top_p.iterrows(), 1):
        print(f"{i:<2} | {r['universe']:<30} | {r['exit']:<10} | {r['cap_lbl']:<36} | {r['trades']:>4}건 | WR:{r['wr']:>4.1f}% | PF:{r['pf']:>4.2f} | +{r['profit']:,.0f}원 (+{r['ret']:.1f}%) | MDD:{r['mdd']:>6.2f}% | Sharpe:{r['sharpe']:>4.2f}")
        print(f"    연도별: " + " | ".join([f"{yr}: {pnl/10000:+,.0f}만" for yr, pnl in sorted(r['yearly'].items())]))

    print("\n" + "=" * 135)
    print("🏆 [슈퍼 챔피언 B+C 합성 전수 결과: 체결 600~800건 / 샤프지수 상위 15개]")
    print("=" * 135)
    top_s = rdf.sort_values('sharpe', ascending=False).head(15)
    for i, (_, r) in enumerate(top_s.iterrows(), 1):
        print(f"{i:<2} | {r['universe']:<30} | {r['exit']:<10} | {r['cap_lbl']:<36} | {r['trades']:>4}건 | WR:{r['wr']:>4.1f}% | PF:{r['pf']:>4.2f} | +{r['profit']:,.0f}원 (+{r['ret']:.1f}%) | MDD:{r['mdd']:>6.2f}% | Sharpe:{r['sharpe']:>4.2f}")
        print(f"    연도별: " + " | ".join([f"{yr}: {pnl/10000:+,.0f}만" for yr, pnl in sorted(r['yearly'].items())]))

    print("\n" + "=" * 135)
    print("🏆 [슈퍼 챔피언 B+C 합성 전수 결과: 체결 600~800건 / MDD 최저(방어력) 상위 15개]")
    print("=" * 135)
    top_m = rdf.sort_values('mdd', ascending=False).head(15)
    for i, (_, r) in enumerate(top_m.iterrows(), 1):
        print(f"{i:<2} | {r['universe']:<30} | {r['exit']:<10} | {r['cap_lbl']:<36} | {r['trades']:>4}건 | WR:{r['wr']:>4.1f}% | PF:{r['pf']:>4.2f} | +{r['profit']:,.0f}원 (+{r['ret']:.1f}%) | MDD:{r['mdd']:>6.2f}% | Sharpe:{r['sharpe']:>4.2f}")
        print(f"    연도별: " + " | ".join([f"{yr}: {pnl/10000:+,.0f}만" for yr, pnl in sorted(r['yearly'].items())]))

if __name__ == '__main__':
    run()
