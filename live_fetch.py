"""
Fetch today's SENSEX and NIFTY closing prices from Upstox
and upsert them into the SQL Server price_data table.

Usage:
    python live_fetch.py              # fetch today's close
    python live_fetch.py --date 2026-06-16   # fetch a specific date

Run after 15:45 IST on any trading day.  The script opens a browser for
Upstox login once per day; the token is cached in upstox_token.json.

Install deps first:
    pip install requests pyodbc
"""
import json
import os
import sys
import webbrowser
from datetime import date
from urllib.parse import urlparse, parse_qs, quote

import pyodbc
import requests

from config import (
    DB_SERVER, DB_NAME, DB_DRIVER,
    UPSTOX_API_KEY, UPSTOX_API_SECRET,
    UPSTOX_REDIRECT_URI, TOKEN_FILE,
)

BASE         = "https://api.upstox.com/v2"
SENSEX_KEY   = "BSE_INDEX|SENSEX"
NIFTY_KEY    = "NSE_INDEX|Nifty 50"


# ─── Auth ─────────────────────────────────────────────────────────────────────

def get_access_token() -> str:
    """Return a valid Upstox access token; prompt for OAuth if token is stale."""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as fh:
            cached = json.load(fh)
        if cached.get("date") == str(date.today()):
            print("Using cached token.")
            return cached["access_token"]

    auth_url = (
        f"{BASE}/login/authorization/dialog"
        f"?response_type=code"
        f"&client_id={UPSTOX_API_KEY}"
        f"&redirect_uri={UPSTOX_REDIRECT_URI}"
    )
    print("\nOpening Upstox login in your browser...")
    webbrowser.open(auth_url)
    print("Log in, then copy the full URL from the browser address bar")
    print("(it starts with https://127.0.0.1/?code=...)")
    redirected = input("Paste full redirect URL here: ").strip()

    code = parse_qs(urlparse(redirected).query).get("code", [None])[0]
    if not code:
        raise ValueError("Could not find ?code= in the URL you pasted.")

    resp = requests.post(
        f"{BASE}/login/authorization/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "code":          code,
            "client_id":     UPSTOX_API_KEY,
            "client_secret": UPSTOX_API_SECRET,
            "redirect_uri":  UPSTOX_REDIRECT_URI,
            "grant_type":    "authorization_code",
        },
    )
    resp.raise_for_status()
    access_token = resp.json()["access_token"]

    with open(TOKEN_FILE, "w") as fh:
        json.dump({"access_token": access_token, "date": str(date.today())}, fh)
    print("Token saved to", TOKEN_FILE)
    return access_token


# ─── Market data ──────────────────────────────────────────────────────────────

def fetch_close(token: str, for_date: date) -> tuple[date, float, float]:
    """Return (trade_date, sensex_close, nifty_close) for a given date."""
    date_str = str(for_date)
    headers  = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get_close(instrument_key: str, label: str) -> float:
        encoded = quote(instrument_key, safe="")
        url = f"{BASE}/historical-candle/{encoded}/day/{date_str}/{date_str}"
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        candles = r.json()["data"]["candles"]
        if not candles:
            raise ValueError(
                f"No candle data returned for {label} on {date_str}. "
                "Market may be closed or data not yet available — try after 16:00 IST."
            )
        # candle: [timestamp, open, high, low, close, volume, oi]
        return round(float(candles[0][4]), 2)

    sensex = get_close(SENSEX_KEY, "SENSEX")
    nifty  = get_close(NIFTY_KEY,  "NIFTY 50")
    return for_date, sensex, nifty


# ─── SQL Server upsert ────────────────────────────────────────────────────────

TABLE = "upstox_feed"


def upsert_price(trade_date: date, sensex_close: float, nifty_close: float) -> None:
    """Upsert a row into upstox_feed (creates the table if it doesn't exist)."""
    conn_str = (
        f"DRIVER={{{DB_DRIVER}}};"
        f"SERVER={DB_SERVER};"
        f"DATABASE={DB_NAME};"
        "Trusted_Connection=yes;"
    )
    conn   = pyodbc.connect(conn_str)
    cursor = conn.cursor()

    # Create table on first run
    cursor.execute(f"""
        IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = '{TABLE}')
        CREATE TABLE {TABLE} (
            trade_date   DATE PRIMARY KEY,
            sensex_close DECIMAL(12,2),
            nifty_close  DECIMAL(12,2)
        )
    """)

    cursor.execute(
        f"""
        MERGE {TABLE} AS tgt
        USING (SELECT ? AS trade_date, ? AS sensex_close, ? AS nifty_close) AS src
          ON tgt.trade_date = src.trade_date
        WHEN MATCHED THEN
            UPDATE SET sensex_close = src.sensex_close,
                       nifty_close  = src.nifty_close
        WHEN NOT MATCHED THEN
            INSERT (trade_date, sensex_close, nifty_close)
            VALUES (src.trade_date, src.sensex_close, src.nifty_close);
        """,
        (str(trade_date), sensex_close, nifty_close),
    )
    conn.commit()
    conn.close()
    print(f"[{TABLE}] saved -> {trade_date} | SENSEX {sensex_close:,.2f} | NIFTY {nifty_close:,.2f}")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    # Parse optional --date YYYY-MM-DD
    target_date = date.today()
    if "--date" in sys.argv:
        idx = sys.argv.index("--date")
        try:
            from datetime import datetime
            target_date = datetime.strptime(sys.argv[idx + 1], "%Y-%m-%d").date()
        except (IndexError, ValueError):
            print("Usage: python live_fetch.py --date YYYY-MM-DD")
            sys.exit(1)

    print(f"Fetching market data for {target_date} ...")
    token = get_access_token()
    td, sx, nf = fetch_close(token, target_date)
    upsert_price(td, sx, nf)
    print("Done.")


if __name__ == "__main__":
    main()
