"""
================================================================================
 SENSEX-NIFTY RATIO SPREAD BACKTEST
================================================================================
Strategy (from your notes):
  - Treat Sensex and Nifty as a pair.  Sensex ~= RATIO * Nifty  (RATIO ~ 3.2)
  - SPREAD = Sensex - RATIO * Nifty
        spread < 0  -> "credit spread"  (Sensex cheap vs Nifty)
        spread > 0  -> "debit spread"   (Sensex rich  vs Nifty)
  - We standardise the spread into a z-score over a rolling window.
  - MEAN-REVERSION (default):
        z <= -ENTRY  -> spread too cheap -> LONG spread  (BUY Sensex, SELL Nifty)
        z >= +ENTRY  -> spread too rich  -> SHORT spread (SELL Sensex, BUY Nifty)
        exit when |z| <= EXIT, or after MAX_HOLD days ("works on expiry").
  - Set MODE = "trend" to flip the logic to momentum instead of reversion.

P&L is computed in SPREAD POINTS:
        long-spread pnl  = (spread_exit - spread_entry)
        short-spread pnl = (spread_entry - spread_exit)
  Multiply by RUPEE_PER_POINT to get an approximate INR figure. Set this to
  your real contract point value (and adjust leg lot sizes) for live accuracy.
================================================================================
"""
import numpy as np
import pandas as pd

# ----------------------------- PARAMETERS ----------------------------------
RATIO_LOOKBACK  = 60     # rolling window to compute dynamic Sensex/Nifty ratio
LOOKBACK        = 15     # rolling window for spread mean/std (trading days)
ENTRY           = 2.0    # enter when |z-score| >= this
EXIT            = 0.7    # exit when |z-score| <= this
STOP_LOSS       = 100    # exit immediately if loss exceeds this many spread points
MAX_HOLD        = 15     # max trading days to hold (proxy for "expiry")
MODE            = "reversion"   # "reversion" or "trend"
RUPEE_PER_POINT = 1.0    # set to your contract value for INR P&L
# ---------------------------------------------------------------------------


def load_from_sql(source: str = "yfinance"):
    """Load price data from SQL Server.
    source='yfinance' -> yfinance_feed table
    source='upstox'   -> upstox_feed table
    """
    import pyodbc
    from config import DB_SERVER, DB_NAME, DB_DRIVER

    table_map = {"yfinance": "yfinance_feed", "upstox": "upstox_feed"}
    if source not in table_map:
        raise ValueError(f"Unknown source '{source}'. Choose 'yfinance' or 'upstox'.")
    table = table_map[source]

    conn_str = (
        f"DRIVER={{{DB_DRIVER}}};"
        f"SERVER={DB_SERVER};"
        f"DATABASE={DB_NAME};"
        "Trusted_Connection=yes;"
    )
    conn   = pyodbc.connect(conn_str)
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT trade_date, sensex_close, nifty_close FROM {table} ORDER BY trade_date"
    )
    rows = cursor.fetchall()
    conn.close()
    df = pd.DataFrame(
        [(r[0], float(r[1]), float(r[2])) for r in rows],
        columns=["date", "sensex_close", "nifty_close"],
    )
    df["date"] = pd.to_datetime(df["date"])
    print(f"Loaded {len(df)} rows from [{table}] ({df['date'].min().date()} to {df['date'].max().date()})")
    return df


def load_real_data(start="2023-01-01", end=None):
    """Pull real Sensex (^BSESN) & Nifty (^NSEI) daily closes via yfinance.
    Run locally after: pip install yfinance"""
    import yfinance as yf
    sx = yf.download("^BSESN", start=start, end=end)["Close"].rename("sensex_close")
    nf = yf.download("^NSEI",  start=start, end=end)["Close"].rename("nifty_close")
    df = pd.concat([sx, nf], axis=1).dropna().reset_index()
    df.columns = ["date", "sensex_close", "nifty_close"]
    return df


def compute_signals(df):
    df = df.copy()
    # Dynamic ratio: rolling mean of actual Sensex/Nifty — keeps spread centered near 0
    df["ratio"]     = (df["sensex_close"] / df["nifty_close"]).rolling(RATIO_LOOKBACK).mean()
    df["spread"]    = df["sensex_close"] - df["ratio"] * df["nifty_close"]
    df["spread_ma"] = df["spread"].rolling(LOOKBACK).mean()
    df["spread_sd"] = df["spread"].rolling(LOOKBACK).std()
    df["zscore"]    = (df["spread"] - df["spread_ma"]) / df["spread_sd"]
    return df


