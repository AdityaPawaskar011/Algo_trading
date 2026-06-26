"""
Real-time SENSEX + NIFTY tick feed via Upstox WebSocket (MarketDataStreamerV3).
Every tick: shows live spread, z-score and BUY/SELL/HOLD signal in terminal.
Also saves every tick to SQL Server tick_data table.

Usage:
    python tick_feed.py                    # history from yfinance_feed
    python tick_feed.py --source upstox    # history from upstox_feed
    python tick_feed.py --no-save          # stream only, skip SQL insert

Run during market hours (09:15 - 15:30 IST). Needs a valid token in
upstox_token.json — run  python live_fetch.py  first today if not set.

Press Ctrl+C to stop.
"""

import json
import os
import sys
import threading
from datetime import date, datetime

import pandas as pd
import pyodbc
import upstox_client

from config import DB_SERVER, DB_NAME, DB_DRIVER, TOKEN_FILE
from backtest import (
    load_from_sql, compute_signals,
    ENTRY, EXIT, RATIO_LOOKBACK, LOOKBACK,
)

SENSEX_KEY = "BSE_INDEX|SENSEX"
NIFTY_KEY  = "NSE_INDEX|Nifty 50"

TICK_TABLE = "tick_data"


# ── Load token ────────────────────────────────────────────────────────────────

def get_token() -> str:
    if not os.path.exists(TOKEN_FILE):
        raise FileNotFoundError(
            f"{TOKEN_FILE} not found. Run  python live_fetch.py  first to log in."
        )
    with open(TOKEN_FILE) as fh:
        data = json.load(fh)
    if data.get("date") != str(date.today()):
        raise ValueError(
            "Token expired (it's from a previous day). "
            "Run  python live_fetch.py  to get today's token."
        )
    return data["access_token"]


# ── SQL Server: create tick table & insert ────────────────────────────────────

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


def save_tick(tick_time, sensex, nifty, spread, zscore, signal, save=True):
    if not save:
        return
    try:
        conn   = get_conn()
        cursor = conn.cursor()
        cursor.execute(
            f"INSERT INTO {TICK_TABLE} "
            "(tick_time, sensex_ltp, nifty_ltp, spread, zscore, signal) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tick_time, sensex, nifty,
             round(spread, 2) if spread else None,
             round(zscore, 3) if zscore else None,
             signal),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass   # don't let DB errors kill the feed


# ── Live z-score engine ───────────────────────────────────────────────────────

class ZScoreEngine:
    """Holds historical data and computes live spread / z-score on every tick."""

    def __init__(self, df_history: pd.DataFrame):
        self._hist = df_history.copy()

    def compute(self, sensex: float, nifty: float) -> dict:
        today = pd.DataFrame([{
            "date":          pd.Timestamp(datetime.now()),
            "sensex_close":  sensex,
            "nifty_close":   nifty,
        }])
        df      = pd.concat([self._hist, today], ignore_index=True)
        signals = compute_signals(df)
        last    = signals.iloc[-1]

        z      = last["zscore"]
        spread = last["spread"]
        ratio  = last["ratio"]

        if pd.isna(z):
            return {"spread": None, "zscore": None, "ratio": None, "signal": "WAIT (warming up)"}

        if   z <= -ENTRY: signal = "BUY  SENSEX  +  SELL NIFTY"
        elif z >=  ENTRY: signal = "SELL SENSEX  +  BUY  NIFTY"
        elif abs(z) <= EXIT: signal = "EXIT / HOLD"
        else:             signal = "HOLD"

        return {
            "spread": round(spread, 2),
            "zscore": round(z, 3),
            "ratio":  round(ratio, 4),
            "signal": signal,
        }


# ── Tick processor ────────────────────────────────────────────────────────────

