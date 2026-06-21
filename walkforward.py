"""
walkforward.py — honest out-of-sample testing of the spread strategy.

It splits the data into sequential time folds, OPTIMISES the strategy on each
"train" fold, then measures that same config on the NEXT "test" fold it has
never seen. A strategy with a real edge profits out-of-sample; an over-fit one
profits in training and loses in testing.

With one day of data and --folds 2 this is the classic "train on the morning,
test on the afternoon" demonstration. With multiple days it becomes a real
walk-forward (just collect more data and raise --folds).

Usage:
    python walkforward.py                      # 1-min bars, 2 folds (AM/PM)
    python walkforward.py --folds 4 --cost-pts 35
"""
import sys
import itertools
from datetime import date
import pandas as pd

import backtest as bt
from backtest_intraday import load_series, resample
from optimize_intraday import WINDOWS, ENTRIES, EXITS, TARGETS, STOPS, HOLDS, run_combo


def arg(flag, default):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def optimize(df, cost, min_trades):
    """Sweep the grid on one fold; return the best config by net P&L that takes
    at least `min_trades` trades (so a single lucky fill can't win)."""
    best = None
    for (rw, sw), entry, exit_, tp, sl, mh in itertools.product(
            WINDOWS, ENTRIES, EXITS, TARGETS, STOPS, HOLDS):
        if len(df) <= rw + 2:
            continue
        r = run_combo(df, rw, sw, entry, exit_, tp, sl, mh, cost)
        if r is None or r["trades"] < min_trades:
            continue
        if best is None or r["net_pts"] > best["net_pts"]:
            best = {"rw": rw, "sw": sw, "entry": entry, "exit": exit_,
                    "tp": tp, "sl": sl, "mh": mh, **r}
    return best


def evaluate(df, cfg, cost):
    """Apply a FIXED config to an unseen fold."""
    if cfg is None:
        return None
    return run_combo(df, cfg["rw"], cfg["sw"], cfg["entry"], cfg["exit"],
                     cfg["tp"], cfg["sl"], cfg["mh"], cost)


def main():
    folds    = int(arg("--folds", "2"))
    cost     = float(arg("--cost-pts", "35"))
    mint     = int(arg("--min-trades", "3"))
    rule     = arg("--resample", "1min")
    tag      = arg("--tag", date.today().strftime("%Y_%m_%d"))

    df = resample(load_series("sensex_data.csv", "nifty_data.csv"), rule)
    n = len(df)
    edges = [int(round(i * n / folds)) for i in range(folds + 1)]
    chunks = [df.iloc[edges[i]:edges[i + 1]].reset_index(drop=True) for i in range(folds)]
    print(f"{n} {rule} bars split into {folds} folds | cost {cost:.0f} pts | "
          f"require >= {mint} trades when picking a config")
    for i, c in enumerate(chunks):
        print(f"  fold {i}: {c['date'].iloc[0]} -> {c['date'].iloc[-1]}  ({len(c)} bars)")

    rows = []
    for i in range(folds - 1):
        train, test = chunks[i], chunks[i + 1]
        cfg = optimize(train, cost, mint)
        if cfg is None:
            print(f"\nfold {i}->{i+1}: no config with >= {mint} trades in training.")
            continue
        test_r = evaluate(test, cfg, cost)
        rows.append({
            "train_fold": i, "test_fold": i + 1,
            "config": f"e{cfg['entry']}/x{cfg['exit']}/tp{cfg['tp']}/sl{cfg['sl']}/"
                      f"w({cfg['rw']},{cfg['sw']})/h{cfg['mh']}",
            "train_trades": cfg["trades"], "train_net_pts": cfg["net_pts"],
            "test_trades": (test_r or {}).get("trades", 0),
            "test_net_pts": (test_r or {}).get("net_pts", None),
        })

    if not rows:
        print("\nNot enough data/trades for a train->test pass. Collect more days.")
        return

    res = pd.DataFrame(rows)
    res.to_csv(f"walkforward_{tag}.csv", index=False)

    print("\n" + "=" * 78)
    print("  WALK-FORWARD: best-on-TRAIN config measured on UNSEEN TEST data")
    print("=" * 78)
    for _, r in res.iterrows():
        tn = r["test_net_pts"]
        print(f"  train fold {r['train_fold']} -> test fold {r['test_fold']}")
        print(f"    best train config : {r['config']}")
        print(f"    TRAIN (in-sample) : {r['train_trades']:>3} trades   net {r['train_net_pts']:+8.2f} pts")
        print(f"    TEST  (out-sample): {r['test_trades']:>3} trades   net "
              f"{tn:+8.2f} pts" if tn is not None else "    TEST: no trades")
        if tn is not None:
            verdict = "HELD UP (real edge?)" if tn > 0 else "FELL APART (over-fit)"
            print(f"    -> {verdict}")
    oos = res["test_net_pts"].dropna().sum()
    print("-" * 78)
    print(f"  Total OUT-OF-SAMPLE net P&L: {oos:+.2f} pts")
    print(f"  (in-sample total was {res['train_net_pts'].sum():+.2f} pts)")
    print("=" * 78)
    print("  Lesson: only the OUT-OF-SAMPLE column matters. In-sample profit is easy")
    print("  to manufacture; it does not mean the strategy will make money live.")
    print(f"  Report -> walkforward_{tag}.csv")


if __name__ == "__main__":
    main()
