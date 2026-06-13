# Copy this file to config.py and fill in your real values.
# config.py is gitignored — never commit your actual keys.

# ── SQL Server ────────────────────────────────────────────────────────────────
DB_SERVER   = "localhost"
DB_NAME     = "testdb"
DB_DRIVER   = "ODBC Driver 17 for SQL Server"   # change to 18 if needed

# ── Upstox API ────────────────────────────────────────────────────────────────
# 1. Go to https://developer.upstox.com/  -> My Apps -> Create App
# 2. Set Redirect URI to exactly:  https://127.0.0.1/
# 3. Paste your keys below
UPSTOX_API_KEY      = "YOUR_API_KEY_HERE"
UPSTOX_API_SECRET   = "YOUR_SECRET_HERE"
UPSTOX_REDIRECT_URI = "https://127.0.0.1/"

# File where the daily access token is cached (auto-created, also gitignored)
TOKEN_FILE = "upstox_token.json"
