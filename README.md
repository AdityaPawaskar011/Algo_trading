# Sensex–Nifty Ratio Spread Strategy

A pairs-trading project: collect live SENSEX/NIFTY data, paper-trade a spread
mean-reversion strategy on the Upstox sandbox, and backtest it honestly
(with real transaction costs and out-of-sample validation).

## The strategy in short
- SENSEX and NIFTY move together at a ratio of ~**3.2**.
- **Spread = SENSEX − (rolling-ratio × NIFTY)**. We turn it into a rolling **z-score**.
- **z ≤ −2 → LONG spread** (BUY SENSEX / SELL NIFTY); **z ≥ +2 → SHORT spread** (reverse).
- Exit on reversion (`|z| ≤ 0.7`), stop-loss, profit-target, or end-of-day.
- All strategy parameters live at the top of `backtest.py` (single source of truth).

> **Two storage modes exist in this repo.** The original scripts use **SQL Server**.
> The pipeline we actively use is **CSV-based** (no database) — that's the
> `tick_poller.py` + `*_intraday.py` set below.

---

## Files — what each one does

### ⭐ Live data + trading (CSV-based — the main pipeline)
| File | What it does |
|------|--------------|
| **`tick_poller.py`** | **The main engine.** Polls Upstox every second, writes full quotes to `sensex_data.csv` / `nifty_data.csv`, computes the live z-score signal, and (with `--trade`) places sandbox paper-trades. Auto-stops at 15:30 IST. Flags: `--trade`, `--lots N`, `--max-loss N`, `--max-trades N`, `--no-save`. |
| `live_fetch.py` | Daily Upstox **login** — opens OAuth in a browser, saves today's token to `upstox_token.json`. Run once each morning. |
| `recover_data.py` | Rebuilds `sensex_data.csv` / `nifty_data.csv` from `feed_run.log` if the raw data files are lost. |

### Strategy engine
| File | What it does |
|------|--------------|
| `backtest.py` | The strategy itself: `compute_signals()` (ratio/spread/z-score) + `backtest()` (entry/exit/P&L). All tunable params at the top: `ENTRY, EXIT, STOP_LOSS, PROFIT_TARGET, MAX_HOLD, RATIO_LOOKBACK, LOOKBACK, MODE`. |

### Backtest & analysis (run on collected CSV data)
| File | What it does |
|------|--------------|
| `backtest_intraday.py` | Applies the strategy to a day's tick CSVs. Supports `--resample 1min` and a `--cost-pts` transaction-cost model. Writes a detailed trade report + cost-sensitivity table. |
| `optimize_intraday.py` | Sweeps ~1,300 parameter combinations on a day's data, **net of costs**, and writes only the profitable settings (ranked). |
| `walkforward.py` | **Honest out-of-sample test.** Optimizes on one time slice, tests on the next unseen slice (e.g. train AM / test PM). Exposes overfitting. |

### Reports
| File | What it does |
|------|--------------|
| `build_excel_report.py` | Consolidates all result CSVs into **one formatted Excel workbook** (summary + trades + configs + price data). |
| `build_onepager.py` | Generates a **one-page Word briefing** (`.docx`) of the run + verdict. |

### Configuration & docs
| File | What it does |
|------|--------------|
| `config.example.py` | Template — copy to `config.py` and fill in your Upstox keys. |
| `config.py` | Your real keys (gitignored). DB settings, live API key/secret, sandbox token. |
| `requirements.txt` | Python dependencies. |
| `RUNBOOK.txt` | Original day-to-day operating guide (written for the SQL-Server workflow). |
| `README.md` | This file. |

### Original SQL-Server scripts (require a database — not used by the CSV pipeline)
| File | What it does |
|------|--------------|
| `live_trader.py` | Original main paper-trader; writes `raw_data` / `tick_data` / `paper_trade` tables to SQL Server. |
| `tick_feed.py` | Live feed via Upstox **WebSocket** → SQL `tick_data`. |
| `raw_feed.py` | Helper that writes full quote fields to the SQL `raw_data` table (used by `live_trader.py`). |
| `algo_signal.py` | One-shot: reads SQL history + a live price, prints the current LONG/SHORT/HOLD signal. |
| `check_trade.py` | Quick check of the current live price, z-score and open-trade status. |
| `fill_real_data.py` | Downloads SENSEX/NIFTY history from Yahoo Finance → SQL `yfinance_feed`. |
| `optimize.py` | Original walk-forward parameter optimizer (SQL, daily data). |
| `report.py` | Original 4-sheet Excel report from SQL data. |
| `export_sql.py` | Turns backtest output into a loadable `.sql` file. |
| `generate_data.py` | Builds a synthetic sample price series for offline testing. |

### Data & output files (generated)
| File | What it is |
|------|-----------|
| `history.csv` | Daily SENSEX/NIFTY closes for z-score warm-up (auto-downloaded from Yahoo on first run). |
| `sensex_data.csv` / `nifty_data.csv` | Per-second live feed capture (one row per tick). |
| `paper_trades.csv` | Log of closed sandbox paper-trades. |
| `instruments.json.gz` | Cached Upstox instrument master (used to find the near-month futures). |
| `upstox_token.json` | Today's live API token (from `live_fetch.py`). |
| `feed_run.log` | Console log of a live feed run (also a price backup). |
| `backtest_2026_06_18*.{csv,xlsx,docx}` | A day's backtest outputs + reports. |

---

## Quick start (CSV pipeline)

```powershell
# 1. one-time setup
.\log_tradingVenv\Scripts\Activate.ps1          # activate the virtualenv
#   copy config.example.py -> config.py and fill in your Upstox keys

# 2. each trading morning — get today's token (opens browser to log in)
python live_fetch.py

# 3. during market hours (09:15–15:30 IST) — collect + paper-trade
python tick_poller.py --trade --max-loss 200 --max-trades 5
#   writes sensex_data.csv / nifty_data.csv, trades -> paper_trades.csv

# 4. after the close — backtest & report on the day's data
python backtest_intraday.py --resample 1min --cost-pts 35 --out day.csv
python optimize_intraday.py --cost-pts 35       # net-of-cost parameter sweep
python walkforward.py --folds 2 --cost-pts 35   # out-of-sample sanity check
python build_excel_report.py                    # one Excel workbook
python build_onepager.py                         # one-page Word briefing
```

---

## Important notes
- **P&L is in spread points.** Realistic round-trip cost for a 1-lot SENSEX+NIFTY
  futures spread is **~35 points** (mostly STT). Always backtest *net of costs*.
- **One day of data proves nothing.** Tuning parameters on a single day is
  curve-fitting; only out-of-sample results (across many days) indicate a real edge.
- **No strategy guarantees profit.** Risk controls (`--max-loss`, `--max-trades`)
  *limit* losses; they cannot remove them.
- This is a study/paper-trading tool, not investment advice. Simulated performance
  does not predict live results, and pair trades carry risk if the ratio breaks down.
