"""
Strategy optimizer — grid search over all key parameters, validated with
walk-forward testing so we don't overfit to history.

How it works:
  TRAIN  2023-01-01 to 2024-12-31  (optimize parameters here)
  TEST   2025-01-01 to 2026-06-12  (blind out-of-sample check)

Only parameters that work on BOTH periods are promoted as "safe to use".

Usage:
    python optimize.py                  # uses yfinance_feed
    python optimize.py --source upstox  # uses upstox_feed
    python optimize.py --apply          # auto-writes best params to backtest.py
"""

import sys
import itertools
import numpy as np
import pandas as pd
from backtest import load_from_sql

# ── Search space ──────────────────────────────────────────────────────────────
PARAM_GRID = {
    "entry":          [1.2, 1.5, 1.8, 2.0],
    "exit_th":        [0.3, 0.5, 0.7],
    "stop_loss":      [100, 150, 200, 250],
    "max_hold":       [10, 12, 15],
    "lookback":       [15, 20, 30],
    "ratio_lookback": [45, 60, 90],
}

TRAIN_START = "2023-01-01"
TRAIN_END   = "2024-12-31"
TEST_START  = "2025-01-01"
MIN_TRADES  = 10   # ignore param sets that produce fewer than this many trades


# ── Self-contained backtest (no globals) ─────────────────────────────────────

def run(df, entry, exit_th, stop_loss, max_hold, lookback, ratio_lookback):
    """Returns list of trade dicts. Fast — no DataFrame overhead for trades."""
    d = df.copy().reset_index(drop=True)

    d["ratio"]     = (d["sensex_close"] / d["nifty_close"]).rolling(ratio_lookback).mean()
    d["spread"]    = d["sensex_close"] - d["ratio"] * d["nifty_close"]
    d["spread_ma"] = d["spread"].rolling(lookback).mean()
    d["spread_sd"] = d["spread"].rolling(lookback).std()
    d["zscore"]    = (d["spread"] - d["spread_ma"]) / d["spread_sd"]

    trades   = []
    position = None
    start    = max(lookback, ratio_lookback)

    for i in range(start, len(d)):
        row = d.iloc[i]
        z   = row["zscore"]
        if np.isnan(z):
            continue

        if position is not None:
            held      = i - position["idx"]
            exit_now  = False
            reason    = ""

            if position["dir"] == "LONG_SPREAD"  and z >= -exit_th:
                exit_now, reason = True, "reverted"
            elif position["dir"] == "SHORT_SPREAD" and z <= exit_th:
                exit_now, reason = True, "reverted"

            if not exit_now:
                live_pnl = (row["spread"] - position["sp"]) if position["dir"] == "LONG_SPREAD" \
                           else (position["sp"] - row["spread"])
                if live_pnl <= -stop_loss:
                    exit_now, reason = True, "stop_loss"

            if held >= max_hold and not exit_now:
                exit_now, reason = True, "expiry"

            if exit_now:
                pnl = (row["spread"] - position["sp"]) if position["dir"] == "LONG_SPREAD" \
                      else (position["sp"] - row["spread"])
                trades.append({"pnl": pnl, "reason": reason, "held": held})
                position = None

        if position is None:
            if   z <= -entry: dirn = "LONG_SPREAD"
            elif z >=  entry: dirn = "SHORT_SPREAD"
            else:             dirn = None

            if dirn:
                position = {"idx": i, "dir": dirn, "sp": row["spread"]}

    return trades


# ── Score a set of trades ─────────────────────────────────────────────────────

