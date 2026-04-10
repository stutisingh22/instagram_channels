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

## Reels "For You" scraper (separate file)

New script for flow: keyword -> search page -> open reel seeds -> move through reel feed and capture IDs + usernames.

```bash
cp reels_for_you_config.example.json reels_for_you_config.json
python3 instagram_reels_for_you_scraper.py
```

Outputs:

- `data/out/instagram_reels_for_you_<timestamp>.csv`
- `data/raw/instagram_reels_for_you_<timestamp>.json`

## Vertical discovery + scoring (append/resume safe)

This path is designed for long runs where you want live CSV monitoring and kill-safe resume.

1. Run discovery (append-only candidates + checkpoint):

```bash
python3 news_discovery_engine.py --config hollywood_popculture_discovery_config.json
python3 news_discovery_engine.py --config us_politics_discovery_config.json
python3 news_discovery_engine.py --config us_local_politics_discovery_config.json
```

2. Run profile scoring/enrichment for each vertical:

```bash
python3 us_entertainment_news_pipeline.py --config hollywood_popculture_news_config.json
python3 us_entertainment_news_pipeline.py --config us_politics_news_config.json
python3 us_entertainment_news_pipeline.py --config us_local_politics_news_config.json
```

Monitoring files while running:

- Hollywood candidates: `data/out/us_hollywood_popculture_news_candidates_append.csv`
- National politics candidates: `data/out/us_politics_news_candidates_append.csv`
- Local (state + city) politics candidates: `data/out/us_local_politics_news_candidates_append.csv`
- Hollywood high-quality live: `data/out/us_hollywood_popculture_news_high_quality_live.csv`
- National politics high-quality live: `data/out/us_politics_news_high_quality_live.csv`
- Local politics high-quality live: `data/out/us_local_politics_news_high_quality_live.csv`

### Local (state + city) politics vertical

The `us_local_politics_*` configs target state capitol reporters, city hall
beat reporters, and state-focused outlets (Texas Tribune, CalMatters,
Spotlight PA, The Nevada Independent, etc.) — the accounts that the national
DC-focused configs never surface. Follower band is `9,000 – 55,000` to match
the acquisition target. Run the three stages the same way as the other
verticals:

```bash
python3 news_discovery_engine.py --config us_local_politics_discovery_config.json
.venv/bin/python following_graph_news_expander.py --config following_graph_local_politics_config.json
python3 us_entertainment_news_pipeline.py --config us_local_politics_news_config.json
```

Reset-from-scratch variants take the same `--reset-checkpoint` flag as the
other verticals.

Reset from scratch for a vertical:

```bash
python3 news_discovery_engine.py --config hollywood_popculture_discovery_config.json --reset-checkpoint
python3 us_entertainment_news_pipeline.py --config hollywood_popculture_news_config.json --reset-checkpoint
```

## Following-graph bulk expander (recommended for volume)

Opens each seed’s **Following** modal, scrolls, and BFS-expands up to `max_graph_depth`. Every new handle is **appended immediately** to CSV (`flush` + `fsync` per row). Checkpoint updates **after each expanded account** so you can stop anytime.

```bash
.venv/bin/python following_graph_news_expander.py --config following_graph_popculture_config.json
.venv/bin/python following_graph_news_expander.py --config following_graph_politics_config.json
.venv/bin/python following_graph_news_expander.py --config following_graph_local_politics_config.json
```

Outputs (append-only):

- Pop culture candidates: `data/out/us_popculture_graph_candidates_append.csv`
- Pop culture edges: `data/out/us_popculture_graph_edges_append.csv`
- National politics candidates: `data/out/us_politics_graph_candidates_append.csv`
- National politics edges: `data/out/us_politics_graph_edges_append.csv`
- Local politics candidates: `data/out/us_local_politics_graph_candidates_append.csv`
- Local politics edges: `data/out/us_local_politics_graph_edges_append.csv`

Point your scorer `input_search_csv` at the matching `*_graph_candidates_append.csv`, or merge that CSV with other candidate files before scoring.

Optional: set `"enrich_via_topsearch": true` in the graph config for richer rows (slower).

```bash
.venv/bin/python following_graph_news_expander.py --config following_graph_popculture_config.json --reset-checkpoint
```

## Instagram DM Outreach (append-only + resume safe)

Sends your message to a shortlist of Instagram handles, while writing progress continuously to CSV so you can kill/restart the terminal without losing already-sent/attempted rows.

### Input CSV
- A CSV file with a column containing the Instagram handle (for example header `handles`).

### Run
```bash
.venv/bin/python instagram_dm_outreach.py \
  --input-csv "/absolute/path/to/a-shortlisted.csv" \
  --handle-col "handles" \
  --message "Hey - I lead corporate development for LH2 Holdings, a US-based digital media company. We own and operate 20+ media brands and are actively looking to acquire pages in this space.\nWould you be open to a quick chat? We'd love to explore buying this page - we know how to grow these and have done it across a number of brands already.\nNo pressure, just want to see if there's interest.\n— Stuti" \
  --max-dms-per-run 100 \
  --batch-size 50 \
  --batch-sleep-seconds 60
```

### Monitor live progress
- Log CSV (append-only): `data/out/instagram_dm_outreach_log_append.csv`
- Checkpoint JSON (resume state): `data/raw/instagram_dm_outreach_checkpoint.json`

### Dry run (no clicking “Send”)
```bash
.venv/bin/python instagram_dm_outreach.py \
  --input-csv "/absolute/path/to/a-shortlisted.csv" \
  --handle-col "handles" \
  --dry-run
```
