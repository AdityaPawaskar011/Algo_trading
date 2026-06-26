"""
Real-time tick poller — fetches SENSEX + NIFTY LTP from Upstox
REST API every second and stores each tick into a CSV file.

Avoids WebSocket entirely (more reliable on Windows).
Same output as tick_feed.py — live spread, z-score, BUY/SELL signal.

NOTE: Database (SQL Server) storage is currently DISABLED.
      - Every field the Upstox full-quote feed returns is captured per tick.
      - SENSEX rows are appended to   sensex_data.csv
      - NIFTY  rows are appended to   nifty_data.csv
        (the raw_json column in each row holds the complete feed payload)
      - Historical warm-up data is read from   history.csv
        (auto-downloaded from Yahoo Finance on first run if missing)
      The original DB code is kept below, commented out, so it can be
      re-enabled later.

Usage:
    python tick_poller.py                    # auto dated folder (see below)
    python tick_poller.py --interval 2       # poll every 2 seconds (default: 1)
    python tick_poller.py --no-save          # don't write ticks to CSV
    python tick_poller.py --sensex-out X --nifty-out Y   # manual paths (no dated folder)

By DEFAULT each run creates a dated folder for today and writes two dated CSVs
into it -- <YYYY_MM_DD>/sensex_<YYYY-MM-DD>.csv and .../nifty_<YYYY-MM-DD>.csv --
and also mirrors the live data to sensex_today.csv / nifty_today.csv so the rest
of the pipeline (paper_tracker.py, live_trades_today.py) keeps working unchanged.
Pass --sensex-out / --nifty-out to override with explicit paths (manual mode).

Run during market hours 09:15 - 15:30 IST.
Get today's token first:  python live_fetch.py
Press Ctrl+C to stop.
"""

import csv
import json
import os
import sys
import time
from datetime import date, datetime

# import pyodbc                 # DB disabled — feed now writes to CSV
import requests
import pandas as pd

# ── Config ──────────────────────────────────────────────────────────────────
# DB settings (DB_SERVER, DB_NAME, DB_DRIVER) are no longer needed; history and
# ticks are both CSV-based now. UPSTOX_API_KEY was imported but never used here.
# from config import DB_SERVER, DB_NAME, DB_DRIVER, TOKEN_FILE, UPSTOX_API_KEY
try:
    from config import TOKEN_FILE
except ImportError:
    TOKEN_FILE = "upstox_token.json"

# Strategy parameters live in backtest.py (single source of truth).
from backtest import (
    compute_signals,
    ENTRY, EXIT, STOP_LOSS, PROFIT_TARGET, MAX_HOLD,
)   # load_from_sql no longer used

# Sandbox paper-trading config (only used with --trade).
try:
    from config import SANDBOX_ACCESS_TOKEN, SANDBOX_BASE_URL
    _SANDBOX_CFG = bool(SANDBOX_ACCESS_TOKEN) and "YOUR_" not in str(SANDBOX_ACCESS_TOKEN)
except ImportError:
    SANDBOX_ACCESS_TOKEN = None
    SANDBOX_BASE_URL     = "https://api-sandbox.upstox.com/v2"
    _SANDBOX_CFG = False

try:
    from config import POINT_VALUE
except ImportError:
    POINT_VALUE = 100   # Rs per spread point (display only)

# ── Intraday EDGE filter (quality over quantity) ──────────────────────────────
# The plain strategy enters on EVERY |z|>=ENTRY cross and books shallow reversions
# (~4 pts) that barely clear the Rs80 charge -> heavy over-trading, thin/negative
# net. EDGE mode only enters when the room-to-revert (|spread - mean|) is >=
# EDGE_TARGET and books at +EDGE_TARGET, so each trade aims for a move several
# times the charge. Backtest on this week's feed: 3,700 -> ~528 trades, net win
# 61% -> ~100%, net +Rs46k -> +Rs183k/lot (target 16). Disable with --no-edge.
EDGE_FILTER      = True   # default ON
EDGE_TARGET      = 16     # pts: entry room filter + profit-book level (--target N)
EDGE_MAXHOLD_MIN = 30     # intraday time-stop (minutes)

BASE        = "https://api.upstox.com/v2"
SENSEX_KEY  = "BSE_INDEX|SENSEX"
NIFTY_KEY   = "NSE_INDEX|Nifty 50"
EOD_HH, EOD_MM = 15, 30            # auto-stop the feed at market close (IST)

# ── CSV storage ───────────────────────────────────────────────────────────────
SENSEX_CSV  = "sensex_data.csv"    # full SENSEX quote, one row per tick (primary path; set in main)
NIFTY_CSV   = "nifty_data.csv"     # full NIFTY  quote, one row per tick (primary path; set in main)
HISTORY_CSV = "history.csv"        # daily SENSEX+NIFTY closes for z-score warm-up

