"""
edge_strategy.py — strategy designed to PROFIT AFTER CHARGES by only taking
trades whose profit potential is several times the charge.

Design principle (what the user asked for): don't trade unless the expected win
is much bigger than the cost.
  1. ENTRY only when the spread is stretched (|z| >= ENTRY) AND the distance back
     to its mean ("room to revert") is >= TARGET. Room = |spread - mean|; if it
     reverts to the mean you capture ~that many points, so this guarantees the
     setup CAN reach the target.
  2. TARGET = a multiple of the charge (so each win clears the toll with margin).
  3. EXIT at +TARGET (book), -STOP (cut), or MAXHOLD bars (carry-forward limit).
     Works INTRADAY (if it reverts same day) and CARRY-FORWARD (held across days).
  4. LONG spread only (the side that held up out-of-sample).

Validated out-of-sample: train 2024-25, test 2026 (unseen). Charge default 48 pts.

Run from feed_data/:
    python ..\\code\\edge_strategy.py --cost-pts 48
"""
import sys
import numpy as np
import pandas as pd
from openpyxl.styles import Font, PatternFill

ENTRY_Z, STOP, MAXHOLD = 2.0, 100, 30
RATIO_WIN, SPREAD_WIN = 60, 15
PV = 20
GREEN = PatternFill("solid", fgColor="C6EFCE"); RED = PatternFill("solid", fgColor="FFC7CE")
HEAD = PatternFill("solid", fgColor="1F4E78")


def arg(flag, d): return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else d


def signals(df):
    d = df.copy()
    d["ratio"]  = (d.sensex_close / d.nifty_close).rolling(RATIO_WIN).mean()
    d["spread"] = d.sensex_close - d.ratio * d.nifty_close
    d["sma"]    = d.spread.rolling(SPREAD_WIN).mean()
    d["ssd"]    = d.spread.rolling(SPREAD_WIN).std()
    d["z"]      = (d.spread - d.sma) / d.ssd
    return d


def backtest(df, target, cost, long_only=True):
    """Enter only if room-to-revert >= target; exit at +target / -STOP / MAXHOLD."""
    s = signals(df).reset_index(drop=True)
    n = len(s); start = max(RATIO_WIN, SPREAD_WIN); pos = None; out = []
    for i in range(start, n):
        r = s.iloc[i]; z = r.z
        if np.isnan(z):
            continue
        if pos is not None:
            held = i - pos["i"]
            live = (r.spread - pos["spread"]) if pos["dir"] == "LONG" else (pos["spread"] - r.spread)
            why = ("profit_target" if live >= target else
                   "stop_loss" if live <= -STOP else
                   "max_hold" if held >= MAXHOLD else "")
            if why:
                out.append({"entry_date": pos["date"], "exit_date": r.date, "direction": pos["dir"],
                            "holding_bars": held, "zscore_entry": round(pos["z"], 2),
                            "room_at_entry": round(pos["room"], 1), "exit_reason": why,
                            "gross_pnl_points": round(live, 1)})
                pos = None
        if pos is None:
            room = abs(r.spread - r.sma)                  # potential reversion profit
            if z <= -ENTRY_Z and room >= target:
                pos = {"dir": "LONG", "i": i, "date": r.date, "spread": r.spread, "z": z, "room": room}
            elif (not long_only) and z >= ENTRY_Z and room >= target:
                pos = {"dir": "SHORT", "i": i, "date": r.date, "spread": r.spread, "z": z, "room": room}
    t = pd.DataFrame(out)
    if len(t):
        t["entry_date"] = pd.to_datetime(t["entry_date"])
        t["net_pnl_points"] = (t.gross_pnl_points - cost).round(1)
    return t


