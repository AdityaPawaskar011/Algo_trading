# SENSEX–NIFTY Spread Strategy

Pairs-trading project: collect live SENSEX/NIFTY data, paper-trade a spread
mean-reversion strategy, and backtest it with realistic charges.

## The strategy in one line
SENSEX ≈ 3.2 × NIFTY. **Spread = SENSEX − (rolling-ratio × NIFTY)** → z-score.
**z ≤ −2 → LONG spread** (BUY SENSEX / SELL NIFTY); **z ≥ +2 → SHORT spread**.
Exit on reversion, stop-loss, profit-target, or end-of-day/expiry.

## Cost basis (IMPORTANT)
All P&L is calculated at **₹20 per order = ₹80 per round-trip** (a spread = 4 orders).
In the scripts this is the default `--cost-pts 4` (₹80 ÷ ₹20-per-point-per-lot).
Brokerage is flat per order, so at multiple lots the per-lot charge shrinks.
> If you trade index *futures*, exchange STT (~₹620/round-trip on the ₹15.5L notional)
> applies on top — not included here, per the chosen ₹20/order basis.

## Key finding
- **Intraday / per-second → loses** (moves ~18 pts; works gross, thin after charges).
- **Carry-forward (daily, LONG spread, hold for the big move) → profitable** — each
  winning trade captures 75–200+ pts, far above the charge. This is the live strategy.

---

## Folder layout
- **`code/`** — all scripts + `config.py` + this README
- **`feed_data/`** — tick CSVs + runtime files (`upstox_token.json`, `history.csv`, `instruments.json.gz`, `tracker_daily.csv`)
- **`reports/`** — all backtest outputs (CSV / XLSX / DOCX)

**Run scripts from `feed_data/`** so they find the data:
```powershell
cd feed_data
python ..\code\paper_tracker.py
```

## Files — what each does

### Live + forward trading
| File | Role |
|------|------|
| `tick_poller.py` | Live engine: polls Upstox each second -> `sensex_today.csv`/`nifty_today.csv`; `--trade` = sandbox paper-trading; flags `--intraday`, `--sensex-out/--nifty-out`, `--max-loss/--max-trades`. Auto-stops 15:30. |
| `paper_tracker.py` | Forward daily tracker for the profitable strategy - LONG spread, z <= -2, hold for +72 / -100 / 30 days. Run once after each close. |
| `live_fetch.py` | Daily Upstox OAuth login -> `upstox_token.json` (token expires daily). |
| `recover_data.py` | Rebuild `sensex_today.csv`/`nifty_today.csv` from `feed_run.log` if lost. |

### Strategy engine
| File | Role |
|------|------|
| `backtest.py` | Core `compute_signals()` + `backtest()`; strategy params at top. |

### Backtest & analysis (run on collected data)
| File | Role |
|------|------|
| `cost_backtester.py` | Comprehensive cost-aware backtester (metrics, sweep, walk-forward, charts -> one .xlsx). |
| `edge_strategy.py` | "Only trade when profit >> charge" - target = multiple of charge, room-filtered, train/test. |
| `backtest_daily.py` | Daily multi-day (position-holding) backtest. |
| `profit_strategy.py` | Daily LONG strategy with train/test out-of-sample split. |
| `bigmove_strategy.py` | Big-target (let-winners-run) test, train/test. |
| `profit_strategy_filtered.py` | Adds a trend filter; tests with/without, out-of-sample. |
| `backtest_intraday.py` | Intraday backtest with resampling + cost. |
| `optimize_intraday.py` | Parameter sweep (net of cost). |
| `walkforward.py` | Out-of-sample AM-train / PM-test. |
| `backtest_june.py` | Single-month (e.g. June) multi-day backtest. |
| `multiday_report.py` | Aggregate across several days. |

### Reports
| File | Role |
|------|------|
| `make_summary.py` | Plain-language summary CSV. |
| `build_excel_report.py` | Consolidated Excel workbook. |
| `build_onepager.py` | One-page Word briefing. |
| `feed_to_excel.py` | Dump a day's tick CSVs to Excel. |

### Config & legacy
| File | Role |
|------|------|
| `config.py` | Upstox API keys + sandbox token (`api-sandbox.upstox.com`). Gitignored. |
| `live_trader.py`, `tick_feed.py`, `raw_feed.py`, `algo_signal.py`, `check_trade.py`, `fill_real_data.py`, `optimize.py`, `report.py`, `export_sql.py`, `generate_data.py` | Original SQL-Server scripts (not used by the CSV pipeline). |

---

## Daily routine
1. **Morning:** `python live_fetch.py` (or paste the OAuth `?code=` URL) -> fresh token.
2. **Market hours:** `cd feed_data; python ..\code\tick_poller.py` (add `--trade` for paper trades).
3. **After 15:30 close:** `python ..\code\paper_tracker.py` to log the day's carry-forward signal.

## Notes
- venv: `log_tradingVenv` (created with the `py` launcher).
- Files here have been deleted across long idle gaps - **back up the folder (zip) regularly**;
  tick data can't be re-fetched once a market day closes.
- A single day proves nothing - validate forward (paper tracker) before real capital.