# Every tick is written to ALL paths in these lists (set in main()): by default the
# dated file PLUS the *_today.csv mirror; in --*-out manual mode, just the one path.
SENSEX_TARGETS = [SENSEX_CSV]
NIFTY_TARGETS  = [NIFTY_CSV]
MIRROR_SENSEX  = "sensex_today.csv"   # flat copy the rest of the pipeline reads
MIRROR_NIFTY   = "nifty_today.csv"

# Every field the Upstox full-quote feed returns per instrument, flattened.
# `raw_json` keeps the complete, untouched payload (including market depth) so
# no data the feed sends is ever lost.
QUOTE_COLUMNS = [
    "tick_time", "symbol", "last_price",
    "open", "high", "low", "prev_close", "net_change",
    "volume", "average_price", "oi",
    "total_buy_quantity", "total_sell_quantity",
    "lower_circuit_limit", "upper_circuit_limit",
    "oi_day_high", "oi_day_low",
    "last_trade_time", "upstox_timestamp", "instrument_token",
    "proxy_instrument", "proxy_last_price",
    "spread", "zscore", "signal",
    "raw_json",
]

# Cash indices have no volume/OI/depth, so each row is enriched with those
# fields from the nearest-expiry index FUTURE (a tradable instrument). The
# instrument master is downloaded once and cached so the contract auto-rolls.
INSTRUMENTS_URL   = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
INSTRUMENTS_CACHE = "instruments.json.gz"

# ── Sandbox paper-trading (--trade) ───────────────────────────────────────────
PAPER_TRADES_CSV = "paper_trades.csv"   # one row per closed trade
TRADE_COLUMNS = [
    "trade_date", "direction", "entry_time", "entry_sensex", "entry_nifty",
    "entry_spread", "entry_zscore", "exit_time", "exit_sensex", "exit_nifty",
    "exit_spread", "exit_zscore", "pnl_pts", "exit_reason", "status",
    "sensex_entry_order", "nifty_entry_order",
    "sensex_exit_order", "nifty_exit_order",
]
PRODUCT  = "I"     # intraday (squared off by EOD)
NUM_LOTS = 1       # lots per leg; quantity = lot_size * NUM_LOTS


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


# ── History (CSV) ─────────────────────────────────────────────────────────────

def download_history_csv(path: str, start: str = "2024-01-01") -> pd.DataFrame:
    """Download SENSEX + NIFTY daily closes from Yahoo Finance and cache to CSV.
    Used only when the history CSV does not exist yet — no database required."""
    import yfinance as yf

    end = str(date.today())
    print(f"  {path} not found — downloading history from Yahoo Finance "
          f"({start} to {end}) ...")

    sx_raw = yf.download("^BSESN", start=start, end=end, auto_adjust=True, progress=False)
    nf_raw = yf.download("^NSEI",  start=start, end=end, auto_adjust=True, progress=False)

    if isinstance(sx_raw.columns, pd.MultiIndex):
        sx_raw.columns = sx_raw.columns.get_level_values(0)
    if isinstance(nf_raw.columns, pd.MultiIndex):
        nf_raw.columns = nf_raw.columns.get_level_values(0)

    sx = sx_raw["Close"].rename("sensex_close")
    nf = nf_raw["Close"].rename("nifty_close")

    df = pd.concat([sx, nf], axis=1).dropna().reset_index()
    df.rename(columns={"Date": "date", "Datetime": "date"}, inplace=True)
    df["date"]         = pd.to_datetime(df["date"]).dt.date
    df["sensex_close"] = df["sensex_close"].round(2)
    df["nifty_close"]  = df["nifty_close"].round(2)
    df = df[["date", "sensex_close", "nifty_close"]]

    df.to_csv(path, index=False)
    print(f"  Saved {len(df)} rows to {path}")
    return df


def load_history(source: str = "csv") -> pd.DataFrame:
    """Load historical daily closes for z-score warm-up from a CSV file.
    Replaces the old load_from_sql() — no database needed.
    Columns expected: date, sensex_close, nifty_close
    `source` is accepted for backward-compat but ignored (always CSV)."""
    if not os.path.exists(HISTORY_CSV):
        df = download_history_csv(HISTORY_CSV)
    else:
        df = pd.read_csv(HISTORY_CSV)

    df["date"] = pd.to_datetime(df["date"])
    df = (df[["date", "sensex_close", "nifty_close"]]
          .dropna()
          .sort_values("date")
          .reset_index(drop=True))
    print(f"Loaded {len(df)} history rows from {HISTORY_CSV} "
          f"({df['date'].min().date()} to {df['date'].max().date()})")
    return df


# ── Futures proxy resolution ──────────────────────────────────────────────────

