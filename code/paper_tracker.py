"""
paper_tracker.py — FORWARD daily paper-trade tracker for the validated edge:

    LONG spread only.  Enter when daily z <= -2 (BUY SENSEX fut / SELL NIFTY fut).
    Hold across days until:  +TARGET pts profit  |  -STOP pts  |  MAXHOLD days.
    No small-reversion exit.  SHORT signals ignored.

Run ONCE per trading day, AFTER the 15:30 close (the feed for the day must have
run, so sensex_today.csv / nifty_today.csv hold today's data). It keeps state in
tracker_daily.csv (the growing daily series), tracker_position.json (open trade),
and tracker_trades.csv (closed trades). Idempotent per day.

Run from feed_data/:
    python ..\\code\\paper_tracker.py
    python ..\\code\\paper_tracker.py --date 2026-06-25 --sensex-close 76900 --nifty-close 24010
"""
import sys
import os
import json
import pandas as pd

# ── validated strategy params ──
ENTRY_Z, TARGET, STOP, MAXHOLD = -2.0, 70, 100, 30
COST, PV = 4, 20    # Rs80 round-trip (Rs20/order x4 orders)
RATIO_WIN, SPREAD_WIN = 60, 15

DAILY = "tracker_daily.csv"; POSF = "tracker_position.json"; TRADES = "tracker_trades.csv"

# feed days to seed a continuous daily series on first run (history.csv ends 06-17)
SEED_FEED = [
    ("2026-06-18", "sensex_Nifty/sensex_data.csv",        "sensex_Nifty/nifty_data.csv"),
    ("2026-06-19", "sensex_data_2026-06-19_archived.csv", "nifty_data_2026-06-19_archived.csv"),
    ("2026-06-22", "sensex_data.csv",                     "nifty_data.csv"),
    ("2026-06-23", "sensex_today_2026-06-23_archived.csv","nifty_today_2026-06-23_archived.csv"),
    ("2026-06-24", "sensex_today.csv",                    "nifty_today.csv"),
]


def arg(flag, d=None): return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else d


def last_close(path):
    df = pd.read_csv(path, usecols=["tick_time", "last_price"]).dropna()
    return pd.to_datetime(df.tick_time.iloc[-1]).date(), float(df.last_price.iloc[-1])


def seed_series():
    df = pd.read_csv("history.csv"); df["date"] = pd.to_datetime(df["date"])
    rows = []
    for d, sxf, nff in SEED_FEED:
        try:
            rows.append({"date": pd.Timestamp(d), "sensex_close": last_close(sxf)[1],
                         "nifty_close": last_close(nff)[1]})
        except Exception:
            pass
    if rows:
        df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
    return df.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def todays_close():
    if "--sensex-close" in sys.argv:
        return pd.Timestamp(arg("--date")).date(), float(arg("--sensex-close")), float(arg("--nifty-close"))
    ds, sx = last_close("sensex_today.csv"); dn, nf = last_close("nifty_today.csv")
    return ds, sx, nf


def signals(df):
    d = df.copy()
    d["ratio"]  = (d.sensex_close / d.nifty_close).rolling(RATIO_WIN).mean()
    d["spread"] = d.sensex_close - d.ratio * d.nifty_close
    d["sma"]    = d.spread.rolling(SPREAD_WIN).mean()
    d["ssd"]    = d.spread.rolling(SPREAD_WIN).std()
    d["z"]      = (d.spread - d.sma) / d.ssd
    return d


def log_trade(row):
    hdr = not os.path.exists(TRADES)
    pd.DataFrame([row]).to_csv(TRADES, mode="a", header=hdr, index=False)


def main():
    df = pd.read_csv(DAILY) if os.path.exists(DAILY) else seed_series()
    df["date"] = pd.to_datetime(df["date"])
    dt, sx, nf = todays_close()
    dt = pd.Timestamp(dt)
    if dt not in set(df.date):
        df = pd.concat([df, pd.DataFrame([{"date": dt, "sensex_close": sx, "nifty_close": nf}])],
                       ignore_index=True)
    df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    df.to_csv(DAILY, index=False)

    s = signals(df)
    today = s.iloc[-1]
    z, spread = today.z, today.spread
    pos = json.load(open(POSF)) if os.path.exists(POSF) else None
    closed_today = False
    action = ""

    if pos:
        live = spread - pos["entry_spread"]            # LONG spread
        held = int((df.date > pd.Timestamp(pos["entry_date"])).sum())
        why = ("profit_target" if live >= TARGET else
               "stop_loss" if live <= -STOP else
               "max_hold" if held >= MAXHOLD else "")
        if why:
            net = round(live - COST, 1)
            log_trade({"entry_date": pos["entry_date"], "exit_date": str(dt.date()),
                       "direction": "LONG_SPREAD", "holding_days": held,
                       "entry_spread": round(pos["entry_spread"], 2), "exit_spread": round(spread, 2),
                       "gross_pnl_points": round(live, 1), "cost_points": COST,
                       "net_pnl_points": net, "net_Rs_per_lot": round(net * PV), "exit_reason": why})
            os.remove(POSF); pos = None; closed_today = True
            action = f"CLOSED LONG [{why}]  gross {live:+.1f}  NET {net:+.1f} pts = Rs{net*PV:+,.0f}/lot"
        else:
            action = f"HOLDING LONG  open {live:+.1f} pts (gross)  day {held}/{MAXHOLD}"

    if pos is None and not closed_today:
        if z <= ENTRY_Z:
            pos = {"direction": "LONG_SPREAD", "entry_date": str(dt.date()),
                   "entry_spread": round(spread, 2), "entry_sensex": round(sx, 2),
                   "entry_nifty": round(nf, 2), "entry_z": round(z, 3)}
            json.dump(pos, open(POSF, "w"))
            action = f"*** ENTERED LONG spread  (z {z:+.2f})  BUY SENSEX fut + SELL NIFTY fut ***"
        elif not action:
            action = f"FLAT - no entry (z {z:+.2f}; need <= {ENTRY_Z} for LONG)"

    # status
    realized = 0.0
    if os.path.exists(TRADES):
        tt = pd.read_csv(TRADES); realized = tt.net_pnl_points.sum()
    print("=" * 64)
    print(f"  PAPER TRACKER  {dt.date()}   (LONG-spread, target {TARGET}/stop {STOP}/{MAXHOLD}d)")
    print("=" * 64)
    print(f"  SENSEX {sx:,.2f} | NIFTY {nf:,.2f} | spread {spread:+.1f} | z {z:+.2f}")
    print(f"  ACTION : {action}")
    print(f"  POSITION: {'LONG since ' + pos['entry_date'] if pos else 'flat'}")
    if os.path.exists(TRADES):
        n = len(pd.read_csv(TRADES))
        print(f"  CLOSED TRADES: {n} | realized NET {realized:+.1f} pts = Rs{realized*PV:+,.0f}/lot")
    print("=" * 64)
    print("  Run again after tomorrow's close:  python ..\\code\\paper_tracker.py")


if __name__ == "__main__":
    main()
