#!/usr/bin/env python3
"""
Download last 2 years of 1-minute OHLC data from Upstox and save as:

    Old_data/
      2024/
        01_January/
          2024-01-01.csv
          2024-01-02.csv
          ...
        02_February/
          ...
      2025/
        ...

Each CSV row = one minute: timestamp, open, high, low, close, volume, open_interest

IMPORTANT
---------
* This uses the Upstox HISTORICAL CANDLE DATA API (a REST pull), NOT a webhook.
  A webhook only pushes live data; it cannot return history.
* 1-minute data is available from January 2022, so 2 years back is fine.
* The ACCESS_TOKEN is NOT hardcoded. It is obtained via the same Upstox OAuth
  flow the rest of the project uses (config.py credentials, cached daily in
  upstox_token.json) so it is always fresh.
* INSTRUMENT_KEY defaults to NIFTY 50; pass "sensex" on the command line to
  pull SENSEX instead, or pass any raw Upstox instrument key directly.

Run:
    pip install requests
    python old_data.py            # NIFTY 50  (default)
    python old_data.py sensex     # SENSEX
    python old_data.py "NSE_INDEX|Nifty 50"   # any explicit instrument key
"""

import os
import sys
import json
import time
import csv
import webbrowser
import datetime as dt
from collections import defaultdict
from urllib.parse import quote, urlparse, parse_qs

import requests

from config import (
    UPSTOX_API_KEY, UPSTOX_API_SECRET,
    UPSTOX_REDIRECT_URI, TOKEN_FILE,
)

# ----------------------------------------------------------------------
# 1. REQUIRED FIELDS  (filled from project config — no manual editing)
# ----------------------------------------------------------------------
# ACCESS_TOKEN is fetched at runtime via get_access_token() below.

# Named instruments (same keys used by live_fetch.py). The default is NIFTY.
INSTRUMENTS = {
    "nifty":  "NSE_INDEX|Nifty 50",
    "sensex": "BSE_INDEX|SENSEX",
}
DEFAULT_INSTRUMENT_KEY = INSTRUMENTS["nifty"]

YEARS_BACK = 2                       # how far back to pull
OUTPUT_ROOT = "Old_data"             # top-level folder name
REQUEST_PAUSE_SEC = 0.4              # be polite to the rate limiter
# ----------------------------------------------------------------------

MONTH_NAMES = ["", "01_January", "02_February", "03_March", "04_April",
               "05_May", "06_June", "07_July", "08_August", "09_September",
               "10_October", "11_November", "12_December"]

# v2 host is used for the OAuth login/token endpoints (matches live_fetch.py).
AUTH_BASE = "https://api.upstox.com/v2"
# v3 host is used for historical candles (supports the minutes/1 interval).
CANDLE_BASE = "https://api.upstox.com/v3/historical-candle"


# ─── Auth (same flow as live_fetch.py) ─────────────────────────────────────────

def get_access_token() -> str:
    """Return a valid Upstox access token; prompt for OAuth if token is stale."""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as fh:
            cached = json.load(fh)
        if cached.get("date") == str(dt.date.today()):
            print("Using cached token.")
            return cached["access_token"]

    auth_url = (
        f"{AUTH_BASE}/login/authorization/dialog"
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
        f"{AUTH_BASE}/login/authorization/token",
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
        json.dump({"access_token": access_token, "date": str(dt.date.today())}, fh)
    print("Token saved to", TOKEN_FILE)
    return access_token


# ─── Date helpers ──────────────────────────────────────────────────────────────

def month_ranges(years_back: int):
    """Yield (from_date, to_date) for each calendar month, oldest first."""
    today = dt.date.today()
    start = today.replace(day=1) - dt.timedelta(days=365 * years_back)
    start = start.replace(day=1)
    cur = start
    while cur <= today:
        if cur.month == 12:
            nxt = cur.replace(year=cur.year + 1, month=1, day=1)
        else:
            nxt = cur.replace(month=cur.month + 1, day=1)
        last_day = min(nxt - dt.timedelta(days=1), today)
        yield cur, last_day
        cur = nxt


# ─── Market data ────────────────────────────────────────────────────────────────

def fetch_month(token: str, instrument_key: str,
                from_date: dt.date, to_date: dt.date):
    """Call the 1-minute historical endpoint for one month. Returns candle list."""
    # The instrument key contains '|' and a space, so it MUST be URL-encoded.
    encoded = quote(instrument_key, safe="")
    # V3 path order is .../minutes/1/{to_date}/{from_date}
    url = (f"{CANDLE_BASE}/{encoded}/minutes/1/"
           f"{to_date.isoformat()}/{from_date.isoformat()}")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"   ! {from_date:%Y-%m}  HTTP {resp.status_code}: {resp.text[:160]}")
        return []
    return resp.json().get("data", {}).get("candles", [])


def write_day_csv(day: dt.date, rows: list, prefix: str = ""):
    """Write one day's minute rows to Old_data/YYYY/MM_Month/<prefix>YYYY-MM-DD.csv"""
    folder = os.path.join(OUTPUT_ROOT, f"{day.year}", MONTH_NAMES[day.month])
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{prefix}{day.isoformat()}.csv")
    rows.sort(key=lambda r: r[0])  # chronological within the day
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close",
                    "volume", "open_interest"])
        w.writerows(rows)
    return path


# ─── Entry point ──────────────────────────────────────────────────────────────

def resolve_instrument_key(arg: str | None) -> str:
    """Map a CLI arg ('nifty'/'sensex'/raw key) to an Upstox instrument key."""
    if not arg:
        return DEFAULT_INSTRUMENT_KEY
    return INSTRUMENTS.get(arg.lower(), arg)


def main():
    instrument_key = resolve_instrument_key(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"Instrument: {instrument_key}")

    # NIFTY (default) keeps the plain  YYYY-MM-DD.csv  name it already saved with;
    # any other instrument is prefixed so it sits beside it, e.g. sensex_2024-06-03.csv
    name = {v: k for k, v in INSTRUMENTS.items()}.get(instrument_key, "")
    prefix = "" if name in ("", "nifty") else f"{name}_"

    token = get_access_token()

    total_files = 0
    for from_date, to_date in month_ranges(YEARS_BACK):
        print(f"-> {from_date:%Y-%m}")
        candles = fetch_month(token, instrument_key, from_date, to_date)
        time.sleep(REQUEST_PAUSE_SEC)
        if not candles:
            continue

        # group the month's minute candles by calendar day
        by_day = defaultdict(list)
        for c in candles:
            # c = [timestamp, open, high, low, close, volume, oi]
            ts = c[0]
            day = dt.date.fromisoformat(ts[:10])
            by_day[day].append(c)

        for day, rows in by_day.items():
            write_day_csv(day, rows, prefix)
            total_files += 1

    print(f"\nDone. Wrote {total_files} daily CSV files under '{OUTPUT_ROOT}/'.")


if __name__ == "__main__":
    main()