def resolve_index_futures():
    """Return {"SENSEX": {...}, "NIFTY": {...}} for the nearest non-expired
    monthly index futures, each with its instrument key, lot size and symbol.
    These tradable contracts supply the volume / OI / depth / circuit-limit
    fields the cash indices do not provide, and are the legs traded in --trade.

    The Upstox instrument master is cached locally and refreshed once a day,
    so the contract automatically rolls to the next month on expiry."""
    import gzip

    fresh = (os.path.exists(INSTRUMENTS_CACHE)
             and (time.time() - os.path.getmtime(INSTRUMENTS_CACHE)) < 86400)
    if not fresh:
        print("  Fetching Upstox instrument master (for futures proxy) ...")
        resp = requests.get(INSTRUMENTS_URL, timeout=120)
        resp.raise_for_status()
        with open(INSTRUMENTS_CACHE, "wb") as fh:
            fh.write(resp.content)

    with gzip.open(INSTRUMENTS_CACHE, "rb") as fh:
        data = json.load(fh)

    now_ms = int(time.time() * 1000)

    def nearest(segment, name):
        futs = [x for x in data
                if x.get("segment") == segment
                and x.get("instrument_type") == "FUT"
                and x.get("name") == name
                and (x.get("expiry") or 0) >= now_ms]
        futs.sort(key=lambda x: x.get("expiry") or 0)
        if not futs:
            return None
        f = futs[0]
        return {"key": f["instrument_key"],
                "lot": int(f.get("lot_size") or 0),
                "tsym": f.get("trading_symbol")}

    out = {}
    sx, nf = nearest("BSE_FO", "SENSEX"), nearest("NSE_FO", "NIFTY")
    if sx:
        out["SENSEX"] = sx
    if nf:
        out["NIFTY"] = nf
    return out


# ── Tick storage (CSV) ────────────────────────────────────────────────────────

def default_dated_paths():
    """Today's (sensex, nifty) CSV paths inside an auto-created dated folder:
    <YYYY_MM_DD>/sensex_<YYYY-MM-DD>.csv and .../nifty_<YYYY-MM-DD>.csv (relative to
    the current dir, i.e. feed_data/). Creates the folder if it does not exist."""
    d = date.today()
    folder = d.strftime("%Y_%m_%d")
    os.makedirs(folder, exist_ok=True)
    iso = d.isoformat()
    return (os.path.join(folder, f"sensex_{iso}.csv"),
            os.path.join(folder, f"nifty_{iso}.csv"))


def ensure_quote_csvs(paths):
    """Ensure both per-symbol CSVs exist with the full header. If an existing
    file has a mismatched header (e.g. an old 2-column recovery file) or holds a
    previous day's data, archive it and start a fresh one - so each file always
    contains a single day's complete, consistently-columned data."""
    header = ",".join(QUOTE_COLUMNS)
    today  = date.today().isoformat()
    for path in paths:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                first  = fh.readline().rstrip("\r\n")
                second = fh.readline().rstrip("\r\n")
            file_date = second.split(",", 1)[0][:10] if second else today
            if first != header or file_date != today:
                tag = file_date if len(file_date) == 10 and file_date[4] == "-" else "prev"
                bak, i = f"{path[:-4]}_{tag}_archived.csv", 1
                while os.path.exists(bak):
                    bak = f"{path[:-4]}_{tag}_archived_{i}.csv"; i += 1
                os.replace(path, bak)
                print(f"  archived stale {path} -> {bak}")
        if not os.path.exists(path):
            with open(path, "w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(QUOTE_COLUMNS)
    print(f"Quotes will be written to: {', '.join(paths)}")


def save_quote(path, symbol, index_q, proxy_q, tick_time, spread, zscore, signal):
    """Append one row for a single symbol.

    Price/OHLC/net_change come from the cash INDEX (this is what drives the
    z-score signal). Volume / OI / depth / circuit limits / buy-sell quantities
    come from the tradable FUTURES proxy, since the index does not provide them.
    `raw_json` keeps BOTH full payloads so nothing the feed sends is lost."""
    ohlc  = index_q.get("ohlc") or {}
    proxy = proxy_q or {}

    def iv(key, src):
        v = src.get(key)
        return v if v is not None else ""

    try:
        with open(path, "a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow([
                tick_time.strftime("%Y-%m-%d %H:%M:%S"),
                symbol,
                iv("last_price", index_q),
                iv("open", ohlc), iv("high", ohlc), iv("low", ohlc), iv("close", ohlc),
                iv("net_change", index_q),
                iv("volume", proxy), iv("average_price", proxy), iv("oi", proxy),
                iv("total_buy_quantity", proxy), iv("total_sell_quantity", proxy),
                iv("lower_circuit_limit", proxy), iv("upper_circuit_limit", proxy),
                iv("oi_day_high", proxy), iv("oi_day_low", proxy),
                iv("last_trade_time", index_q), iv("timestamp", index_q),
                iv("instrument_token", index_q),
                iv("instrument_token", proxy), iv("last_price", proxy),
                round(spread, 2) if spread is not None else "",
                round(zscore, 3) if zscore is not None else "",
                signal,
                json.dumps({"index": index_q, "proxy": proxy_q}, separators=(",", ":")),
            ])
    except Exception as e:
        print(f"\n  [CSV error] {e}")


