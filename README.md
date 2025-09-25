# Aviron — Simple Competitor Price Monitor (Email Version)

This version emails you when a competitor's product price changes.

## Quick Start on Windows

1. Install Python 3.11 from python.org (check "Add to PATH").
2. Unzip this folder somewhere easy to find.
3. Open Command Prompt and `cd` into the folder.
4. Run:
   ```bat
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
5. Copy `.env.example` → `.env` and fill your email details (see below).
6. Edit `watchlist.csv` to add one product (URL + CSS selector).
7. Run:
   ```bat
   python watch.py
   ```

On first run, you’ll get an [INIT] email with the current price. On future runs, you’ll get an email only if the price changes.

## .env setup (for Gmail)
```
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USER=youraddress@gmail.com
EMAIL_PASS=your_app_password_here
EMAIL_FROM=Aviron Price Bot <youraddress@gmail.com>
EMAIL_TO=yourcoworker@avironactive.com
```

👉 For Gmail, you must create an App Password (Google Account → Security → 2‑Step Verification → App passwords).

## Watchlist
Open `watchlist.csv` and add competitor product URLs + CSS selectors for the price.

## Example run
```bat
python watch.py
```
