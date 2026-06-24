"""
backtest_intraday.py — apply the SENSEX/NIFTY spread strategy to a day's
per-second tick data (sensex_data.csv + nifty_data.csv), with optional
bar resampling and a transaction-cost model, and write a detailed trade report.

It reuses the EXACT strategy from backtest.py (compute_signals + backtest):
  ratio  = rolling mean of Sensex/Nifty over RATIO_LOOKBACK
  spread = Sensex - ratio*Nifty ; z = (spread - mean)/std over LOOKBACK
  entry  |z| >= ENTRY ; exit on reversion past EXIT, stop-loss, profit-target,
  or after MAX_HOLD bars.

Timescale note: backtest.py's windows (RATIO_LOOKBACK, LOOKBACK, MAX_HOLD) count
BARS. On tick data a bar is ~1 second; with --resample 1min a bar is 1 minute.

Cost model (--cost-pts): spread P&L is in Sensex index points. A 1-lot
SENSEX-fut + 1-lot NIFTY-fut spread is ~Rs1.5M/leg, so STT on the two sell
legs (~0.02% of notional) alone is ~31 points/round-trip; with brokerage,
exchange fees, GST and slippage a realistic figure is ~35 points. It is
roughly scale-invariant (STT is a % of notional).

Usage:
    python backtest_intraday.py --resample 1min --cost-pts 35 --out backtest_2026_06_18_1min.csv
    python backtest_intraday.py                      # per-second, zero cost (raw)
"""
import sys
import pandas as pd

from backtest import (
    backtest, RATIO_LOOKBACK, LOOKBACK, ENTRY, EXIT,
    STOP_LOSS, PROFIT_TARGET, MAX_HOLD, MODE,
)


def arg(flag, default):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def load_series(sensex_csv, nifty_csv):
    """Merge the two per-symbol feeds into one date/sensex_close/nifty_close
    frame, de-duplicating timestamps first to avoid a cartesian join."""
    sx = (pd.read_csv(sensex_csv, usecols=["tick_time", "last_price"])
            .dropna().drop_duplicates(subset="tick_time", keep="first")
            .rename(columns={"last_price": "sensex_close"}))
    nf = (pd.read_csv(nifty_csv, usecols=["tick_time", "last_price"])
            .dropna().drop_duplicates(subset="tick_time", keep="first")
            .rename(columns={"last_price": "nifty_close"}))
    df = pd.merge(sx, nf, on="tick_time", how="inner")
    df["date"] = pd.to_datetime(df["tick_time"])
    df = df.sort_values("date").reset_index(drop=True)
    return df[["date", "sensex_close", "nifty_close"]]


def resample(df, rule):
    """Resample to `rule` bars, taking the last price in each bar (= close)."""
    out = (df.set_index("date").resample(rule).last().dropna().reset_index())
    return out


def main():
    sensex_csv = arg("--sensex", "sensex_data.csv")
    nifty_csv  = arg("--nifty",  "nifty_data.csv")
    rule       = arg("--resample", None)
    cost_pts   = float(arg("--cost-pts", "4"))
    out        = arg("--out", "backtest_2026_06_18.csv")

    df = load_series(sensex_csv, nifty_csv)
    bar = "1 tick (~1s)"
    if rule:
        n0 = len(df)
        df = resample(df, rule)
        bar = rule
        print(f"Resampled {n0} ticks -> {len(df)} {rule} bars")
    print(f"Data: {len(df)} bars ({df['date'].min()} -> {df['date'].max()})  bar={bar}")
    print(f"Strategy: mode={MODE} | entry +/-{ENTRY} exit +/-{EXIT} | "
          f"SL -{STOP_LOSS} TP +{PROFIT_TARGET} pts | "
          f"ratio_win {RATIO_LOOKBACK} spread_win {LOOKBACK} max_hold {MAX_HOLD} bars")
    print(f"Transaction cost: {cost_pts:.1f} spread points / round-trip")

    trades = backtest(df)
    if not len(trades):
        trades.to_csv(out, index=False)
        print(f"\nNo trades generated. Wrote empty report -> {out}")
        return

    # ── enrich + apply costs ─────────────────────────────────────────────────
    trades = trades.rename(columns={
        "entry_date": "entry_time", "exit_date": "exit_time",
        "holding_days": "holding_bars", "pnl_points": "gross_pnl_points",
    })
    trades.insert(0, "trade_no", range(1, len(trades) + 1))
    trades["duration_secs"] = (
        pd.to_datetime(trades["exit_time"]) - pd.to_datetime(trades["entry_time"])
    ).dt.total_seconds().round().astype(int)
    trades["cost_points"]    = cost_pts
    trades["net_pnl_points"] = (trades["gross_pnl_points"] - cost_pts).round(2)
    trades["cum_net_points"] = trades["net_pnl_points"].cumsum().round(2)
    trades["result"] = trades["net_pnl_points"].apply(
        lambda p: "WIN" if p > 0 else ("LOSS" if p < 0 else "FLAT"))

    cols = ["trade_no", "direction", "spread_type", "entry_time",
            "sensex_entry", "nifty_entry", "ratio", "spread_entry", "zscore_entry",
            "exit_time", "sensex_exit", "nifty_exit", "spread_exit", "zscore_exit",
            "holding_bars", "duration_secs", "exit_reason",
            "gross_pnl_points", "cost_points", "net_pnl_points", "cum_net_points",
            "result"]
    trades[cols].to_csv(out, index=False)

    # ── summary (gross vs net) ───────────────────────────────────────────────
    g = trades["gross_pnl_points"]; n = trades["net_pnl_points"]
    print("\n" + "=" * 66)
    print(f"  TRADE BACKTEST  ({df['date'].min().date()})  bar={bar}  cost={cost_pts:.0f}pts")
    print("=" * 66)
    print(f"  Total trades       : {len(trades)}")
    print(f"  Avg hold           : {trades.duration_secs.mean():.0f}s | Long/Short {(trades.direction=='LONG_SPREAD').sum()}/{(trades.direction=='SHORT_SPREAD').sum()}")
    print(f"  GROSS  win {len(g[g>0])/len(g)*100:5.1f}%  total {g.sum():+9.2f} pts  avg {g.mean():+.2f}")
    print(f"  NET    win {len(n[n>0])/len(n)*100:5.1f}%  total {n.sum():+9.2f} pts  avg {n.mean():+.2f}")
    print(f"  Exit reasons       : {trades.exit_reason.value_counts().to_dict()}")
    print("-" * 66)
    print("  Cost sensitivity (NET total pts / win% by cost per round-trip):")
    for c in [0, 5, 10, 20, 35, 50]:
        net = g - c
        print(f"     {c:3d} pts -> {net.sum():+10.2f} pts   win {len(net[net>0])/len(net)*100:5.1f}%")
    be = g.sum() / len(trades)
    print(f"  Break-even cost    : {be:.2f} pts/round-trip "
          f"(above this the day is net-negative)")
    print("=" * 66)
    print(f"  Detailed report -> {out}  ({len(trades)} trades)")
    print("=" * 66)


if __name__ == "__main__":
    main()
