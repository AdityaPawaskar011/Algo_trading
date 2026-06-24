# Copy this file to config.py and fill in your real values.
# config.py is gitignored — never commit your actual keys.

# ── SQL Server ────────────────────────────────────────────────────────────────
DB_SERVER   = "localhost"
DB_NAME     = "testdb"
DB_DRIVER   = "ODBC Driver 17 for SQL Server"   # change to 18 if needed

# ── Upstox Live API (for market data, refreshed daily) ────────────────────────
# 1. Go to https://developer.upstox.com/  -> My Apps -> Create App
# 2. Set Redirect URI to exactly:  https://127.0.0.1/
# 3. Paste your keys below
UPSTOX_API_KEY      = "YOUR_API_KEY_HERE"
UPSTOX_API_SECRET   = "YOUR_SECRET_HERE"
UPSTOX_REDIRECT_URI = "https://127.0.0.1/"
TOKEN_FILE          = "upstox_token.json"

# ── Upstox Sandbox (for paper trading — no real money) ────────────────────────
# Create a sandbox app at https://developer.upstox.com/  -> My Apps -> Sandbox
# Token is long-lived (~months), no daily refresh needed
SANDBOX_API_KEY      = "YOUR_SANDBOX_KEY_HERE"
SANDBOX_BASE_URL     = "https://sandbox.upstox.com/v2"
SANDBOX_ACCESS_TOKEN = "YOUR_SANDBOX_ACCESS_TOKEN_HERE"

# ── Sandbox trade instruments (proxy ETFs used to simulate index trades) ───────
# These are the instruments placed in the sandbox when a signal fires.
# P&L is always tracked from actual index prices regardless of these tokens.
# Find exact tokens from Upstox instrument master CSV if needed.
NIFTY_TRADE_INSTRUMENT  = "NSE_EQ|INF204KB15I2"   # NIFTYBEES
SENSEX_TRADE_INSTRUMENT = "BSE_EQ|INF200K01VU8"   # SBI SENSEX ETF (SETFBSE)
TRADE_QUANTITY          = 1                        # units per side

# ₹ per spread point — used only for P&L display, not order sizing
POINT_VALUE = 100