# ── SQL Server (DISABLED — kept for reference) ─────────────────────────────────
# Re-enable by uncommenting this block, restoring the pyodbc / config imports at
# the top, and calling ensure_tick_table()/save_tick() against the DB instead.
#
# TICK_TABLE = "tick_data"
#
# def get_conn():
#     return pyodbc.connect(
#         f"DRIVER={{{DB_DRIVER}}};"
#         f"SERVER={DB_SERVER};"
#         f"DATABASE={DB_NAME};"
#         "Trusted_Connection=yes;"
#     )
#
#
# def ensure_tick_table():
#     conn   = get_conn()
#     cursor = conn.cursor()
#     cursor.execute(f"""
#         IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = '{TICK_TABLE}')
#         CREATE TABLE {TICK_TABLE} (
#             id           INT IDENTITY(1,1) PRIMARY KEY,
#             tick_time    DATETIME       NOT NULL,
#             sensex_ltp   DECIMAL(12,2)  NOT NULL,
#             nifty_ltp    DECIMAL(12,2)  NOT NULL,
#             spread       DECIMAL(12,2),
#             zscore       DECIMAL(8,3),
#             signal       VARCHAR(40)
#         )
#     """)
#     conn.commit()
#     conn.close()
#     print(f"[{TICK_TABLE}] table ready in SQL Server.")
#
#
# def save_tick(tick_time, sensex, nifty, spread, zscore, signal):
#     try:
#         conn   = get_conn()
#         cursor = conn.cursor()
#         cursor.execute(
#             f"INSERT INTO {TICK_TABLE} "
#             "(tick_time, sensex_ltp, nifty_ltp, spread, zscore, signal) "
#             "VALUES (?, ?, ?, ?, ?, ?)",
#             (
#                 tick_time,
#                 round(sensex, 2),
#                 round(nifty,  2),
#                 round(spread, 2) if spread is not None else None,
#                 round(zscore, 3) if zscore is not None else None,
#                 signal,
#             ),
#         )
#         conn.commit()
#         conn.close()
#     except Exception as e:
#         print(f"\n  [DB error] {e}")


# ── Sandbox paper-trading ─────────────────────────────────────────────────────

