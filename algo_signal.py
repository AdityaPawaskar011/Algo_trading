"""
Live algo signal generator for the Sensex-Nifty Ratio Spread strategy.

Reads recent historical data from SQL, fetches today's live price from
Upstox, computes the dynamic rolling ratio + z-score, and prints the
current signal: LONG_SPREAD / SHORT_SPREAD / HOLD.

Usage:
    python algo_signal.py                  # uses yfinance_feed + live Upstox price
    python algo_signal.py --source upstox  # uses upstox_feed + live Upstox price
    python algo_signal.py --no-live        # uses only SQL data (no Upstox call)
"""
import os
import sys
import json
import pyodbc
import requests
import pandas as pd
from datetime import date
from urllib.parse import quote, urlparse, parse_qs
import webbrowser

from config import (
    DB_SERVER, DB_NAME, DB_DRIVER,
    UPSTOX_API_KEY, UPSTOX_API_SECRET,
    UPSTOX_REDIRECT_URI, TOKEN_FILE,
)
from backtest import RATIO_LOOKBACK, LOOKBACK, ENTRY, EXIT

BASE       = "https://api.upstox.com/v2"
SENSEX_KEY = "BSE_INDEX|SENSEX"
NIFTY_KEY  = "NSE_INDEX|Nifty 50"


# ─── Load historical data from SQL ───────────────────────────────────────────

def load_history(source: str) -> pd.DataFrame:
    table = "yfinance_feed" if source == "yfinance" else "upstox_feed"
    conn_str = (
        f"DRIVER={{{DB_DRIVER}}};"
        f"SERVER={DB_SERVER};"
        f"DATABASE={DB_NAME};"
        "Trusted_Connection=yes;"
    )
    conn   = pyodbc.connect(conn_str)
    cursor = conn.cursor()
    # Fetch enough rows for both rolling windows
    rows_needed = RATIO_LOOKBACK + LOOKBACK + 10
    cursor.execute(
        f"SELECT TOP {rows_needed} trade_date, sensex_close, nifty_close "
        f"FROM {table} ORDER BY trade_date DESC"
    )
    rows = cursor.fetchall()
    conn.close()
    df = pd.DataFrame(
        [(r[0], float(r[1]), float(r[2])) for r in rows],
        columns=["date", "sensex_close", "nifty_close"],
    )
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


# ─── Upstox token (reuse from live_fetch) ────────────────────────────────────

def get_access_token() -> str:
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as fh:
            cached = json.load(fh)
        if cached.get("date") == str(date.today()):
            return cached["access_token"]

    auth_url = (
        f"{BASE}/login/authorization/dialog"
        f"?response_type=code"
        f"&client_id={UPSTOX_API_KEY}"
        f"&redirect_uri={UPSTOX_REDIRECT_URI}"
    )
    print("Opening Upstox login in browser...")
    webbrowser.open(auth_url)
    print("After login, copy the full redirect URL (https://127.0.0.1/?code=...)")
    redirected = input("Paste URL here: ").strip()
    code = parse_qs(urlparse(redirected).query).get("code", [None])[0]
    if not code:
        raise ValueError("Could not find ?code= in URL.")

    resp = requests.post(
        f"{BASE}/login/authorization/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "code": code, "client_id": UPSTOX_API_KEY,
            "client_secret": UPSTOX_API_SECRET,
            "redirect_uri": UPSTOX_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
    )
    resp.raise_for_status()
    access_token = resp.json()["access_token"]
    with open(TOKEN_FILE, "w") as fh:
        json.dump({"access_token": access_token, "date": str(date.today())}, fh)
    return access_token


# ─── Fetch live LTP from Upstox ──────────────────────────────────────────────

def fetch_live_prices(token: str) -> tuple[float, float]:
    """Return (sensex_ltp, nifty_ltp) from Upstox market quote API."""
    keys = f"{SENSEX_KEY},{NIFTY_KEY}"
    url  = f"{BASE}/market-quote/quotes?instrument_key={quote(keys, safe=',')}"
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()["data"]

    sensex = float(data[SENSEX_KEY.replace("|", ":")]["last_price"])
    nifty  = float(data[NIFTY_KEY.replace("|", ":")]["last_price"])
    return sensex, nifty


# ─── Signal computation ───────────────────────────────────────────────────────

def compute_signal(df: pd.DataFrame) -> dict:
    """Compute rolling ratio, spread, z-score and return current signal."""
    df = df.copy()
    df["ratio"]     = (df["sensex_close"] / df["nifty_close"]).rolling(RATIO_LOOKBACK).mean()
    df["spread"]    = df["sensex_close"] - df["ratio"] * df["nifty_close"]
    df["spread_ma"] = df["spread"].rolling(LOOKBACK).mean()
    df["spread_sd"] = df["spread"].rolling(LOOKBACK).std()
    df["zscore"]    = (df["spread"] - df["spread_ma"]) / df["spread_sd"]

    last = df.iloc[-1]
    z    = last["zscore"]

    if z <= -ENTRY:
        signal = "LONG_SPREAD"    # Sensex cheap vs Nifty -> BUY Sensex, SELL Nifty
        action = "BUY  SENSEX futures  |  SELL NIFTY futures"
    elif z >= ENTRY:
        signal = "SHORT_SPREAD"   # Sensex rich vs Nifty  -> SELL Sensex, BUY Nifty
        action = "SELL SENSEX futures  |  BUY  NIFTY futures"
    elif abs(z) <= EXIT:
        signal = "EXIT / HOLD"
        action = "Close open position if any, or stay flat"
    else:
        signal = "HOLD"
        action = "No new trade — waiting for z-score to reach entry threshold"

    return {
        "date":          str(last["date"].date()),
        "sensex":        round(last["sensex_close"], 2),
        "nifty":         round(last["nifty_close"], 2),
        "dynamic_ratio": round(last["ratio"], 4),
        "spread":        round(last["spread"], 2),
        "spread_type":   "CREDIT (Sensex cheap)" if last["spread"] < 0 else "DEBIT  (Sensex rich)",
        "zscore":        round(z, 3),
        "signal":        signal,
        "action":        action,
    }


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    source   = "yfinance"
    use_live = True

    if "--source" in sys.argv:
        idx    = sys.argv.index("--source")
        source = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "yfinance"
    if "--no-live" in sys.argv:
        use_live = False

    print(f"Loading history from [{source}_feed] ...")
    df = load_history(source)
    print(f"  {len(df)} rows  ({df['date'].iloc[0].date()} to {df['date'].iloc[-1].date()})")

    if use_live:
        try:
            token = get_access_token()
            sx, nf = fetch_live_prices(token)
            today_row = pd.DataFrame([{
                "date":          pd.Timestamp(date.today()),
                "sensex_close":  sx,
                "nifty_close":   nf,
            }])
            df = pd.concat([df, today_row], ignore_index=True)
            print(f"  Live price appended -> SENSEX {sx:,.2f}  NIFTY {nf:,.2f}")
        except Exception as e:
            print(f"  Live fetch failed ({e}), using last SQL row.")

    result = compute_signal(df)

    print()
    print("=" * 55)
    print("  ALGO SIGNAL REPORT")
    print("=" * 55)
    for k, v in result.items():
        print(f"  {k:<18}: {v}")
    print("=" * 55)

    if result["signal"] in ("LONG_SPREAD", "SHORT_SPREAD"):
        print(f"\n  *** ACTION: {result['action']} ***\n")
    else:
        print(f"\n  No trade. {result['action']}\n")


if __name__ == "__main__":
    main()
