"""
live_trader.py — Tick feed + automated paper trading running simultaneously.

Every second:
  1. Fetches SENSEX + NIFTY LTP from Upstox REST API
  2. Computes rolling ratio, spread, z-score
  3. Fires entry/exit orders on Upstox Sandbox (no real money)
  4. Saves every tick to SQL  tick_data  table
  5. Saves every trade to SQL  paper_trades  table
  6. Prints a live dashboard in the terminal

Usage:
    python live_trader.py                       (history from yfinance_feed)
    python live_trader.py --source upstox       (history from upstox_feed)
    python live_trader.py --interval 2          (poll every 2 seconds)
    python live_trader.py --no-save             (skip all SQL writes)
    python live_trader.py --interval 2          (poll every 2 seconds)

Market hours: 09:15 - 15:30 IST
Pre-req: python live_fetch.py  to get today's market data token.
"""

import json
import os
import sys
import time
from datetime import date, datetime
from urllib.parse import quote

import pyodbc
import requests
import pandas as pd

# ── Config ─────────────────────────────────────────────────────────────────────

from config import DB_SERVER, DB_NAME, DB_DRIVER, TOKEN_FILE

try:
    from config import SANDBOX_ACCESS_TOKEN, SANDBOX_BASE_URL
    _SANDBOX_CFG = True
except ImportError:
    SANDBOX_ACCESS_TOKEN = None
    SANDBOX_BASE_URL     = "https://sandbox.upstox.com/v2"
    _SANDBOX_CFG = False

try:
    from config import SENSEX_TRADE_INSTRUMENT, NIFTY_TRADE_INSTRUMENT, TRADE_QUANTITY
except ImportError:
    SENSEX_TRADE_INSTRUMENT = "BSE_EQ|INF200K01VU8"
    NIFTY_TRADE_INSTRUMENT  = "NSE_EQ|INF204KB15I2"
    TRADE_QUANTITY          = 1

try:
    from config import POINT_VALUE
except ImportError:
    POINT_VALUE = 100  # ₹ per spread point (display only)

from backtest import load_from_sql, compute_signals, ENTRY, EXIT, STOP_LOSS, MAX_HOLD, PROFIT_TARGET
from raw_feed import ensure_raw_table, save_raw

LIVE_BASE   = "https://api.upstox.com/v2"
SENSEX_KEY  = "BSE_INDEX|SENSEX"
NIFTY_KEY   = "NSE_INDEX|Nifty 50"
TICK_TABLE  = "tick_data"
TRADE_TABLE = "paper_trade"
EOD_HH, EOD_MM = 15, 29     # square-off all positions at this time


# ── Database ──────────────────────────────────────────────────────────────────

def get_conn():
    return pyodbc.connect(
        f"DRIVER={{{DB_DRIVER}}};SERVER={DB_SERVER};"
        f"DATABASE={DB_NAME};Trusted_Connection=yes;"
    )


def ensure_tables():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(f"""
        IF NOT EXISTS (SELECT * FROM sys.tables WHERE name='{TICK_TABLE}')
        CREATE TABLE {TICK_TABLE} (
            id         INT IDENTITY(1,1) PRIMARY KEY,
            tick_time  DATETIME      NOT NULL,
            sensex_ltp DECIMAL(12,2) NOT NULL,
            nifty_ltp  DECIMAL(12,2) NOT NULL,
            spread     DECIMAL(12,2),
            zscore     DECIMAL(8,3),
            signal     VARCHAR(50)
        )
    """)
    cur.execute(f"""
        IF NOT EXISTS (SELECT * FROM sys.tables WHERE name='{TRADE_TABLE}')
        CREATE TABLE {TRADE_TABLE} (
            id               INT IDENTITY(1,1) PRIMARY KEY,
            trade_date       DATE           NOT NULL,
            direction        VARCHAR(10)    NOT NULL,
            entry_time       DATETIME       NOT NULL,
            entry_sensex     DECIMAL(12,2)  NOT NULL,
            entry_nifty      DECIMAL(12,2)  NOT NULL,
            entry_spread     DECIMAL(12,2)  NOT NULL,
            entry_ratio      DECIMAL(8,4)   NOT NULL,
            entry_zscore     DECIMAL(8,3)   NOT NULL,
            exit_time        DATETIME,
            exit_sensex      DECIMAL(12,2),
            exit_nifty       DECIMAL(12,2),
            exit_spread      DECIMAL(12,2),
            exit_zscore      DECIMAL(8,3),
            pnl_pts          DECIMAL(10,2),
            exit_reason      VARCHAR(20),
            status           VARCHAR(10)    NOT NULL DEFAULT 'OPEN',
            sensex_order_id  VARCHAR(100),
            nifty_order_id   VARCHAR(100)
        )
    """)
    conn.commit()
    conn.close()
    ensure_raw_table()
    print(f"SQL tables ready: [{TICK_TABLE}]  [{TRADE_TABLE}]  [raw_data]")


