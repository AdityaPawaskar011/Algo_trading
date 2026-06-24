import pyodbc, requests, json, os, pandas as pd
from datetime import datetime
from urllib.parse import quote
from config import DB_SERVER, DB_NAME, DB_DRIVER, TOKEN_FILE
from backtest import load_from_sql, compute_signals

# ── live price ────────────────────────────────────────────────────────────────
with open(TOKEN_FILE) as f:
    token = json.load(f)["access_token"]

keys = "BSE_INDEX|SENSEX,NSE_INDEX|Nifty 50"
r = requests.get(
    "https://api.upstox.com/v2/market-quote/ltp?instrument_key=" + quote(keys, safe=","),
    headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    timeout=5,
)
data = r.json().get("data", {})
sx, nf = None, None
for k, v in data.items():
    if "SENSEX" in k: sx = float(v["last_price"])
    if "Nifty" in k or "NIFTY" in k: nf = float(v["last_price"])

# ── z-score ───────────────────────────────────────────────────────────────────
df = load_from_sql("yfinance")
today = pd.DataFrame([{"date": pd.Timestamp(datetime.now()), "sensex_close": sx, "nifty_close": nf}])
sig = compute_signals(pd.concat([df, today], ignore_index=True)).iloc[-1]
cur_spread = round(float(sig["spread"]), 2)
cur_z      = round(float(sig["zscore"]), 3)
cur_ratio  = round(float(sig["ratio"]), 4)

# ── open trade ────────────────────────────────────────────────────────────────
conn = pyodbc.connect("DRIVER={" + DB_DRIVER + "};SERVER=" + DB_SERVER + ";DATABASE=" + DB_NAME + ";Trusted_Connection=yes;")
cur = conn.cursor()
cur.execute("SELECT id, direction, entry_spread, entry_zscore, entry_time FROM paper_trade WHERE status = 'OPEN' ORDER BY entry_time DESC")
row = cur.fetchone()

cur.execute("SELECT COUNT(*) FROM paper_trade WHERE status = 'CLOSED'")
closed = cur.fetchone()[0]
cur.execute("SELECT SUM(pnl_pts), COUNT(*) FROM paper_trade WHERE status = 'CLOSED' AND pnl_pts > 0")
wins = cur.fetchone()
conn.close()

print("=" * 55)
print(" PAPER TRADE LIVE STATUS")
print("=" * 55)
print("SENSEX  : {:>12,.2f}".format(sx))
print("NIFTY   : {:>12,.2f}".format(nf))
print("Ratio   : {:>12,.4f}".format(cur_ratio))
print("Spread  : {:>12,.2f}".format(cur_spread))
print("Z-score : {:>12,.3f}".format(cur_z))
print("-" * 55)

if row:
    entry_sp  = float(row[2])
    direction = row[1]
    pnl = (entry_sp - cur_spread) if direction == "SHORT" else (cur_spread - entry_sp)
    pnl_rs = round(pnl * 100)
    print("OPEN TRADE  : {} SPREAD  (id={})".format(direction, row[0]))
    print("Entered at  : {}".format(str(row[4])[11:19]))
    print("Entry spread: {:+.2f}  Entry Z: {:+.3f}".format(entry_sp, float(row[3])))
    print("Live P&L    : {:+.2f} pts  (Rs {:+,})".format(pnl, pnl_rs))
    print()
    if direction == "SHORT":
        print("Exit when   : Z drops BELOW 0.70  (currently {})".format(cur_z))
        pts_needed = cur_z - 0.70
        print("Z still needs to fall {:.3f} more before exit".format(pts_needed))
    else:
        print("Exit when   : Z rises ABOVE 0.70  (currently {})".format(cur_z))
    print()
    if pnl >= 0:
        print("STATUS : PROFITABLE -- holding for bigger exit")
    else:
        print("STATUS : LOSS -- holding, stop loss at -100 pts")
else:
    print("No open trade right now.")

print("-" * 55)
print("Closed trades: {}  |  Wins: {}".format(closed, wins[1] if wins[0] else 0))
print("=" * 55)
