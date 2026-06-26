"""
optimize_intraday.py — net-of-cost parameter sweep of the SENSEX/NIFTY spread
strategy on one day's tick data. Finds settings that are PROFITABLE after
transaction costs, and writes human-readable CSV reports.

Outputs:
  backtest_2026_06_18_configs.csv  -> every net-profitable setting, ranked best first
  backtest_2026_06_18_best.csv     -> the trade-by-trade log of the single best setting

WARNING: this optimises on a SINGLE day = in-sample curve-fitting. The "best"
settings describe today only and are NOT predictive. Validate on many days
before trusting anything here.

Usage:
    python optimize_intraday.py --cost-pts 35
"""
import sys
import itertools
from datetime import date
import pandas as pd

import backtest as bt
from backtest_intraday import load_series, resample


def arg(flag, default):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


# Parameter grid
BARS         = ["1min", "3min", "5min"]
WINDOWS      = [(60, 15), (30, 10), (20, 5)]   # (ratio_win, spread_win) in bars
ENTRIES      = [2.0, 2.5, 3.0]
EXITS        = [0.5, 0.7, 1.0]
TARGETS      = [30, 50, 80, 120]
STOPS        = [100, 150]
HOLDS        = [15, 30]


def run_combo(df, rw, sw, entry, exit_, tp, sl, mh, cost):
    bt.RATIO_LOOKBACK, bt.LOOKBACK = rw, sw
    bt.ENTRY, bt.EXIT = entry, exit_
    bt.PROFIT_TARGET, bt.STOP_LOSS, bt.MAX_HOLD = tp, sl, mh
    trades = bt.backtest(df)
    if not len(trades):
        return None
    gross = trades["pnl_points"]
    net   = gross - cost
    return {
        "trades": len(trades),
        "gross_pts": round(gross.sum(), 2),
        "net_pts": round(net.sum(), 2),
        "net_win_pct": round((net > 0).mean() * 100, 1),
        "avg_net_pts": round(net.mean(), 2),
        "best_pts": round(gross.max(), 2),
        "worst_pts": round(gross.min(), 2),
    }


def main():
    cost = float(arg("--cost-pts", "4"))
    tag  = arg("--tag", date.today().strftime("%Y_%m_%d"))
    df_tick = load_series(arg("--sensex", "sensex_data.csv"), arg("--nifty", "nifty_data.csv"))
    bars = {b: resample(df_tick, b) for b in BARS}
    for b in BARS:
        print(f"  {b}: {len(bars[b])} bars")

    rows = []
    combos = list(itertools.product(BARS, WINDOWS, ENTRIES, EXITS, TARGETS, STOPS, HOLDS))
    print(f"Sweeping {len(combos)} combinations at cost {cost:.0f} pts/round-trip ...")
    for b, (rw, sw), entry, exit_, tp, sl, mh in combos:
        df = bars[b]
        if len(df) <= rw + 2:           # not enough bars for the window
            continue
        r = run_combo(df, rw, sw, entry, exit_, tp, sl, mh, cost)
        if r is None:
            continue
        rows.append({"bar": b, "ratio_win": rw, "spread_win": sw,
                     "entry_z": entry, "exit_z": exit_, "profit_target": tp,
                     "stop_loss": sl, "max_hold": mh, **r})

    res = pd.DataFrame(rows).sort_values("net_pts", ascending=False).reset_index(drop=True)
    res.to_csv(f"backtest_{tag}_all_configs.csv", index=False)
    profitable = res[res.net_pts > 0].copy()

    print(f"\nTested {len(res)} valid configs | net-profitable: {len(profitable)} "
          f"({len(profitable)/max(len(res),1)*100:.0f}%)")

    profitable.to_csv(f"backtest_{tag}_configs.csv", index=False)
    print(f"Profitable configs -> backtest_{tag}_configs.csv ({len(profitable)} rows)")

    if not len(profitable):
        print("\nNo configuration is net-profitable after costs on this day.")
        return

    # A 1-2 trade "profit" is just a lucky fill. Require a meaningful count.
    MIN_TRADES = 5
    robust = profitable[profitable.trades >= MIN_TRADES]
    print(f"  of which with >= {MIN_TRADES} trades (not flukes): {len(robust)}")

    print("\nTop 10 net-profitable settings (any trade count):")
    print(profitable.head(10).to_string(index=False))
    if len(robust):
        print(f"\nTop 10 net-profitable settings with >= {MIN_TRADES} trades:")
        print(robust.head(10).to_string(index=False))

    # ── trade-by-trade log for the best NON-FLUKE setting ────────────────────
    pool = robust if len(robust) else profitable
    best = pool.iloc[0]
    fluke = not len(robust)
    df = bars[best["bar"]]
    t = run_and_detail(df, best, cost)
    t.to_csv(f"backtest_{tag}_best.csv", index=False)
    print(f"\nBest{' (FLUKE - only ' + str(int(best['trades'])) + ' trade)' if fluke else ''} setting: "
          f"bar={best['bar']} entry+/-{best['entry_z']} exit+/-{best['exit_z']} "
          f"target+{best['profit_target']} SL-{best['stop_loss']} "
          f"win({best['ratio_win']},{best['spread_win']}) hold{best['max_hold']}")
    print(f"  -> {int(best['trades'])} trades | net {best['net_pts']:+.2f} pts | "
          f"win {best['net_win_pct']:.0f}%")
    print(f"Detailed trade log -> backtest_{tag}_best.csv")


def run_and_detail(df, best, cost):
    bt.RATIO_LOOKBACK, bt.LOOKBACK = int(best["ratio_win"]), int(best["spread_win"])
    bt.ENTRY, bt.EXIT = float(best["entry_z"]), float(best["exit_z"])
    bt.PROFIT_TARGET, bt.STOP_LOSS, bt.MAX_HOLD = (
        float(best["profit_target"]), float(best["stop_loss"]), int(best["max_hold"]))
    tr = bt.backtest(df).rename(columns={
        "entry_date": "entry_time", "exit_date": "exit_time",
        "pnl_points": "gross_pnl_points"})
    tr.insert(0, "trade_no", range(1, len(tr) + 1))
    tr["action"] = tr["direction"].map({
        "LONG_SPREAD":  "BUY SENSEX + SELL NIFTY",
        "SHORT_SPREAD": "SELL SENSEX + BUY NIFTY"})
    tr["held_minutes"] = (
        (pd.to_datetime(tr["exit_time"]) - pd.to_datetime(tr["entry_time"]))
        .dt.total_seconds() / 60).round(1)
    tr["cost_points"]      = cost
    tr["net_pnl_points"]   = (tr["gross_pnl_points"] - cost).round(2)
    tr["running_net_pts"]  = tr["net_pnl_points"].cumsum().round(2)
    tr["result"]           = tr["net_pnl_points"].apply(lambda p: "WIN" if p > 0 else "LOSS")
    cols = ["trade_no", "action", "entry_time", "exit_time", "held_minutes",
            "zscore_entry", "zscore_exit", "spread_entry", "spread_exit",
            "sensex_entry", "nifty_entry", "sensex_exit", "nifty_exit", "exit_reason",
            "gross_pnl_points", "cost_points", "net_pnl_points",
            "running_net_pts", "result"]
    return tr[cols]


if __name__ == "__main__":
    main()