def save_tick(tick_time, sx, nf, sp, z, signal):
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute(
            f"INSERT INTO {TICK_TABLE} "
            "(tick_time,sensex_ltp,nifty_ltp,spread,zscore,signal) "
            "VALUES (?,?,?,?,?,?)",
            tick_time,
            round(sx, 2), round(nf, 2),
            round(sp, 2) if sp is not None else None,
            round(z,  3) if z  is not None else None,
            signal,
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── Market data ────────────────────────────────────────────────────────────────

def get_live_token():
    if not os.path.exists(TOKEN_FILE):
        raise FileNotFoundError(
            "upstox_token.json not found. Run:  python live_fetch.py"
        )
    with open(TOKEN_FILE) as fh:
        d = json.load(fh)
    if d.get("date") != str(date.today()):
        raise ValueError(
            f"Token is from {d.get('date')} (expired). Run:  python live_fetch.py"
        )
    return d["access_token"]


def fetch_full_quote(token):
    """
    Calls Upstox full-quote endpoint. Returns:
      (sensex_ltp, nifty_ltp, sensex_quote_dict, nifty_quote_dict)
    quote dicts contain all raw fields for saving to raw_data.
    """
    keys = f"{SENSEX_KEY},{NIFTY_KEY}"
    url  = f"{LIVE_BASE}/market-quote/quotes?instrument_key={quote(keys, safe=',')}"
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=5,
    )
    r.raise_for_status()
    data = r.json().get("data", {})

    sx_quote, nf_quote = None, None
    for k, v in data.items():
        if "SENSEX" in k: sx_quote = v
        if "Nifty"  in k or "NIFTY" in k: nf_quote = v

    if sx_quote is None or nf_quote is None:
        raise ValueError(f"Could not parse quote response: {list(data.keys())}")

    return (
        float(sx_quote["last_price"]),
        float(nf_quote["last_price"]),
        sx_quote,
        nf_quote,
    )


def compute_live(df_hist, sensex, nifty):
    """Appends current tick to history, returns spread/zscore/ratio."""
    today = pd.DataFrame([{
        "date":         pd.Timestamp(datetime.now()),
        "sensex_close": sensex,
        "nifty_close":  nifty,
    }])
    df  = pd.concat([df_hist, today], ignore_index=True)
    sig = compute_signals(df).iloc[-1]
    z   = sig["zscore"]
    sp  = sig["spread"]
    ra  = sig["ratio"]
    if pd.isna(z):
        return None, None, None
    return round(sp, 2), round(z, 3), round(ra, 4)


# ── Sandbox order placement ────────────────────────────────────────────────────

def sandbox_order(instrument, side):
    """Place one paper order on Upstox sandbox. Returns order_id or error string."""
    try:
        r = requests.post(
            f"{SANDBOX_BASE_URL}/order/place",
            headers={
                "Authorization": f"Bearer {SANDBOX_ACCESS_TOKEN}",
                "Accept":        "application/json",
                "Content-Type":  "application/json",
            },
            json={
                "quantity":           TRADE_QUANTITY,
                "product":            "I",
                "validity":           "DAY",
                "price":              0.0,
                "tag":                "paper_snx_nf",
                "instrument_token":   instrument,
                "order_type":         "MARKET",
                "transaction_type":   side,
                "disclosed_quantity": 0,
                "trigger_price":      0.0,
                "is_amo":             False,
            },
            timeout=5,
        )
        if r.ok:
            oid = r.json().get("data", {}).get("order_id", "placed")
            return str(oid)
        return f"ERR:{r.status_code}"
    except Exception as e:
        return f"ERR:{str(e)[:40]}"


# ── Paper Trader ───────────────────────────────────────────────────────────────

