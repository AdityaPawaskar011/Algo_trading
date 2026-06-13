# Sensex–Nifty Ratio Spread Strategy — Backtest Kit

## How I read your strategy
- Sensex and Nifty are traded as a **pair** at a ratio of ~**3.2** (your note: 23,500 × 3.2 = 75,200).
- **Spread = Sensex − (3.2 × Nifty)**. Negative = "credit spread" (Sensex cheap vs Nifty); positive = "debit spread".
- The spread is turned into a rolling **z-score**. When it stretches far from its average, enter and wait for it to revert ("works on expiry").
- **LONG_SPREAD** = BUY Sensex / SELL Nifty ("big buy, small sell"). **SHORT_SPREAD** = the reverse.

Default mode is **mean-reversion**. If you actually meant trend-following, set `MODE = "trend"` in `backtest.py`.

## Files
| File | What it is |
|------|-----------|
| `backtest.py` | The strategy engine. Tunable params at the top (RATIO, ENTRY, EXIT, LOOKBACK, MAX_HOLD, MODE). |
| `generate_data.py` | Builds a realistic SAMPLE Sensex/Nifty price series so you can test immediately. |
| `export_sql.py` | Turns the backtest output into a ready-to-load `.sql` file. |
| `ratio_strategy.sql` | **Load this into your DB.** Creates `price_data` (750 daily rows) + `trades` (26 trades) with INSERTs. |
| `trades.csv` / `price_data.csv` | Same data as CSV. |

## Run it
```bash
pip install pandas numpy
python3 generate_data.py     # makes price_data.csv (sample data)
python3 backtest.py          # runs strategy -> trades.csv + summary
python3 export_sql.py        # builds ratio_strategy.sql
```

### Use REAL market data
```bash
pip install yfinance
python3 backtest.py --real   # pulls ^BSESN (Sensex) + ^NSEI (Nifty) from Yahoo
```

### Load into SQL
```bash
# MySQL / Postgres
mysql  -u user -p dbname < ratio_strategy.sql
psql   -d dbname -f ratio_strategy.sql
# SQLite
sqlite3 mydb.db < ratio_strategy.sql
```

## Important notes
- The shipped data is **synthetic** (so the backtest runs without a feed). Numbers are illustrative, not real history — use `--real` for actual results.
- P&L is in **spread points**. Set `RUPEE_PER_POINT` and use real lot sizes (Nifty/Sensex lots & point values differ) before reading INR figures as real.
- This is a backtest for studying your idea, not trading or investment advice. Past/simulated performance doesn't predict live results, and pair trades carry risk if the ratio relationship breaks down.