def backtest(df):
    df = compute_signals(df).reset_index(drop=True)
    trades = []
    position = None  # None or dict describing the open trade

    for i in range(max(LOOKBACK, RATIO_LOOKBACK), len(df)):
        row = df.iloc[i]
        z = row["zscore"]
        if np.isnan(z):
            continue

        # -------- manage an open position --------
        if position is not None:
            held = i - position["entry_idx"]
            exit_now, reason = False, ""

            if MODE == "reversion":
                # exit when spread has reverted toward the mean
                if position["dir"] == "LONG_SPREAD" and z >= -EXIT:
                    exit_now, reason = True, "reverted"
                elif position["dir"] == "SHORT_SPREAD" and z <= EXIT:
                    exit_now, reason = True, "reverted"
            else:  # trend: exit when momentum fades back through zero
                if position["dir"] == "LONG_SPREAD" and z <= 0:
                    exit_now, reason = True, "trend_faded"
                elif position["dir"] == "SHORT_SPREAD" and z >= 0:
                    exit_now, reason = True, "trend_faded"

            # stop loss: exit if current loss exceeds STOP_LOSS points
            if not exit_now:
                s_now = row["spread"]
                live_pnl = (s_now - position["spread_entry"]) if position["dir"] == "LONG_SPREAD" \
                           else (position["spread_entry"] - s_now)
                if live_pnl <= -STOP_LOSS:
                    exit_now, reason = True, "stop_loss"

            if held >= MAX_HOLD and not exit_now:
                exit_now, reason = True, "expiry"

            if exit_now:
                s_exit = row["spread"]
                if position["dir"] == "LONG_SPREAD":
                    pnl = s_exit - position["spread_entry"]
                else:
                    pnl = position["spread_entry"] - s_exit
                trades.append({
                    **position,
                    "exit_date":   row["date"],
                    "sensex_exit": row["sensex_close"],
                    "nifty_exit":  row["nifty_close"],
                    "spread_exit": round(s_exit, 2),
                    "zscore_exit": round(z, 3),
                    "holding_days": held,
                    "pnl_points":  round(pnl, 2),
                    "pnl_inr":     round(pnl * RUPEE_PER_POINT, 2),
                    "exit_reason": reason,
                })
                position = None

        # -------- look for a new entry --------
        if position is None:
            direction = None
            if MODE == "reversion":
                if z <= -ENTRY:
                    direction = "LONG_SPREAD"    # buy Sensex, sell Nifty
                elif z >= ENTRY:
                    direction = "SHORT_SPREAD"   # sell Sensex, buy Nifty
            else:  # trend
                if z >= ENTRY:
                    direction = "LONG_SPREAD"
                elif z <= -ENTRY:
                    direction = "SHORT_SPREAD"

            if direction:
                position = {
                    "entry_idx":    i,
                    "entry_date":   row["date"],
                    "direction":    direction,
                    "dir":          direction,
                    "sensex_entry": row["sensex_close"],
                    "nifty_entry":  row["nifty_close"],
                    "ratio":        round(row["ratio"], 4),
                    "spread_entry": round(row["spread"], 2),
                    "zscore_entry": round(z, 3),
                    "spread_type":  "credit" if row["spread"] < 0 else "debit",
                }

    cols = ["entry_date", "exit_date", "direction", "spread_type",
            "sensex_entry", "nifty_entry", "ratio", "spread_entry", "zscore_entry",
            "sensex_exit", "nifty_exit", "spread_exit", "zscore_exit",
            "holding_days", "pnl_points", "pnl_inr", "exit_reason"]
    tdf = pd.DataFrame(trades)
    return tdf[cols] if len(tdf) else tdf


def summary(tdf):
    if len(tdf) == 0:
        print("No trades generated. Loosen ENTRY or lengthen the data.")
        return
    wins      = tdf[tdf.pnl_points > 0]
    stopped   = tdf[tdf.exit_reason == "stop_loss"]
    expired   = tdf[tdf.exit_reason == "expiry"]
    reverted  = tdf[tdf.exit_reason == "reverted"]
    print("=" * 60)
    print(f"Mode:            {MODE}")
    print(f"Entry threshold: {ENTRY}  |  Stop loss: {STOP_LOSS} pts  |  Max hold: {MAX_HOLD} days")
    print(f"Total trades:    {len(tdf)}")
    print(f"Win rate:        {len(wins)/len(tdf)*100:.1f}%")
    print(f"Total P&L (pts): {tdf.pnl_points.sum():.2f}")
    print(f"Avg P&L/trade:   {tdf.pnl_points.mean():.2f} pts")
    print(f"Best / Worst:    {tdf.pnl_points.max():.2f} / {tdf.pnl_points.min():.2f}")
    print(f"Avg hold:        {tdf.holding_days.mean():.1f} days")
    print(f"Exit reasons:    reverted={len(reverted)}  stop_loss={len(stopped)}  expiry={len(expired)}")
    print("=" * 60)


if __name__ == "__main__":
    import sys
    from datetime import datetime

    # Parse optional date filters
    start_date = None
    end_date   = None
    if "--start" in sys.argv:
        idx        = sys.argv.index("--start")
        start_date = sys.argv[idx + 1]
    if "--end" in sys.argv:
        idx      = sys.argv.index("--end")
        end_date = sys.argv[idx + 1]

    if "--source" in sys.argv:
        idx = sys.argv.index("--source")
        src = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        if src not in ("yfinance", "upstox"):
            print("Usage: python backtest.py --source yfinance|upstox [--start YYYY-MM-DD] [--end YYYY-MM-DD]")
            sys.exit(1)
        data = load_from_sql(src)
    elif "--real" in sys.argv:
        data = load_real_data()
    else:
        from generate_data import generate
        data = generate()

    # Apply date filters if given
    if start_date:
        data = data[data["date"] >= pd.Timestamp(start_date)]
    if end_date:
        data = data[data["date"] <= pd.Timestamp(end_date)]
    if start_date or end_date:
        data = data.reset_index(drop=True)
        print(f"Filtered to {len(data)} rows  ({data['date'].min().date()}  to  {data['date'].max().date()})")

    trades = backtest(data)
    summary(trades)
    trades.to_csv("trades.csv", index=False)
    print(f"\nSaved {len(trades)} trades -> trades.csv")
    print(trades.head(10).to_string(index=False))
