from __future__ import annotations

import csv
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, sync_playwright


ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "out"
RAW_DIR = ROOT / "data" / "raw"
CONFIG_PATH = ROOT / "account_to_reels_config.json"
CHECKPOINT_FILE = RAW_DIR / "account_to_reels_checkpoint.json"


def ensure_dirs() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Missing config file: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def is_login_page(page: Page) -> bool:
    return "accounts/login" in page.url or page.locator("input[name='username']").count() > 0


def wait_for_manual_login(page: Page) -> None:
    page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    time.sleep(2)
    if is_login_page(page):
        print("Login required in Playwright browser.", flush=True)
        print("Please login manually. Waiting up to 10 minutes...", flush=True)
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(3)
            if not is_login_page(page):
                break
    if is_login_page(page):
        raise RuntimeError("Still on login page after wait.")


def load_checkpoint() -> dict[str, Any]:
    if not CHECKPOINT_FILE.exists():
        return {"processed_usernames": [], "rows": []}
    try:
        return json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"processed_usernames": [], "rows": []}


def save_checkpoint(data: dict[str, Any]) -> None:
    CHECKPOINT_FILE.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")


def latest_run_csv() -> Path | None:
    candidates = sorted(
        OUT_DIR.glob("instagram_search_accounts_run_*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def first_existing(paths: list[Path]) -> Path | None:
    for p in paths:
        if p.exists():
            return p
    return None


def resolve_accounts_csv(config_path: str) -> Path:
    path = Path(config_path)
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    # If configured file is empty/missing, fallback to latest run CSV.
    if not path.exists():
        fallback = latest_run_csv()
        if fallback:
            return fallback
        fallback = first_existing(
            [
                OUT_DIR / "discovered_accounts.csv",
                OUT_DIR / "ddg_instagram_accounts.csv",
            ]
        )
        return fallback if fallback else path
    with path.open("r", encoding="utf-8") as f:
        row_count = max(0, sum(1 for _ in f) - 1)
    if row_count <= 0:
        fallback = latest_run_csv()
        if fallback:
            return fallback
        fallback = first_existing(
            [
                OUT_DIR / "discovered_accounts.csv",
                OUT_DIR / "ddg_instagram_accounts.csv",
            ]
        )
        if fallback:
            return fallback
    return path


def load_usernames(accounts_csv: Path, max_accounts: int) -> list[str]:
    if not accounts_csv.exists():
        raise RuntimeError(f"Accounts CSV not found: {accounts_csv}")
    usernames: list[str] = []
    with accounts_csv.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            username = str(row.get("username", "")).strip().lower()
            if not username:
                continue
            usernames.append(username)
    usernames = list(dict.fromkeys(usernames))
    return usernames[:max_accounts]


def extract_reel_links(page: Page, limit: int) -> list[str]:
    hrefs = page.eval_on_selector_all(
        "a[href*='/reel/']",
        "els => els.map(e => e.getAttribute('href')).filter(Boolean)",
    )
    links: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        m = re.match(r"^/reel/([^/]+)/", href)
        if not m:
            continue
        url = f"https://www.instagram.com/reel/{m.group(1)}/"
        if url in seen:
            continue
        seen.add(url)
        links.append(url)
        if len(links) >= limit:
            break
    return links


def write_live_output(rows: list[dict[str, Any]]) -> Path:
    out_csv = OUT_DIR / "account_to_reels_output.csv"
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["username", "reel_id", "reel_url", "profile_reels_url", "error"],
        )
        writer.writeheader()
        writer.writerows(rows)
    return out_csv


def run() -> None:
    ensure_dirs()
    cfg = load_config()

    state_dir_cfg = str(cfg.get("playwright_state_dir", ".ig_browser_state")).strip()
    state_dir = Path(state_dir_cfg)
    if not state_dir.is_absolute():
        state_dir = (ROOT / state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)

    accounts_csv = resolve_accounts_csv(str(cfg.get("source_accounts_csv", "data/out/instagram_search_accounts.csv")))
    max_accounts = int(cfg.get("max_accounts", 300))
    max_reels_per_account = int(cfg.get("max_reels_per_account", 30))
    sleep_seconds = float(cfg.get("sleep_seconds", 1.0))
    headless = bool(cfg.get("headless", False))

    usernames = load_usernames(accounts_csv, max_accounts=max_accounts)
    print(
        f"Starting account->reels scrape with {len(usernames)} accounts from {accounts_csv}",
        flush=True,
    )
    if not usernames:
        print(
            "No accounts found in the selected CSV. Fill source_accounts_csv with usernames and rerun.",
            flush=True,
        )
        return

    checkpoint = load_checkpoint()
    processed = set(checkpoint.get("processed_usernames", []))
    rows: list[dict[str, Any]] = checkpoint.get("rows", [])
    seen_pairs = {(r.get("username"), r.get("reel_id")) for r in rows}

    with sync_playwright() as p:
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(state_dir),
            headless=headless,
            viewport={"width": 1440, "height": 920},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()
        wait_for_manual_login(page)

        for idx, username in enumerate(usernames, start=1):
            if username in processed:
                print(f"[{idx}/{len(usernames)}] @{username} already processed, skipping", flush=True)
                continue

            reels_url = f"https://www.instagram.com/{username}/reels/"
            print(f"[{idx}/{len(usernames)}] @{username} -> {reels_url}", flush=True)
            try:
                page.goto(reels_url, wait_until="domcontentloaded")
                if is_login_page(page):
                    wait_for_manual_login(page)
                    page.goto(reels_url, wait_until="domcontentloaded")
                time.sleep(1.6)

                links = extract_reel_links(page, limit=max_reels_per_account)
                if not links:
                    rows.append(
                        {
                            "username": username,
                            "reel_id": "",
                            "reel_url": "",
                            "profile_reels_url": reels_url,
                            "error": "no_reels_found",
                        }
                    )
                for link in links:
                    m = re.search(r"/reel/([^/]+)/", link)
                    reel_id = m.group(1) if m else ""
                    key = (username, reel_id)
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    rows.append(
                        {
                            "username": username,
                            "reel_id": reel_id,
                            "reel_url": link,
                            "profile_reels_url": reels_url,
                            "error": "",
                        }
                    )
                print(
                    f"[{idx}/{len(usernames)}] @{username}: reels captured={len(links)}",
                    flush=True,
                )
            except Exception as exc:
                rows.append(
                    {
                        "username": username,
                        "reel_id": "",
                        "reel_url": "",
                        "profile_reels_url": reels_url,
                        "error": str(exc),
                    }
                )
                print(f"[{idx}/{len(usernames)}] @{username} failed: {exc}", flush=True)

            processed.add(username)
            save_checkpoint({"processed_usernames": sorted(processed), "rows": rows})
            out_csv = write_live_output(rows)
            print(f"Live output updated -> {out_csv} (rows={len(rows)})", flush=True)
            time.sleep(sleep_seconds)

        context.close()

    print("Done.", flush=True)


if __name__ == "__main__":
    run()
