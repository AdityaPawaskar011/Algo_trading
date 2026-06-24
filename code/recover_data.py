"""
recover_data.py — rebuild sensex_data.csv / nifty_data.csv from feed_run.log.

The live feed printed every tick as a dashboard line:
   HH:MM:SS   <sensex>   <nifty>   <ratio> <spread> <z> SIGNAL ...
so the per-second SENSEX and NIFTY prices can be recovered from the log even
though the original data CSVs were deleted. Only price is recovered (the
volume/OI/depth enrichment columns are not in the log) — but the backtest only
needs prices, so the full trade analysis is reproducible.
"""
import csv
import re

DATE = "2026-06-18"
LINE = re.compile(r"^(\d{2}:\d{2}:\d{2})\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)\s")


def main():
    with open("feed_run.log", encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    sx, nf = [], []
    for raw in text.replace("\r", "\n").split("\n"):
        m = LINE.match(raw.strip())
        if not m:
            continue
        ts = f"{DATE} {m.group(1)}"
        sx.append((ts, m.group(2).replace(",", "")))
        nf.append((ts, m.group(3).replace(",", "")))

    for path, rows in (("sensex_data.csv", sx), ("nifty_data.csv", nf)):
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["tick_time", "last_price"])
            w.writerows(rows)

    print(f"Recovered {len(sx)} SENSEX ticks -> sensex_data.csv")
    print(f"Recovered {len(nf)} NIFTY  ticks -> nifty_data.csv")
    if sx:
        print(f"Span: {sx[0][0]}  ->  {sx[-1][0]}")


if __name__ == "__main__":
    main()