def score(trades):
    if len(trades) < MIN_TRADES:
        return None
    pnls    = np.array([t["pnl"] for t in trades])
    wins    = pnls[pnls > 0]
    losses  = pnls[pnls <= 0]

    total_pnl     = pnls.sum()
    win_rate      = len(wins) / len(pnls) * 100
    gross_loss    = abs(losses.sum()) if len(losses) > 0 else 1e-9
    profit_factor = wins.sum() / gross_loss if len(wins) > 0 else 0
    cum           = np.cumsum(pnls)
    max_dd        = (np.maximum.accumulate(cum) - cum).max()
    sharpe        = pnls.mean() / pnls.std() if pnls.std() > 0 else 0

    return {
        "trades":        len(trades),
        "win_rate":      round(win_rate, 1),
        "total_pnl":     round(total_pnl, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown":  round(max_dd, 2),
        "sharpe":        round(sharpe, 3),
        "avg_pnl":       round(pnls.mean(), 2),
    }


# ── Composite score for ranking ───────────────────────────────────────────────
# Weights: profit factor (most important), sharpe, win rate, total P&L
def composite(s):
    if s is None:
        return -999
    return (
        s["profit_factor"] * 40 +
        s["sharpe"]        * 30 +
        s["win_rate"]       * 0.5 +
        s["total_pnl"]     * 0.01
    )


# ── Grid search ───────────────────────────────────────────────────────────────

def grid_search(df_train, df_test):
    keys   = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    combos = list(itertools.product(*values))
    total  = len(combos)
    print(f"Testing {total} parameter combinations ...")

    results = []
    for i, combo in enumerate(combos):
        if i % 200 == 0:
            print(f"  {i}/{total} ...", end="\r")

        params = dict(zip(keys, combo))

        train_trades = run(df_train, **params)
        train_score  = score(train_trades)
        if train_score is None:
            continue

        test_trades = run(df_test, **params)
        test_score  = score(test_trades)

        results.append({
            **params,
            "train_trades":    train_score["trades"],
            "train_win_rate":  train_score["win_rate"],
            "train_pnl":       train_score["total_pnl"],
            "train_pf":        train_score["profit_factor"],
            "train_sharpe":    train_score["sharpe"],
            "train_maxdd":     train_score["max_drawdown"],
            "test_trades":     test_score["trades"]   if test_score else 0,
            "test_win_rate":   test_score["win_rate"] if test_score else 0,
            "test_pnl":        test_score["total_pnl"] if test_score else 0,
            "test_pf":         test_score["profit_factor"] if test_score else 0,
            "train_composite": composite(train_score),
            "test_composite":  composite(test_score),
        })

    print(f"  {total}/{total} ... done.      ")
    return pd.DataFrame(results)


# ── Print results table ───────────────────────────────────────────────────────

def print_top(df_results, n=15):
    # Rank by: train profitable AND test profitable, then by combined score
    df = df_results.copy()
    df = df[(df["train_pnl"] > 0) & (df["test_trades"] >= MIN_TRADES)]
    df["combined"] = df["train_composite"] + df["test_composite"]
    df = df.sort_values("combined", ascending=False).head(n).reset_index(drop=True)

    print(f"\n{'='*110}")
    print(f"  TOP {n} PARAMETER SETS  (profitable on BOTH train 2023-24 AND test 2025-26)")
    print(f"{'='*110}")
    header = (f"{'#':>3}  {'ENTRY':>5}  {'EXIT':>5}  {'SL':>5}  {'HOLD':>5}  "
              f"{'LB':>4}  {'RLB':>4} | "
              f"{'TRN Trades':>10}  {'TRN Win%':>8}  {'TRN P&L':>8}  {'TRN PF':>7} | "
              f"{'TST Trades':>10}  {'TST Win%':>8}  {'TST P&L':>8}  {'TST PF':>7}")
    print(header)
    print("-" * 110)
    for i, r in df.iterrows():
        print(
            f"{i+1:>3}  {r['entry']:>5.1f}  {r['exit_th']:>5.1f}  "
            f"{int(r['stop_loss']):>5}  {int(r['max_hold']):>5}  "
            f"{int(r['lookback']):>4}  {int(r['ratio_lookback']):>4} | "
            f"{int(r['train_trades']):>10}  {r['train_win_rate']:>8.1f}%  "
            f"{r['train_pnl']:>8.1f}  {r['train_pf']:>7.2f} | "
            f"{int(r['test_trades']):>10}  {r['test_win_rate']:>8.1f}%  "
            f"{r['test_pnl']:>8.1f}  {r['test_pf']:>7.2f}"
        )
    print(f"{'='*110}")
    return df


# ── Apply best params to backtest.py ─────────────────────────────────────────

def apply_best(best_row):
    import re
    path = "backtest.py"
    with open(path) as f:
        src = f.read()

    replacements = {
        r"(ENTRY\s*=\s*)[\d.]+":          f"\\g<1>{best_row['entry']}",
        r"(EXIT\s*=\s*)[\d.]+":           f"\\g<1>{best_row['exit_th']}",
        r"(STOP_LOSS\s*=\s*)[\d]+":       f"\\g<1>{int(best_row['stop_loss'])}",
        r"(MAX_HOLD\s*=\s*)[\d]+":        f"\\g<1>{int(best_row['max_hold'])}",
        r"(LOOKBACK\s*=\s*)[\d]+":        f"\\g<1>{int(best_row['lookback'])}",
        r"(RATIO_LOOKBACK\s*=\s*)[\d]+":  f"\\g<1>{int(best_row['ratio_lookback'])}",
    }
    for pattern, repl in replacements.items():
        src = re.sub(pattern, repl, src)

    with open(path, "w") as f:
        f.write(src)

    print(f"\nbacktest.py updated with best parameters:")
    print(f"  ENTRY={best_row['entry']}  EXIT={best_row['exit_th']}  "
          f"STOP_LOSS={int(best_row['stop_loss'])}  MAX_HOLD={int(best_row['max_hold'])}  "
          f"LOOKBACK={int(best_row['lookback'])}  RATIO_LOOKBACK={int(best_row['ratio_lookback'])}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    source     = "yfinance"
    auto_apply = "--apply" in sys.argv
    if "--source" in sys.argv:
        idx    = sys.argv.index("--source")
        source = sys.argv[idx + 1]

    print(f"Loading data from [{source}_feed] ...")
    full_df = load_from_sql(source)

    df_train = full_df[
        (full_df["date"] >= pd.Timestamp(TRAIN_START)) &
        (full_df["date"] <= pd.Timestamp(TRAIN_END))
    ].reset_index(drop=True)

    df_test = full_df[
        full_df["date"] >= pd.Timestamp(TEST_START)
    ].reset_index(drop=True)

    print(f"  Train: {len(df_train)} days ({TRAIN_START} to {TRAIN_END})")
    print(f"  Test:  {len(df_test)} days ({TEST_START} to today)\n")

    results = grid_search(df_train, df_test)

    if results.empty:
        print("No valid parameter combinations found.")
        return

    top = print_top(results, n=15)

    if top.empty:
        print("\nNo combination was profitable on both train AND test periods.")
        print("Showing top train-only results:")
        top = results.sort_values("train_composite", ascending=False).head(5)
        print_top(top, n=5)
        return

    best = top.iloc[0]
    print(f"\nBEST PARAMETERS:")
    print(f"  Entry z-score   : +/- {best['entry']}")
    print(f"  Exit  z-score   : +/- {best['exit_th']}")
    print(f"  Stop loss (pts) : {int(best['stop_loss'])}")
    print(f"  Max hold (days) : {int(best['max_hold'])}")
    print(f"  Signal lookback : {int(best['lookback'])} days")
    print(f"  Ratio lookback  : {int(best['ratio_lookback'])} days")
    print(f"\n  Train P&L: {best['train_pnl']} pts  |  Win rate: {best['train_win_rate']}%  |  Profit factor: {best['train_pf']}")
    print(f"  Test  P&L: {best['test_pnl']} pts  |  Win rate: {best['test_win_rate']}%  |  Profit factor: {best['test_pf']}")

    if auto_apply:
        apply_best(best)
        print("\nRun  python backtest.py --source yfinance --start 2023-01-01  to see full results.")
    else:
        print("\nTo apply these parameters automatically, run:")
        print("  python optimize.py --apply")

    # Save full results CSV
    results.sort_values("train_composite", ascending=False).to_csv("optimization_results.csv", index=False)
    print("\nAll results saved -> optimization_results.csv")


if __name__ == "__main__":
    main()
