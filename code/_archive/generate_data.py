"""
Generate a realistic SAMPLE daily price series for Sensex & Nifty.

This is synthetic data so you can test the backtest mechanics end-to-end
WITHOUT needing a live data feed. The two series are built to be
"cointegrated" (Sensex tracks ~3.2x Nifty with a mean-reverting spread),
which is exactly the relationship your ratio strategy assumes.

When you run for REAL: replace this with backtest.load_real_data() which
pulls Yahoo Finance (^BSESN = Sensex, ^NSEI = Nifty) via yfinance.
"""
import numpy as np
import pandas as pd

def generate(start="2023-01-02", periods=750, seed=42, ratio=3.2):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start=start, periods=periods)

    # Nifty: geometric random walk with slight upward drift, starting ~21000
    nifty_ret = rng.normal(loc=0.0004, scale=0.010, size=periods)
    nifty = 21000 * np.cumprod(1 + nifty_ret)

    # Spread follows an Ornstein-Uhlenbeck (mean-reverting) process around 0.
    # This is what makes the ratio strategy tradeable.
    theta, mu, sigma = 0.06, 0.0, 180.0   # reversion speed, mean, vol
    spread = np.zeros(periods)
    for t in range(1, periods):
        spread[t] = spread[t-1] + theta * (mu - spread[t-1]) + rng.normal(0, sigma)

    # Sensex = ratio * Nifty + mean-reverting spread + small noise
    sensex = ratio * nifty + spread + rng.normal(0, 25, size=periods)

    df = pd.DataFrame({
        "date": dates,
        "sensex_close": np.round(sensex, 2),
        "nifty_close": np.round(nifty, 2),
    })
    return df

if __name__ == "__main__":
    df = generate()
    df.to_csv("price_data.csv", index=False)
    print(df.head())
    print(f"\nRows: {len(df)}  |  {df.date.min().date()} -> {df.date.max().date()}")
    print(f"Implied ratio (mean Sensex/Nifty): {(df.sensex_close/df.nifty_close).mean():.3f}")
