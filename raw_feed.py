"""
raw_feed.py — helper module for the raw_data SQL table.

Saves one row per symbol (SENSEX + NIFTY) every second with
all fields returned by the Upstox full-quote API.

Imported by live_trader.py — not meant to be run standalone.
"""

import pyodbc
from datetime import datetime

from config import DB_SERVER, DB_NAME, DB_DRIVER

TABLE = "raw_data"


def get_conn():
    return pyodbc.connect(
        f"DRIVER={{{DB_DRIVER}}};SERVER={DB_SERVER};"
        f"DATABASE={DB_NAME};Trusted_Connection=yes;"
    )


def ensure_raw_table():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(f"""
        IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = '{TABLE}')
        CREATE TABLE {TABLE} (
            id                  INT IDENTITY(1,1) PRIMARY KEY,
            symbol              VARCHAR(20)    NOT NULL,
            price               DECIMAL(12,2)  NOT NULL,
            datetime            DATETIME       NOT NULL,

            -- All fields from Upstox /v2/market-quote/quotes
            instrument_token    VARCHAR(60),
            open_price          DECIMAL(12,2),
            high_price          DECIMAL(12,2),
            low_price           DECIMAL(12,2),
            prev_close          DECIMAL(12,2),
            net_change          DECIMAL(10,2),
            volume              BIGINT,
            average_price       DECIMAL(12,2),
            oi                  BIGINT,
            total_buy_qty       BIGINT,
            total_sell_qty      BIGINT,
            lower_circuit       DECIMAL(12,2),
            upper_circuit       DECIMAL(12,2),
            oi_day_high         DECIMAL(12,2),
            oi_day_low          DECIMAL(12,2),
            last_trade_time     BIGINT,
            upstox_timestamp    VARCHAR(50)
        )
    """)
    conn.commit()
    conn.close()
    print(f"[{TABLE}] table ready in SQL Server.")


def save_raw(symbol: str, quote: dict, received_at: datetime):
    """
    Save one row for a single symbol.
    quote = the dict under data[instrument_key] from Upstox full-quote response.
    """
    ohlc = quote.get("ohlc") or {}

    def f(key, sub=None):
        val = (sub or quote).get(key)
        return float(val) if val is not None else None

    def i(key):
        val = quote.get(key)
        return int(val) if val is not None else None

    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute(
            f"INSERT INTO {TABLE} ("
            "symbol, price, datetime, instrument_token, "
            "open_price, high_price, low_price, prev_close, net_change, "
            "volume, average_price, oi, "
            "total_buy_qty, total_sell_qty, "
            "lower_circuit, upper_circuit, "
            "oi_day_high, oi_day_low, "
            "last_trade_time, upstox_timestamp"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            symbol,
            f("last_price"),
            received_at,
            quote.get("instrument_token"),
            f("open",  ohlc),
            f("high",  ohlc),
            f("low",   ohlc),
            f("close", ohlc),
            f("net_change"),
            i("volume"),
            f("average_price"),
            i("oi"),
            i("total_buy_quantity"),
            i("total_sell_quantity"),
            f("lower_circuit_limit"),
            f("upper_circuit_limit"),
            f("oi_day_high"),
            f("oi_day_low"),
            int(quote["last_trade_time"]) if quote.get("last_trade_time") else None,
            quote.get("timestamp"),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"\n  [raw_data DB error] {e}")
