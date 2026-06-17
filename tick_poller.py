"""
Real-time tick poller — fetches SENSEX + NIFTY LTP from Upstox
REST API every second and stores each tick into SQL Server tick_data table.

Avoids WebSocket entirely (more reliable on Windows).
Same output as tick_feed.py — live spread, z-score, BUY/SELL signal.

Usage:
    python tick_poller.py                    # history from yfinance_feed
    python tick_poller.py --source upstox    # history from upstox_feed
    python tick_poller.py --interval 2       # poll every 2 seconds (default: 1)
    python tick_poller.py --no-save          # don't write to SQL

Run during market hours 09:15 - 15:30 IST.
Get today's token first:  python live_fetch.py
Press Ctrl+C to stop.
"""

import json
import os
import sys
import time
from datetime import date, datetime

import pyodbc
import requests
import pandas as pd

from config import DB_SERVER, DB_NAME, DB_DRIVER, TOKEN_FILE, UPSTOX_API_KEY
from backtest import load_from_sql, compute_signals, ENTRY, EXIT

BASE       = "https://api.upstox.com/v2"
SENSEX_KEY = "BSE_INDEX|SENSEX"
NIFTY_KEY  = "NSE_INDEX|Nifty 50"
TICK_TABLE = "tick_data"


# ── Token ─────────────────────────────────────────────────────────────────────

def get_token() -> str:
    if not os.path.exists(TOKEN_FILE):
        raise FileNotFoundError(
            "upstox_token.json not found.\n"
            "Run  python live_fetch.py  first to log in today."
        )
    with open(TOKEN_FILE) as fh:
        data = json.load(fh)
    if data.get("date") != str(date.today()):
        raise ValueError(
            f"Token is from {data.get('date')} — expired.\n"
            "Run  python live_fetch.py  to get today's fresh token."
        )
    return data["access_token"]


# ── SQL Server ────────────────────────────────────────────────────────────────

def get_conn():
    return pyodbc.connect(
        f"DRIVER={{{DB_DRIVER}}};"
        f"SERVER={DB_SERVER};"
        f"DATABASE={DB_NAME};"
        "Trusted_Connection=yes;"
    )


def ensure_tick_table():
    conn   = get_conn()
    cursor = conn.cursor()
    cursor.execute(f"""
        IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = '{TICK_TABLE}')
        CREATE TABLE {TICK_TABLE} (
            id           INT IDENTITY(1,1) PRIMARY KEY,
            tick_time    DATETIME       NOT NULL,
            sensex_ltp   DECIMAL(12,2)  NOT NULL,
            nifty_ltp    DECIMAL(12,2)  NOT NULL,
            spread       DECIMAL(12,2),
            zscore       DECIMAL(8,3),
            signal       VARCHAR(40)
        )
    """)
    conn.commit()
    conn.close()
    print(f"[{TICK_TABLE}] table ready in SQL Server.")


def save_tick(tick_time, sensex, nifty, spread, zscore, signal):
    try:
        conn   = get_conn()
        cursor = conn.cursor()
        cursor.execute(
            f"INSERT INTO {TICK_TABLE} "
            "(tick_time, sensex_ltp, nifty_ltp, spread, zscore, signal) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                tick_time,
                round(sensex, 2),
                round(nifty,  2),
                round(spread, 2) if spread is not None else None,
                round(zscore, 3) if zscore is not None else None,
                signal,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"\n  [DB error] {e}")


# ── Upstox LTP fetch ──────────────────────────────────────────────────────────

def fetch_ltp(token: str) -> tuple[float, float]:
    """Returns (sensex_ltp, nifty_ltp) from Upstox REST quote API."""
    from urllib.parse import quote
    keys = f"{SENSEX_KEY},{NIFTY_KEY}"
    url  = f"{BASE}/market-quote/ltp?instrument_key={quote(keys, safe=',')}"
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=5,
    )
    r.raise_for_status()
    data = r.json().get("data", {})

    # Upstox returns keys with ':' instead of '|' in response
    def find(target_key):
        for k, v in data.items():
            if target_key.replace("|", ":") in k or target_key in k:
                return float(v.get("last_price", 0))
        return None

    sensex = find(SENSEX_KEY)
    nifty  = find(NIFTY_KEY)

    if sensex is None or nifty is None:
        raise ValueError(f"Could not parse LTP from response: {data}")
    return sensex, nifty


