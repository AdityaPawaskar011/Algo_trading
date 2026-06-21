"""
make_summary.py — build backtest_<tag>_SUMMARY.csv (plain-language verdict)
from the backtest CSVs already produced for that day.

Usage:
    python make_summary.py --tag 2026_06_19
"""
import sys
from datetime import date
import pandas as pd


def arg(flag, default):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def main():
    tag = arg("--tag", date.today().strftime("%Y_%m_%d"))
    datestr = tag.replace("_", "-")

    sx = pd.read_csv("sensex_data.csv", usecols=["tick_time"])
    n_ticks, span = len(sx), f"{sx.tick_time.min()} to {sx.tick_time.max()}"

    one = pd.read_csv(f"backtest_{tag}_1min.csv")
    trades = len(one)
    gross  = one.gross_pnl_points.sum() if trades else 0.0
    net    = one.net_pnl_points.sum()   if trades else 0.0
    cost   = one.cost_points.iloc[0]    if trades else 35.0
    be     = gross / trades             if trades else 0.0

    allc = pd.read_csv(f"backtest_{tag}_all_configs.csv")
    n_cfg    = len(allc)
    n_prof   = int((allc.net_pts > 0).sum())
    n_robust = int(((allc.net_pts > 0) & (allc.trades >= 5)).sum())

    try:
        wf = pd.read_csv(f"walkforward_{tag}.csv").iloc[0]
        wf_val = f"train {wf.train_net_pts:+.0f} / test {wf.test_net_pts:+.0f} pts"
        wf_mean = "Best morning config measured on the unseen afternoon"
    except Exception:
        wf_val, wf_mean = "n/a", "not enough data for a train/test split"

    profitable = net > 0 and n_robust > 0
    verdict = "PROFITABLE after costs (this day)" if profitable else "NOT profitable after costs"
    verdict_why = (f"Edge ~{be:.0f} pts/trade vs cost ~{cost:.0f} pts/trade"
                   if not profitable else "Net positive even after realistic costs - verify on more days")

    rows = [
        ("Date", datestr, "One trading day of per-second SENSEX + NIFTY data"),
        ("Data source", "Live Upstox feed", f"{n_ticks} ticks, {span}"),
        ("Strategy", "Spread mean-reversion", "Trade SENSEX vs NIFTY when spread z-score hits +/-2, exit on reversion"),
        ("Realistic test", "1-minute bars", "Resampled to 1-min so it is not just trading 1-second noise"),
        ("Trades taken", trades, "Round-trip spread trades on the day"),
        ("Gross P&L", f"{gross:+.2f} points", "Profit BEFORE costs"),
        ("Transaction cost", f"~{cost:.0f} points per round-trip", "Mostly STT (0.02% on two ~Rs1.5M futures sell-legs) + fees + slippage"),
        ("Net P&L", f"{net:+.2f} points", "Profit AFTER realistic costs - the real result"),
        ("Break-even cost", f"~{be:.0f} points per trade", "Costs must be below this to profit"),
        ("Configs swept", f"{n_cfg} valid of 1296", "Tried many entry/exit/target/window/bar combinations"),
        ("Profitable configs", n_prof, "Net-positive settings (most are single-trade flukes)"),
        ("Robust profitable configs (>=5 trades)", n_robust, "Reliable multi-trade settings that profit after costs"),
        ("Walk-forward (AM train / PM test)", wf_val, wf_mean),
        ("VERDICT", verdict, verdict_why),
        ("Caveat", "Single day = curve-fitting", "Profit found by tuning on one day will not repeat; validate across many days"),
    ]
    out = f"backtest_{tag}_SUMMARY.csv"
    pd.DataFrame(rows, columns=["metric", "value", "what_it_means"]).to_csv(out, index=False)
    print(f"Wrote {out}  | {trades} trades, gross {gross:+.1f}, net {net:+.1f}, "
          f"robust-profitable configs {n_robust}")


if __name__ == "__main__":
    main()