class TickProcessor:
    def __init__(self, engine: ZScoreEngine, save_ticks: bool):
        self._engine     = engine
        self._save       = save_ticks
        self._lock       = threading.Lock()
        self._sensex     = None
        self._nifty      = None
        self._tick_count = 0

    def _extract_ltp(self, feeds: dict, key: str):
        """Try multiple protobuf-to-dict layouts the SDK may produce."""
        entry = feeds.get(key) or feeds.get(key.replace("|", ":"))
        if not entry:
            return None
        # v3 ltpc layout
        ltpc = entry.get("ltpc") or {}
        if "ltp" in ltpc:
            return float(ltpc["ltp"])
        # v2 ff.marketFF.ltpc layout
        ff = entry.get("ff", {}).get("marketFF", {}).get("ltpc", {})
        if "ltp" in ff:
            return float(ff["ltp"])
        return None

    def process(self, message: dict):
        feeds = message.get("feeds", {})
        if not feeds:
            return

        sx = self._extract_ltp(feeds, SENSEX_KEY)
        nf = self._extract_ltp(feeds, NIFTY_KEY)

        with self._lock:
            if sx: self._sensex = sx
            if nf: self._nifty  = nf

            if not (self._sensex and self._nifty):
                return

            result = self._engine.compute(self._sensex, self._nifty)
            self._tick_count += 1
            now = datetime.now()

            # Terminal output — single updating line
            sig_color = result["signal"]
            line = (
                f"\r[{now.strftime('%H:%M:%S')}]  "
                f"SENSEX: {self._sensex:>10,.2f}  "
                f"NIFTY: {self._nifty:>9,.2f}  "
                f"Ratio: {result['ratio'] or '---':>7}  "
                f"Spread: {result['spread'] or '---':>8}  "
                f"Z: {result['zscore'] or '---':>7}  "
                f"Signal: {sig_color:<32}"
                f"  #{self._tick_count}"
            )
            print(line, end="", flush=True)

            # Save to SQL every tick
            save_tick(
                now,
                self._sensex, self._nifty,
                result["spread"], result["zscore"], result["signal"],
                save=self._save,
            )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    source     = "yfinance"
    save_ticks = "--no-save" not in sys.argv
    if "--source" in sys.argv:
        idx    = sys.argv.index("--source")
        source = sys.argv[idx + 1]

    print(f"Loading history from [{source}_feed] ...")
    df_history = load_from_sql(source)
    print(f"  {len(df_history)} historical rows  "
          f"({df_history['date'].min().date()} to {df_history['date'].max().date()})")

    engine    = ZScoreEngine(df_history)
    processor = TickProcessor(engine, save_ticks)

    if save_ticks:
        ensure_tick_table()
        print(f"  Ticks will be saved to [{TICK_TABLE}] table")

    token = get_token()

    config              = upstox_client.Configuration()
    config.access_token = token

    streamer = upstox_client.MarketDataStreamerV3(
        api_client     = upstox_client.ApiClient(config),
        instrumentKeys = [SENSEX_KEY, NIFTY_KEY],
        mode           = "ltpc",
    )
    streamer.auto_reconnect(enable=True, interval=2, retry_count=10)

    def on_open():
        print("\nConnected to Upstox WebSocket feed")
        print(f"Subscribed: {SENSEX_KEY}  |  {NIFTY_KEY}")
        print(f"Mode: ltpc (Last Traded Price)  |  Entry: +/-{ENTRY}  Exit: +/-{EXIT}\n")
        print("-" * 110)

    def on_message(message):
        processor.process(message)

    def on_error(error):
        print(f"\nStream error: {error}")

    def on_close(code, msg):
        print(f"\nStream closed — code={code}  msg={msg}")

    streamer.on("open",    on_open)
    streamer.on("message", on_message)
    streamer.on("error",   on_error)
    streamer.on("close",   on_close)

    print("Connecting to Upstox real-time feed ...")
    print("Press Ctrl+C to stop.\n")

    try:
        streamer.connect()
    except KeyboardInterrupt:
        print("\n\nStopped by user.")
        streamer.disconnect()


if __name__ == "__main__":
    main()