class PaperTrader:
    """
    State machine:
        IDLE  --(entry signal)-->  OPEN
        OPEN  --(exit signal / stop loss / eod)-->  CLOSED  --> IDLE
    """

    def __init__(self, save_db, place_orders):
        self._save        = save_db
        self._orders      = place_orders and _SANDBOX_CFG
        self._trade       = None   # dict when a position is open
        self._session_pnl = 0.0
        self._n_trades    = 0
        self._n_wins      = 0
        self._load_open_trade()

    # ── Startup: resume any OPEN trade from previous session ─────────────────

    def _load_open_trade(self):
        try:
            conn = get_conn()
            cur  = conn.cursor()
            cur.execute(f"""
                SELECT TOP 1 id, direction, entry_time,
                       entry_sensex, entry_nifty,
                       entry_spread, entry_ratio, entry_zscore
                FROM {TRADE_TABLE}
                WHERE status = 'OPEN'
                ORDER BY entry_time DESC
            """)
            row = cur.fetchone()
            conn.close()
            if row:
                self._trade = dict(
                    id           = row[0],
                    direction    = row[1],
                    entry_time   = row[2],
                    entry_sensex = float(row[3]),
                    entry_nifty  = float(row[4]),
                    entry_spread = float(row[5]),
                    entry_ratio  = float(row[6]),
                    entry_zscore = float(row[7]),
                )
                print(f"\n>>> Resuming open {row[1]} trade from {row[2]}")
        except Exception as e:
            print(f"  [DB] load_open_trade: {e}")

    # ── Called every tick ────────────────────────────────────────────────────

    def on_tick(self, sensex, nifty, spread, zscore, ratio, now):
        """
        Returns (status_string, live_pnl_pts).
        live_pnl_pts is None when there is no open trade.
        """
        if spread is None:
            return "WARMING UP", None

        # End-of-day square-off
        if now.hour == EOD_HH and now.minute >= EOD_MM:
            if self._trade:
                self._close("EOD", sensex, nifty, spread, zscore, now)
            return "EOD — MARKET CLOSED", None

        if self._trade:
            pnl = self._cur_pnl(spread)
            d   = self._trade["direction"]

            # Stop loss
            if pnl <= -STOP_LOSS:
                self._close("STOP_LOSS", sensex, nifty, spread, zscore, now)
                return "STOP LOSS HIT", pnl

            # Profit target
            if pnl >= PROFIT_TARGET:
                self._close("PROFIT_TARGET", sensex, nifty, spread, zscore, now)
                return "PROFIT TARGET HIT ***", pnl

            # Max hold
            age_days = (now - self._trade["entry_time"]).days
            if age_days >= MAX_HOLD:
                self._close("MAX_HOLD", sensex, nifty, spread, zscore, now)
                return "MAX HOLD EXIT", pnl

            # Z-score exit
            if d == "LONG"  and zscore >= EXIT:
                self._close("SIGNAL", sensex, nifty, spread, zscore, now)
                return "EXIT LONG", pnl
            if d == "SHORT" and zscore <= -EXIT:
                self._close("SIGNAL", sensex, nifty, spread, zscore, now)
                return "EXIT SHORT", pnl

            # Reversal (z flips to opposite entry side)
            if d == "LONG" and zscore >= ENTRY:
                self._close("REVERSAL", sensex, nifty, spread, zscore, now)
                self._open("SHORT", sensex, nifty, spread, zscore, ratio, now)
                return "REVERSAL -> SHORT", self._cur_pnl(spread)
            if d == "SHORT" and zscore <= -ENTRY:
                self._close("REVERSAL", sensex, nifty, spread, zscore, now)
                self._open("LONG", sensex, nifty, spread, zscore, ratio, now)
                return "REVERSAL -> LONG", self._cur_pnl(spread)

            return f"{d} SPREAD OPEN", pnl

        else:
            # No position — watch for entry
            if zscore <= -ENTRY:
                self._open("LONG", sensex, nifty, spread, zscore, ratio, now)
                return "LONG SPREAD ENTERED ***", 0.0
            if zscore >= ENTRY:
                self._open("SHORT", sensex, nifty, spread, zscore, ratio, now)
                return "SHORT SPREAD ENTERED ***", 0.0
            return "HOLD — no signal", None

    # ── Open / Close position ─────────────────────────────────────────────────

    def _open(self, direction, sensex, nifty, spread, zscore, ratio, now):
        self._trade = dict(
            direction    = direction,
            entry_time   = now,
            entry_sensex = sensex,
            entry_nifty  = nifty,
            entry_spread = spread,
            entry_ratio  = ratio,
            entry_zscore = zscore,
        )

        # Sandbox orders
        s_side = "BUY"  if direction == "LONG" else "SELL"
        n_side = "SELL" if direction == "LONG" else "BUY"
        if self._orders:
            self._trade["sensex_order_id"] = sandbox_order(SENSEX_TRADE_INSTRUMENT, s_side)
            self._trade["nifty_order_id"]  = sandbox_order(NIFTY_TRADE_INSTRUMENT, n_side)

        # SQL insert
        if self._save:
            self._trade["id"] = self._insert()

        sl_level = spread - STOP_LOSS if direction == "LONG" else spread + STOP_LOSS
        print(
            f"\n\n"
            f">>> [{now.strftime('%H:%M:%S')}] {direction} SPREAD ENTERED\n"
            f"    Sensex {sensex:>12,.2f}  |  Nifty {nifty:>10,.2f}  |  Ratio {ratio:.4f}\n"
            f"    Entry spread: {spread:+.2f}  |  Entry Z: {zscore:+.3f}\n"
            f"    Stop loss at spread: {sl_level:+.2f}  |  "
            f"Exit target: Z {'>' if direction=='LONG' else '<'} {EXIT}\n"
        )

    def _close(self, reason, sensex, nifty, spread, zscore, now):
        if not self._trade:
            return
        pnl = self._cur_pnl(spread)

        # Closing sandbox orders
        s_side = "SELL" if self._trade["direction"] == "LONG" else "BUY"
        n_side = "BUY"  if self._trade["direction"] == "LONG" else "SELL"
        if self._orders:
            sandbox_order(SENSEX_TRADE_INSTRUMENT, s_side)
            sandbox_order(NIFTY_TRADE_INSTRUMENT, n_side)

        # SQL update
        if self._save and self._trade.get("id"):
            self._update(now, sensex, nifty, spread, zscore, pnl, reason)

        self._n_trades    += 1
        self._session_pnl += pnl
        if pnl >= 0:
            self._n_wins += 1

        tag = "WIN" if pnl >= 0 else "LOSS"
        print(
            f"\n\n"
            f"<<< [{now.strftime('%H:%M:%S')}] {self._trade['direction']} CLOSED  "
            f"reason={reason}  [{tag}]\n"
            f"    Sensex {sensex:>12,.2f}  |  Nifty {nifty:>10,.2f}  |  Z: {zscore:+.3f}\n"
            f"    P&L: {pnl:+.2f} pts  (approx Rs{pnl * POINT_VALUE:+,.0f})\n"
            f"    Session: {self._n_trades} trades | "
            f"Wins: {self._n_wins} | "
            f"Total P&L: {self._session_pnl:+.2f} pts\n"
        )
        self._trade = None

    # ── P&L helper ─────────────────────────────────────────────────────────────

    def _cur_pnl(self, current_spread):
        """Spread P&L using entry_ratio to avoid ratio drift."""
        if not self._trade:
            return 0.0
        # Use entry_ratio so ratio change doesn't affect our P&L calc
        fixed_spread = current_spread   # already computed with rolling ratio
        diff = fixed_spread - self._trade["entry_spread"]
        return diff if self._trade["direction"] == "LONG" else -diff

    # ── SQL helpers ─────────────────────────────────────────────────────────────

    def _insert(self):
        try:
            t = self._trade
            conn = get_conn()
            cur  = conn.cursor()
            cur.execute(
                f"INSERT INTO {TRADE_TABLE} "
                "(trade_date,direction,entry_time,entry_sensex,entry_nifty,"
                " entry_spread,entry_ratio,entry_zscore,status,"
                " sensex_order_id,nifty_order_id) "
                "OUTPUT INSERTED.id "
                "VALUES (?,?,?,?,?,?,?,?,'OPEN',?,?)",
                date.today(),
                t["direction"],
                t["entry_time"],
                t["entry_sensex"], t["entry_nifty"],
                t["entry_spread"], t["entry_ratio"], t["entry_zscore"],
                t.get("sensex_order_id"), t.get("nifty_order_id"),
            )
            row = cur.fetchone()
            conn.commit()
            conn.close()
            return row[0] if row else None
        except Exception as e:
            print(f"\n  [DB] insert: {e}")
            return None

    def _update(self, exit_time, sx, nf, sp, z, pnl, reason):
        try:
            conn = get_conn()
            cur  = conn.cursor()
            cur.execute(
                f"UPDATE {TRADE_TABLE} SET "
                "exit_time=?, exit_sensex=?, exit_nifty=?, "
                "exit_spread=?, exit_zscore=?, "
                "pnl_pts=?, exit_reason=?, status='CLOSED' "
                "WHERE id=?",
                exit_time,
                round(sx, 2), round(nf, 2),
                round(sp, 2), round(z, 3),
                round(pnl, 2), reason,
                self._trade["id"],
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"\n  [DB] update: {e}")

    # ── Display helpers ─────────────────────────────────────────────────────────

    def trade_summary_line(self, current_spread):
        if not self._trade:
            return "No open position"
        t   = self._trade
        pnl = self._cur_pnl(current_spread)
        age = str(datetime.now() - t["entry_time"]).split(".")[0]
        return (
            f"{t['direction']}  entry_Z {t['entry_zscore']:+.3f}  "
            f"entry_spread {t['entry_spread']:+.2f}  "
            f"live P&L {pnl:+.2f} pts  age {age}"
        )

    def session_line(self):
        wr = f"{self._n_wins}/{self._n_trades}" if self._n_trades else "0/0"
        return (
            f"Session: {self._n_trades} trades  W/L {wr}  "
            f"P&L {self._session_pnl:+.2f} pts  "
            f"(approx Rs{self._session_pnl * POINT_VALUE:+,.0f})"
        )


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    source    = "yfinance"
    interval  = 0.5          # poll every 0.5s = up to 2 ticks/second
    save_db   = "--no-save" not in sys.argv
    do_orders = "--orders"  in sys.argv   # off by default; sandbox unreachable on most networks

    if "--source" in sys.argv:
        source = sys.argv[sys.argv.index("--source") + 1]
    if "--interval" in sys.argv:
        interval = int(sys.argv[sys.argv.index("--interval") + 1])

    print(f"Loading history from [{source}_feed] ...")
    df_hist = load_from_sql(source)
    print(
        f"  {len(df_hist)} rows  "
        f"({df_hist['date'].min().date()} to {df_hist['date'].max().date()})"
    )

    live_token = get_live_token()
    print(f"  Market data token valid for {date.today()}")

    if save_db:
        ensure_tables()

    sandbox_enabled = do_orders and _SANDBOX_CFG
    print(f"  Paper trading: ACTIVE  (all trades saved to [{TRADE_TABLE}])")
    if sandbox_enabled:
        print(f"  Sandbox orders: ENABLED  {SANDBOX_BASE_URL}")

    trader = PaperTrader(save_db, do_orders)

    print(
        f"\nStrategy:  Entry +/-{ENTRY}  |  Exit +/-{EXIT}  |  "
        f"Target +{PROFIT_TARGET} pts  |  SL -{STOP_LOSS} pts  |  "
        f"MaxHold {MAX_HOLD} days  |  Rs{POINT_VALUE}/pt"
    )
    print(f"Interval: {interval}s  |  Press Ctrl+C to stop\n")

    hdr = (
        f"{'TIME':<10} {'SENSEX':>12} {'NIFTY':>11} {'RATIO':>8} "
        f"{'SPREAD':>9} {'Z':>8}  {'STATUS':<45} {'P&L':>12}"
    )
    print(hdr)
    print("-" * len(hdr))

    tick_count = 0

    try:
        while True:
            t0 = time.time()
            try:
                sensex, nifty, sx_quote, nf_quote = fetch_full_quote(live_token)
                now         = datetime.now()
                tick_count += 1

                # ── raw_data: save FIRST, before any computation ──────────────
                # This guarantees every single tick lands in raw_data even if
                # spread / z-score computation fails or throws.
                if save_db:
                    save_raw("SENSEX", sx_quote, now)
                    save_raw("NIFTY",  nf_quote, now)

                spread, zscore, ratio = compute_live(df_hist, sensex, nifty)

                status, live_pnl  = trader.on_tick(
                    sensex, nifty, spread, zscore, ratio, now
                )
                pnl_str = f"{live_pnl:+.2f}pts" if live_pnl is not None else "---"

                print(
                    f"\r{now.strftime('%H:%M:%S'):<10}"
                    f"{sensex:>12,.2f}"
                    f"{nifty:>11,.2f}"
                    f"{ratio or 0:>8.4f}"
                    f"{spread or 0:>9.2f}"
                    f"{zscore or 0:>8.3f}  "
                    f"{status:<45}"
                    f"{pnl_str:>12}",
                    end="", flush=True,
                )

                if save_db and spread is not None:
                    save_tick(now, sensex, nifty, spread, zscore, status)

            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 401:
                    print("\n\nToken expired — run  python live_fetch.py  and restart.")
                    break
                print(f"\n  [HTTP {e.response.status_code}] {e}")
            except Exception as e:
                print(f"\n  [Error] {e}")

            time.sleep(max(0, interval - (time.time() - t0)))

    except KeyboardInterrupt:
        print(f"\n\nStopped after {tick_count} ticks.")
        print(trader.session_line())
        if save_db:
            print(
                f"\nSQL queries:\n"
                f"  SELECT TOP 50 * FROM {TICK_TABLE}  ORDER BY tick_time  DESC\n"
                f"  SELECT * FROM {TRADE_TABLE} ORDER BY entry_time DESC"
            )


if __name__ == "__main__":
    main()