def ensure_paper_csv():
    if not os.path.exists(PAPER_TRADES_CSV):
        with open(PAPER_TRADES_CSV, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(TRADE_COLUMNS)
    print(f"[{PAPER_TRADES_CSV}] ready - closed trades will be appended here.")


def sandbox_order(instrument, side, quantity, tag="snx_nf_pair"):
    """Place one MARKET order on the Upstox sandbox. Returns order_id, or
    'SIM' when no sandbox token is configured, or 'ERR:..' on failure.
    P&L is tracked from the index spread regardless of order outcome."""
    if not _SANDBOX_CFG:
        return "SIM"
    try:
        r = requests.post(
            f"{SANDBOX_BASE_URL}/order/place",
            headers={
                "Authorization": f"Bearer {SANDBOX_ACCESS_TOKEN}",
                "Accept":        "application/json",
                "Content-Type":  "application/json",
            },
            json={
                "quantity": quantity, "product": PRODUCT, "validity": "DAY",
                "price": 0.0, "tag": tag, "instrument_token": instrument,
                "order_type": "MARKET", "transaction_type": side,
                "disclosed_quantity": 0, "trigger_price": 0.0, "is_amo": False,
            },
            timeout=8,
        )
        if r.ok:
            return str(r.json().get("data", {}).get("order_id", "placed"))
        return f"ERR:{r.status_code}"
    except Exception as e:
        return f"ERR:{str(e)[:40]}"


class PaperTrader:
    """Live sandbox paper-trader for the SENSEX/NIFTY spread strategy.

    EDGE mode (default): enter z<=-ENTRY (LONG) / z>=+ENTRY (SHORT) ONLY when the
    room-to-revert (|spread - mean|) >= target, so each trade aims for a move
    several times the Rs80 charge. Exit at +target / -STOP_LOSS / time-stop / EOD.
    --no-edge restores the old behavior (enter every cross, exit shallow reversion
    at |z|<=EXIT / +PROFIT_TARGET). P&L in spread points (LONG: spread-entry).
    """

    def __init__(self, futs, num_lots, do_orders, save_csv,
                 max_loss=None, max_trades=None,
                 edge=True, target=EDGE_TARGET, maxhold_min=EDGE_MAXHOLD_MIN):
        self._futs    = futs            # {"SENSEX": {"key","lot"}, "NIFTY": {...}}
        self._lots    = num_lots
        self._orders  = do_orders
        self._save    = save_csv
        self._trade   = None
        self._n       = 0
        self._wins    = 0
        self._pnl     = 0.0
        # EDGE filter (quality over quantity)
        self._edge        = edge
        self._target      = target
        self._maxhold_min = maxhold_min
        # Risk controls (minimise losses; they cannot eliminate them).
        self._max_loss   = max_loss      # halt new trades once session P&L <= -max_loss
        self._max_trades = max_trades    # cap trades per day
        self._halted     = False

    def _qty(self, symbol):
        return self._futs.get(symbol, {}).get("lot", 0) * self._lots

    def _key(self, symbol):
        return self._futs.get(symbol, {}).get("key")

    # ── per-tick state machine ───────────────────────────────────────────────
    def on_tick(self, sensex, nifty, spread, zscore, now, sma=None):
        if spread is None or zscore is None:
            return "WARMING UP", None
        room = abs(spread - sma) if (sma is not None and not pd.isna(sma)) else None

        if self._trade:
            pnl = self._cur_pnl(spread)
            d   = self._trade["direction"]
            if pnl <= -STOP_LOSS:
                return self._close("STOP_LOSS", sensex, nifty, spread, zscore, now), pnl
            if self._edge:
                # EDGE exits: book the target move, else time-stop, else hold.
                if pnl >= self._target:
                    return self._close("PROFIT_TARGET", sensex, nifty, spread, zscore, now), pnl
                held_min = (now - self._trade["entry_time"]).total_seconds() / 60.0
                if held_min >= self._maxhold_min:
                    return self._close("MAX_HOLD", sensex, nifty, spread, zscore, now), pnl
                return f"{d} OPEN", pnl
            # legacy exits: profit target + shallow mean-reversion + day-based max-hold
            if pnl >= PROFIT_TARGET:
                return self._close("PROFIT_TARGET", sensex, nifty, spread, zscore, now), pnl
            if d == "LONG"  and zscore >= -EXIT:
                return self._close("REVERTED", sensex, nifty, spread, zscore, now), pnl
            if d == "SHORT" and zscore <=  EXIT:
                return self._close("REVERTED", sensex, nifty, spread, zscore, now), pnl
            if (now - self._trade["entry_time"]).days >= MAX_HOLD:
                return self._close("MAX_HOLD", sensex, nifty, spread, zscore, now), pnl
            return f"{d} OPEN", pnl

        # flat -> look for entry (blocked if a risk limit has halted trading)
        if self._halted:
            return "HALTED (risk limit)", None
        # EDGE entry gate: skip shallow setups whose reversion won't clear the charge
        if self._edge and room is not None and room < self._target:
            return f"HOLD - room {room:.0f} < {self._target:.0f}", None
        if zscore <= -ENTRY:
            self._open("LONG", sensex, nifty, spread, zscore, now)
            return "LONG ENTERED ***", 0.0
        if zscore >= ENTRY:
            self._open("SHORT", sensex, nifty, spread, zscore, now)
            return "SHORT ENTERED ***", 0.0
        return "HOLD - no signal", None

    def _cur_pnl(self, spread):
        if not self._trade:
            return 0.0
        diff = spread - self._trade["entry_spread"]
        return diff if self._trade["direction"] == "LONG" else -diff

    # ── open / close ─────────────────────────────────────────────────────────
    def _open(self, direction, sensex, nifty, spread, zscore, now):
        self._trade = dict(direction=direction, entry_time=now,
                           entry_sensex=sensex, entry_nifty=nifty,
                           entry_spread=spread, entry_zscore=zscore,
                           sensex_entry_order="", nifty_entry_order="")
        if self._orders:
            s_side = "BUY"  if direction == "LONG" else "SELL"
            n_side = "SELL" if direction == "LONG" else "BUY"
            self._trade["sensex_entry_order"] = sandbox_order(self._key("SENSEX"), s_side, self._qty("SENSEX"))
            self._trade["nifty_entry_order"]  = sandbox_order(self._key("NIFTY"),  n_side, self._qty("NIFTY"))
        print(f"\n>>> [{now.strftime('%H:%M:%S')}] {direction} SPREAD ENTERED  "
              f"Sensex {sensex:,.2f} | Nifty {nifty:,.2f} | spread {spread:+.2f} | Z {zscore:+.3f}"
              f"  orders[snx={self._trade['sensex_entry_order']}, nf={self._trade['nifty_entry_order']}]")

    def _close(self, reason, sensex, nifty, spread, zscore, now):
        t   = self._trade
        pnl = self._cur_pnl(spread)
        sx_exit_oid = nf_exit_oid = ""
        if self._orders:
            s_side = "SELL" if t["direction"] == "LONG" else "BUY"
            n_side = "BUY"  if t["direction"] == "LONG" else "SELL"
            sx_exit_oid = sandbox_order(self._key("SENSEX"), s_side, self._qty("SENSEX"))
            nf_exit_oid = sandbox_order(self._key("NIFTY"),  n_side, self._qty("NIFTY"))

        self._n += 1
        self._pnl += pnl
        if pnl >= 0:
            self._wins += 1

        if self._save:
            with open(PAPER_TRADES_CSV, "a", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow([
                    now.strftime("%Y-%m-%d"), t["direction"],
                    t["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),
                    round(t["entry_sensex"], 2), round(t["entry_nifty"], 2),
                    round(t["entry_spread"], 2), round(t["entry_zscore"], 3),
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    round(sensex, 2), round(nifty, 2),
                    round(spread, 2), round(zscore, 3),
                    round(pnl, 2), reason, "CLOSED",
                    t["sensex_entry_order"], t["nifty_entry_order"],
                    sx_exit_oid, nf_exit_oid,
                ])

        tag = "WIN" if pnl >= 0 else "LOSS"
        print(f"\n<<< [{now.strftime('%H:%M:%S')}] {t['direction']} CLOSED  reason={reason} [{tag}]  "
              f"P&L {pnl:+.2f} pts (~Rs{pnl * POINT_VALUE:+,.0f})  "
              f"session {self._n} trades, {self._wins} wins, {self._pnl:+.2f} pts")
        self._trade = None

        # Risk halts — stop opening new trades for the rest of the day
        if self._max_loss is not None and self._pnl <= -self._max_loss:
            self._halted = True
            print(f"  ** DAILY LOSS LIMIT hit ({self._pnl:+.2f} <= -{self._max_loss}) - "
                  f"no new trades today **")
        elif self._max_trades is not None and self._n >= self._max_trades:
            self._halted = True
            print(f"  ** MAX TRADES reached ({self._n}) - no new trades today **")
        return f"{reason} -> {tag}"

    def square_off_eod(self, sensex, nifty, spread, zscore, now):
        if self._trade and spread is not None:
            self._close("EOD", sensex, nifty, spread, zscore, now)

    def session_line(self):
        wr = f"{self._wins}/{self._n}" if self._n else "0/0"
        return (f"Paper session: {self._n} trades  W/L {wr}  "
                f"P&L {self._pnl:+.2f} pts (~Rs{self._pnl * POINT_VALUE:+,.0f})")


# ── Upstox full-quote fetch ───────────────────────────────────────────────────

def fetch_quotes(token: str, instrument_keys: list) -> dict:
    """Fetch full quotes for several instruments in one call.
    Returns {instrument_token: quote_dict}. Each quote carries every field the
    feed provides — ohlc, volume, oi, average price, circuit limits, depth,
    timestamps, etc. (volume/oi/depth are null for cash indices)."""
    from urllib.parse import quote
    keys = ",".join(instrument_keys)
    url  = f"{BASE}/market-quote/quotes?instrument_key={quote(keys, safe=',')}"
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=5,
    )
    r.raise_for_status()
    data = r.json().get("data", {})

    # Upstox returns keys with ':' instead of '|' in the response; the
    # instrument_token field inside each entry is the canonical '|' key.
    out = {}
    for v in data.values():
        it = v.get("instrument_token")
        if it:
            out[it] = v
    return out


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
    sma    = last["spread_ma"]

    if pd.isna(z):
        return {"spread": None, "zscore": None, "ratio": None, "sma": None, "signal": "WARMING UP"}

    if   z <= -ENTRY: signal = "BUY  SENSEX  +  SELL NIFTY"
    elif z >=  ENTRY: signal = "SELL SENSEX  +  BUY  NIFTY"
    elif abs(z) <= 0.3: signal = "EXIT / HOLD"
    else:             signal = "HOLD"

    return {
        "spread": round(spread, 2),
        "zscore": round(z, 3),
        "ratio":  round(ratio, 4),
        "sma":    round(sma, 2),
        "signal": signal,
    }


def compute_intraday(buf: list, sensex: float, nifty: float) -> dict:
    """Intraday signal — z-score over the DAY'S OWN recent ticks (rolling
    RATIO_LOOKBACK / LOOKBACK in ticks ~= seconds), matching the per-second
    backtest. Unlike compute_live (z vs daily history, which sits ~1.5), this
    crosses +/-ENTRY intraday so trades actually fire. Needs ~RATIO_LOOKBACK+
    LOOKBACK ticks of warm-up before z is valid."""
    buf.append({"date": pd.Timestamp(datetime.now()),
                "sensex_close": sensex, "nifty_close": nifty})
    if len(buf) > 400:               # keep only the recent window
        del buf[:len(buf) - 400]

    last = compute_signals(pd.DataFrame(buf)).iloc[-1]
    z, spread, ratio, sma = last["zscore"], last["spread"], last["ratio"], last["spread_ma"]
    if pd.isna(z):
        return {"spread": None, "zscore": None, "ratio": None, "sma": None, "signal": "WARMING UP"}

    if   z <= -ENTRY: signal = "BUY  SENSEX  +  SELL NIFTY"
    elif z >=  ENTRY: signal = "SELL SENSEX  +  BUY  NIFTY"
    elif abs(z) <= 0.3: signal = "EXIT / HOLD"
    else:             signal = "HOLD"
    return {"spread": round(spread, 2), "zscore": round(z, 3),
            "ratio": round(ratio, 4), "sma": round(sma, 2), "signal": signal}


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    global SENSEX_CSV, NIFTY_CSV, SENSEX_TARGETS, NIFTY_TARGETS
    source   = "csv"
    interval = 1
    save_csv = "--no-save" not in sys.argv
    intraday = "--intraday" in sys.argv   # z over the day's own ticks (per-second backtest style)
    manual_out = ("--sensex-out" in sys.argv) or ("--nifty-out" in sys.argv)
    if manual_out:
        # manual mode: write exactly where told (no dated folder, no today-mirror)
        if "--sensex-out" in sys.argv:
            SENSEX_CSV = sys.argv[sys.argv.index("--sensex-out") + 1]
        if "--nifty-out" in sys.argv:
            NIFTY_CSV = sys.argv[sys.argv.index("--nifty-out") + 1]
        SENSEX_TARGETS = [SENSEX_CSV]
        NIFTY_TARGETS  = [NIFTY_CSV]
    else:
        # default: auto-created dated folder + dated filenames, mirrored to the
        # *_today.csv files the rest of the pipeline reads
        SENSEX_CSV, NIFTY_CSV = default_dated_paths()
        SENSEX_TARGETS = [SENSEX_CSV, MIRROR_SENSEX]
        NIFTY_TARGETS  = [NIFTY_CSV, MIRROR_NIFTY]

    if "--source" in sys.argv:          # accepted for backward-compat, ignored
        idx    = sys.argv.index("--source")
        source = sys.argv[idx + 1]
    if "--interval" in sys.argv:
        idx      = sys.argv.index("--interval")
        interval = int(sys.argv[idx + 1])

    trade_enabled = "--trade" in sys.argv
    num_lots      = NUM_LOTS
    if "--lots" in sys.argv:
        num_lots = int(sys.argv[sys.argv.index("--lots") + 1])
    max_loss   = float(sys.argv[sys.argv.index("--max-loss") + 1])   if "--max-loss"   in sys.argv else None
    max_trades = int(sys.argv[sys.argv.index("--max-trades") + 1])   if "--max-trades" in sys.argv else None
    edge_filter = "--no-edge" not in sys.argv                          # quality-over-quantity entry filter
    edge_target = float(sys.argv[sys.argv.index("--target") + 1]) if "--target" in sys.argv else EDGE_TARGET

    print("Loading history from history.csv ...")
    df_history = load_history(source)

    token = get_token()
    print(f"  Token valid for today ({date.today()})")

    print("Resolving near-month index futures (volume/OI/depth + trade legs) ...")
    futs = {}
    try:
        futs = resolve_index_futures()
        for sym, info in futs.items():
            print(f"  {sym} future: {info['key']}  ({info['tsym']}, lot {info['lot']})")
    except Exception as e:
        print(f"  [warn] could not resolve futures ({e}) - liquidity fields/trading disabled")

    sensex_fut = futs.get("SENSEX", {}).get("key")
    nifty_fut  = futs.get("NIFTY",  {}).get("key")
    poll_keys  = [SENSEX_KEY, NIFTY_KEY] + [k for k in (sensex_fut, nifty_fut) if k]

    if save_csv:
        ensure_quote_csvs(SENSEX_TARGETS + NIFTY_TARGETS)

    trader = None
    if trade_enabled:
        if sensex_fut and nifty_fut:
            trader = PaperTrader(futs, num_lots, do_orders=True, save_csv=save_csv,
                                 max_loss=max_loss, max_trades=max_trades,
                                 edge=edge_filter, target=edge_target, maxhold_min=EDGE_MAXHOLD_MIN)
            if save_csv:
                ensure_paper_csv()
            mode = "REAL sandbox orders" if _SANDBOX_CFG else "SIMULATED (no sandbox token)"
            risk = []
            if max_loss   is not None: risk.append(f"daily loss limit -{max_loss} pts")
            if max_trades is not None: risk.append(f"max {max_trades} trades")
            risk_str = " | risk: " + ", ".join(risk) if risk else " | risk: none (use --max-loss/--max-trades)"
            print(f"Paper trading: ON  | legs {futs['SENSEX']['tsym']} & {futs['NIFTY']['tsym']} "
                  f"| {num_lots} lot(s)/leg | {mode}{risk_str}")
            if edge_filter:
                print(f"  EDGE filter: ON  | enter only when room-to-revert >= {edge_target:.0f} pts; "
                      f"book +{edge_target:.0f} / stop -{STOP_LOSS} / {EDGE_MAXHOLD_MIN}min / EOD  "
                      f"(quality over quantity)")
            else:
                print("  EDGE filter: OFF (--no-edge) -> enters every z-cross, exits shallow reversion")
        else:
            print("Paper trading: requested but futures unavailable - DISABLED")

    print(f"\nPolling {interval}s | Entry +/-{ENTRY} Exit +/-{EXIT} "
          f"SL -{STOP_LOSS} TP +{PROFIT_TARGET} pts | EOD {EOD_HH:02d}:{EOD_MM:02d} IST")
    if save_csv:
        print(f"Saving quotes -> {', '.join(SENSEX_TARGETS + NIFTY_TARGETS)}")
    else:
        print("Saving quotes: NO (--no-save)")
    print(f"Press Ctrl+C to stop.\n")
    print(f"{'TIME':<10} {'SENSEX':>12} {'NIFTY':>11} {'RATIO':>8} "
          f"{'SPREAD':>9} {'Z':>7}  {'SIGNAL':<28} {'TRADE':<18} {'P&L':>10}")
    print("-" * 120)

    tick_count = 0
    last_sx = last_nf = last_sp = last_z = last_now = None
    intraday_buf = []
    if intraday:
        print("Signal mode: INTRADAY (z over the day's own ticks, per-second backtest style)")

    try:
        while True:
            now_check = datetime.now()
            if (now_check.hour, now_check.minute) >= (EOD_HH, EOD_MM):
                if trader:
                    trader.square_off_eod(last_sx, last_nf, last_sp, last_z, now_check)
                print(f"\n\nMarket close {EOD_HH:02d}:{EOD_MM:02d} IST reached "
                      f"after {tick_count} ticks. Stopping feed.")
                if trader:
                    print(trader.session_line())
                print(f"Data saved to {SENSEX_CSV} and {NIFTY_CSV}")
                break
            loop_start = time.time()
            try:
                quotes = fetch_quotes(token, poll_keys)
                sx_idx = quotes.get(SENSEX_KEY)
                nf_idx = quotes.get(NIFTY_KEY)
                if sx_idx is None or nf_idx is None:
                    raise ValueError(f"index quote missing: {list(quotes.keys())}")
                sensex = float(sx_idx["last_price"])
                nifty  = float(nf_idx["last_price"])
                sx_fut = quotes.get(sensex_fut) if sensex_fut else None
                nf_fut = quotes.get(nifty_fut)  if nifty_fut  else None

                result        = (compute_intraday(intraday_buf, sensex, nifty)
                                  if intraday else compute_live(df_history, sensex, nifty))
                now           = datetime.now()
                tick_count   += 1
                last_sx, last_nf       = sensex, nifty
                last_sp, last_z, last_now = result["spread"], result["zscore"], now

                # Run the paper-trader on this tick (entry/exit/orders/logging)
                tstatus, tpnl = "", None
                if trader:
                    tstatus, tpnl = trader.on_tick(
                        sensex, nifty, result["spread"], result["zscore"], now, result.get("sma"))
                pnl_str = f"{tpnl:+.2f}pts" if tpnl is not None else "---"

                print(
                    f"\r{now.strftime('%H:%M:%S'):<10} "
                    f"{sensex:>12,.2f} "
                    f"{nifty:>11,.2f} "
                    f"{result['ratio'] or '':>8} "
                    f"{result['spread'] or '':>9} "
                    f"{result['zscore'] or '':>7}  "
                    f"{result['signal']:<28} {tstatus:<18} {pnl_str:>10}  #{tick_count}",
                    end="", flush=True,
                )

                if save_csv:
                    for p in SENSEX_TARGETS:
                        save_quote(p, "SENSEX", sx_idx, sx_fut, now,
                                   result["spread"], result["zscore"], result["signal"])
                    for p in NIFTY_TARGETS:
                        save_quote(p, "NIFTY", nf_idx, nf_fut, now,
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
        if trader and last_now is not None:
            trader.square_off_eod(last_sx, last_nf, last_sp, last_z, last_now)
        print(f"\n\nStopped. {tick_count} ticks collected.")
        if trader:
            print(trader.session_line())
        if save_csv:
            print(f"All saved to {SENSEX_CSV} and {NIFTY_CSV}")
            print(f"Peek:  python -c \"import pandas as pd; "
                  f"print(pd.read_csv('{SENSEX_CSV}').tail(10))\"")


if __name__ == "__main__":
    main()