# ── Z-score engine ────────────────────────────────────────────────────────────

def compute_live(df_history: pd.DataFrame, sensex: float, nifty: float) -> dict:
    today = pd.DataFrame([{
        "date":         pd.Timestamp(datetime.now()),
        "sensex_close": sensex,
        "nifty_close":  nifty,
    }])
    df   = pd.concat([df_history, today], ignore_index=True)
    sigs = compute_signals(df)
    last = sigs.iloc[-1]

    z      = last["zscore"]
    spread = last["spread"]
    ratio  = last["ratio"]

    if pd.isna(z):
        return {"spread": None, "zscore": None, "ratio": None, "signal": "WARMING UP"}

    if   z <= -ENTRY: signal = "BUY  SENSEX  +  SELL NIFTY"
    elif z >=  ENTRY: signal = "SELL SENSEX  +  BUY  NIFTY"
    elif abs(z) <= 0.3: signal = "EXIT / HOLD"
    else:             signal = "HOLD"

    return {
        "spread": round(spread, 2),
        "zscore": round(z, 3),
        "ratio":  round(ratio, 4),
        "signal": signal,
    }


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    source   = "yfinance"
    interval = 1
    save_db  = "--no-save" not in sys.argv

    if "--source" in sys.argv:
        idx    = sys.argv.index("--source")
        source = sys.argv[idx + 1]
    if "--interval" in sys.argv:
        idx      = sys.argv.index("--interval")
        interval = int(sys.argv[idx + 1])

    print(f"Loading history from [{source}_feed] ...")
    df_history = load_from_sql(source)
    print(f"  {len(df_history)} rows  "
          f"({df_history['date'].min().date()} to {df_history['date'].max().date()})")

    token = get_token()
    print(f"  Token valid for today ({date.today()})")

    if save_db:
        ensure_tick_table()

    print(f"\nPolling every {interval}s  |  Entry: +/-{ENTRY}  Exit: +/-{EXIT}")
    print(f"Saving to SQL: {'YES -> tick_data' if save_db else 'NO (--no-save)'}")
    print(f"Press Ctrl+C to stop.\n")
    print(f"{'TIME':<10} {'SENSEX':>12} {'NIFTY':>11} {'RATIO':>8} "
          f"{'SPREAD':>10} {'Z-SCORE':>8}  SIGNAL")
    print("-" * 100)

    tick_count = 0

    try:
        while True:
            loop_start = time.time()
            try:
                sensex, nifty = fetch_ltp(token)
                result        = compute_live(df_history, sensex, nifty)
                now           = datetime.now()
                tick_count   += 1

                sig = result["signal"]
                # Highlight entry signals
                marker = " ***" if "BUY" in sig or "SELL" in sig else ""

                print(
                    f"\r{now.strftime('%H:%M:%S'):<10} "
                    f"{sensex:>12,.2f} "
                    f"{nifty:>11,.2f} "
                    f"{result['ratio'] or '':>8} "
                    f"{result['spread'] or '':>10} "
                    f"{result['zscore'] or '':>8}  "
                    f"{sig:<35}{marker}  #{tick_count}",
                    end="", flush=True,
                )

                if save_db:
                    save_tick(now, sensex, nifty,
                              result["spread"], result["zscore"], result["signal"])

            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 401:
                    print("\n\nToken expired mid-session. Run  python live_fetch.py  and restart.")
                    break
                print(f"\n  [HTTP {e.response.status_code}] {e}")
            except Exception as e:
                print(f"\n  [Error] {e}")

            # Sleep remainder of interval
            elapsed = time.time() - loop_start
            sleep_for = max(0, interval - elapsed)
            time.sleep(sleep_for)

    except KeyboardInterrupt:
        print(f"\n\nStopped. {tick_count} ticks collected.")
        if save_db:
            print(f"All saved to [{TICK_TABLE}] in SQL Server.")
            print(f"Query:  SELECT TOP 100 * FROM {TICK_TABLE} ORDER BY tick_time DESC")


if __name__ == "__main__":
    main()
