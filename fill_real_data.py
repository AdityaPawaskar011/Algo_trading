"""
Download real SENSEX + NIFTY history from Yahoo Finance and load into
the yfinance_feed table in SQL Server.

Usage:
    python fill_real_data.py                                  # 2025-01-01 to today
    python fill_real_data.py --start 2025-01-01               # custom start, to today
    python fill_real_data.py --start 2025-01-01 --end 2025-12-31  # full custom range
    python fill_real_data.py --start 2025-01-01 --yes         # skip confirmation
"""
import sys
import pyodbc
import pandas as pd
import yfinance as yf
from datetime import date

from config import DB_SERVER, DB_NAME, DB_DRIVER

TABLE = "yfinance_feed"


def parse_args():
    start = "2025-01-01"
    end   = str(date.today())
    if "--start" in sys.argv:
        idx   = sys.argv.index("--start")
        start = sys.argv[idx + 1]
    if "--end" in sys.argv:
        idx = sys.argv.index("--end")
        end = sys.argv[idx + 1]
    return start, end


def download_real_prices(start: str, end: str) -> pd.DataFrame:
    print(f"Downloading SENSEX (^BSESN) and NIFTY (^NSEI)  [{start}  to  {end}] ...")

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

    print(f"Downloaded {len(df)} trading days  ({df['date'].min()}  to  {df['date'].max()})")
    return df[["date", "sensex_close", "nifty_close"]]


def replace_sql_data(df: pd.DataFrame) -> None:
    conn_str = (
        f"DRIVER={{{DB_DRIVER}}};"
        f"SERVER={DB_SERVER};"
        f"DATABASE={DB_NAME};"
        "Trusted_Connection=yes;"
    )
    conn   = pyodbc.connect(conn_str)
    cursor = conn.cursor()

    cursor.execute(f"""
        IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = '{TABLE}')
        CREATE TABLE {TABLE} (
            trade_date   DATE PRIMARY KEY,
            sensex_close DECIMAL(12,2),
            nifty_close  DECIMAL(12,2)
        )
    """)

    print(f"Clearing existing [{TABLE}] rows ...")
    cursor.execute(f"DELETE FROM {TABLE}")

    print(f"Inserting {len(df)} rows into [{TABLE}] ...")
    for _, row in df.iterrows():
        cursor.execute(
            f"INSERT INTO {TABLE} (trade_date, sensex_close, nifty_close) VALUES (?, ?, ?)",
            (str(row["date"]), float(row["sensex_close"]), float(row["nifty_close"])),
        )

    conn.commit()
    conn.close()
    print(f"[{TABLE}] updated with real Yahoo Finance data.")


def main():
    start, end = parse_args()
    df = download_real_prices(start, end)

    print("\nSample (first 3 rows):")
    print(df.head(3).to_string(index=False))
    print("Sample (last 3 rows):")
    print(df.tail(3).to_string(index=False))

    if "--yes" not in sys.argv:
        answer = input(f"\nLoad {len(df)} rows into [{TABLE}]? [y/N]: ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    replace_sql_data(df)
    print(f"\nDone. Run  python backtest.py --source yfinance  to backtest.")


if __name__ == "__main__":
    main()
