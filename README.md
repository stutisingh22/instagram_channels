# Instagram Account Discovery Pipeline (Local)

This runs entirely from your local workspace.

## What it does

- Discovery from:
  - hashtag crawl
  - following-graph crawl from seed accounts
- Profile enrichment
- Filtering for Instagram-native, media-like, US-targeted accounts
- CSV outputs for review

## Setup

1. Create a virtual environment and install deps:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

1. Create your config:

```bash
cp config.example.json config.json
```

1. Add your Apify token (required if `use_apify: true`):

```bash
cp .env.example .env
# edit .env with IG_USERNAME / IG_PASSWORD
```

## Run

```bash
python3 pipeline.py
```

On first run, the script logs in and saves an Instagram session file (`ig_session` by default).
Next runs can reuse that session.

## Output files

- `data/out/discovered_accounts.csv`
- `data/out/enriched_accounts.csv`
- `data/out/filtered_accounts.csv`

## Notes

- Keep `following_depth` and `following_per_account_limit` moderate to avoid rate limits.
- If login has 2FA/checkpoint prompts, complete them in browser and rerun.

## DDG fallback (no Instagram login required)

If Instagram throttles Instaloader, run the DuckDuckGo discovery path:

```bash
cp ddg_config.example.json ddg_config.json
python3 ddg_instagram_discovery.py
```

Output:

- `data/out/ddg_instagram_accounts.csv`
- `data/raw/ddg_search_results.json`

## Browser-session scraper (uses logged-in browser cookies)

If Instaloader endpoints are blocked, this path reuses your browser Instagram session:

```bash
python3 browser_session_scraper.py
```

If browser cookie auto-load fails on your machine, set fallback cookies in `.env`:

```bash
IG_SESSIONID=...
IG_CSRFTOKEN=...
```

Output:

- `data/out/browser_session_accounts.csv`
- `data/raw/browser_session_hashtag_logs.json`

## Robust browser automation scraper (recommended)

Uses real browser actions (open hashtag page -> open each post -> read username -> back).
This is more resilient than direct HTTP for Instagram.

```bash
python3 instagram_browser_scraper.py
```

Notes:

- First run opens a Chromium window; login manually once when prompted.
- Browser state is saved in `.ig_browser_state` and reused.
- Optional headless mode:
  - `IG_PLAYWRIGHT_HEADLESS=true python3 instagram_browser_scraper.py`

Outputs:

- `data/out/instagram_browser_accounts.csv`
- `data/raw/instagram_browser_hashtag_logs.json`

## Search-driven browser scraper (fallback for hashtag blocks)

Uses Instagram search pages (keywords) instead of hashtag pages:

```bash
python3 instagram_search_browser_scraper.py
```

Outputs:

- `data/out/instagram_search_accounts.csv`
- `data/raw/instagram_search_keyword_logs.json`