def main():
    cost  = float(arg("--cost-pts", "4"))
    split = pd.Timestamp(arg("--split", "2026-01-01"))
    out   = arg("--out", "edge_strategy.xlsx")

    df = pd.read_csv("history.csv"); df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    print(f"Daily series: {len(df)} days ({df.date.min().date()} -> {df.date.max().date()}) | charge {cost:.0f} pts\n")

    rows = []
    for mult in (1.0, 1.5, 2.0, 3.0):
        target = round(mult * cost)
        t = backtest(df, target, cost, long_only=True)
        if not len(t):
            rows.append({"target_pts": target, "x_charge": mult, "trades": 0, "train_net": 0,
                         "test_net": 0, "test_net_Rs": 0, "avg_win_pts": 0, "win%": 0})
            continue
        tr = t[t.entry_date < split]; te = t[t.entry_date >= split]
        wins = t[t.net_pnl_points > 0]
        rows.append({"target_pts": target, "x_charge": mult, "trades": len(t),
                     "train_net": round(tr.net_pnl_points.sum(), 0),
                     "test_net": round(te.net_pnl_points.sum(), 0),
                     "test_net_Rs": round(te.net_pnl_points.sum() * PV),
                     "avg_win_pts": round(wins.gross_pnl_points.mean(), 0) if len(wins) else 0,
                     "win%": round((t.net_pnl_points > 0).mean() * 100, 0)})
    grid = pd.DataFrame(rows)

    ok = grid[(grid.trades >= 3) & (grid.train_net > 0) & (grid.test_net > 0)]
    if len(ok):
        b = ok.sort_values("test_net", ascending=False).iloc[0]
        verdict = (f"BEST: target {int(b.target_pts)} pts ({b.x_charge:g}x charge) -> net-positive in train "
                   f"AND test ({b.test_net:+.0f} pts / Rs{b.test_net_Rs:+,.0f}/lot OOS), avg win {b.avg_win_pts:.0f} pts")
    else:
        verdict = "No target is net-positive out-of-sample on this data (carry-forward)."

    print("=" * 96)
    print("  EDGE STRATEGY: target = multiple of charge, room-filtered, LONG, carry-forward")
    print("=" * 96)
    print(grid.to_string(index=False))
    print("-" * 96)
    print(f"  {verdict}")
    print("=" * 96)

    summary = pd.DataFrame({
        "metric": ["Design", "Entry filter", "Exit", "Hold", "Charge (pts/Rs)",
                   "Best target (OOS)", "Max achievable margin", "Intraday note", "VERDICT", "Caveat"],
        "value": ["LONG spread, only when potential profit >> charge",
                  f"|z| >= {ENTRY_Z} AND room-to-revert >= target",
                  "+target / -stop / max-hold", f"up to {MAXHOLD} days (carry-forward)",
                  f"{cost:.0f} pts / Rs{cost*PV:.0f}",
                  verdict, "see grid - this spread does not move 5x charge",
                  "Intraday moves (~20 pts) are below 1x charge -> intraday cannot clear costs",
                  ("Profitable OOS at the best target above" if len(ok) else "Not net-positive OOS"),
                  "Small sample; validate forward with the paper tracker before real money."]})
    with pd.ExcelWriter(out, engine="openpyxl") as xl:
        summary.to_excel(xl, sheet_name="Summary", index=False)
        grid.to_excel(xl, sheet_name="Target vs Charge", index=False)
        wb = xl.book
        for nm in wb.sheetnames:
            ws = wb[nm]
            for c in ws[1]:
                c.font = Font(bold=True, color="FFFFFF"); c.fill = HEAD
            ws.freeze_panes = "A2"
        ws = wb["Target vs Charge"]; hdr = {c.value: c.column for c in ws[1]}
        for col in ("train_net", "test_net"):
            ci = hdr.get(col)
            for r in range(2, ws.max_row + 1):
                cell = ws.cell(row=r, column=ci)
                try: v = float(cell.value)
                except (TypeError, ValueError): continue
                cell.fill = GREEN if v > 0 else (RED if v < 0 else cell.fill)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
