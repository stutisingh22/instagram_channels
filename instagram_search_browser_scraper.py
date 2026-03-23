from __future__ import annotations

import csv
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, sync_playwright


ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "out"
RAW_DIR = ROOT / "data" / "raw"
STATE_DIR = ROOT / ".ig_browser_state"
CHECKPOINT_FILE = RAW_DIR / "instagram_search_checkpoint.json"

RESERVED_HANDLES = {
    "explore",
    "accounts",
    "reels",
    "stories",
    "about",
    "p",
    "direct",
    "instagram",
    "meta",
    "help",
    "privacy",
    "legal",
    "press",
    "api",
    "terms",
    "locations",
    "threads",
}


def load_config() -> dict[str, Any]:
    path = ROOT / "config.json"
    if not path.exists():
        raise RuntimeError("Missing config.json. Copy config.example.json first.")
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_dirs() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def is_login_page(page: Page) -> bool:
    return "accounts/login" in page.url or page.locator("input[name='username']").count() > 0


def is_challenge_page(page: Page) -> bool:
    url = page.url.lower()
    return "challenge" in url or "checkpoint" in url or "captcha" in url


def wait_for_manual_login(page: Page) -> None:
    page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    time.sleep(2)
    if is_login_page(page):
        print("Login required in Playwright browser window.", flush=True)
        print("Please login manually. Waiting up to 10 minutes...", flush=True)
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(3)
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
            if not is_login_page(page):
                break
    if is_login_page(page):
        raise RuntimeError("Still on login page after wait.")
    print(f"Authenticated page URL: {page.url}", flush=True)


def ensure_not_blocked(page: Page, reason: str) -> None:
    if is_challenge_page(page):
        print(f"Challenge/checkpoint detected ({reason}). Resolve in browser window.", flush=True)
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(3)
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
            if not is_challenge_page(page):
                break
        if is_challenge_page(page):
            raise RuntimeError("Challenge/checkpoint not resolved in time.")
    if is_login_page(page):
        wait_for_manual_login(page)


def normalize_followers_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().replace(",", "").upper()
    m = re.match(r"^(\d+(?:\.\d+)?)([MK])?$", text)
    if not m:
        return None
    base = float(m.group(1))
    suffix = m.group(2)
    if suffix == "M":
        base *= 1_000_000
    elif suffix == "K":
        base *= 1_000
    return int(base)


def load_checkpoint() -> dict[str, Any]:
    if not CHECKPOINT_FILE.exists():
        return {"processed_keywords": [], "keyword_logs": [], "profiles": {}}
    try:
        return json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"processed_keywords": [], "keyword_logs": [], "profiles": {}}


def save_checkpoint(payload: dict[str, Any]) -> None:
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def recompute_processed_keywords(keyword_logs: list[dict[str, Any]]) -> set[str]:
    processed: set[str] = set()
    for row in keyword_logs:
        keyword = str(row.get("keyword", "")).strip()
        if not keyword:
            continue
        found = int(row.get("usernames_found", 0) or 0)
        err = row.get("error")
        if found > 0 and not err:
            processed.add(keyword)
    return processed


def parse_profile_meta(page: Page, username: str) -> dict[str, Any]:
    title = page.title() or ""
    desc = ""
    try:
        desc = page.locator("meta[name='description']").first.get_attribute("content") or ""
    except Exception:
        desc = ""

    name = None
    t = re.match(r"(.+?)\s+\(@" + re.escape(username) + r"\)", title, flags=re.IGNORECASE)
    if t:
        name = t.group(1).strip()

    followers = None
    f = re.search(r"([\d.,]+[MK]?)\s+Followers", desc, flags=re.IGNORECASE)
    if f:
        followers = f.group(1)

    bio = None
    b = re.search(r'on Instagram:\s*"(.*?)"', desc, flags=re.IGNORECASE | re.DOTALL)
    if b:
        bio = b.group(1).replace("\\n", " ").strip()

    return {"name": name, "followers": followers, "bio": bio}


def collect_usernames_from_search(page: Page, keyword: str, max_accounts: int, sleep_s: float) -> list[str]:
    search_url = f"https://www.instagram.com/explore/search/keyword/?q={keyword.replace(' ', '%20')}"
    page.goto(search_url, wait_until="domcontentloaded")
    time.sleep(2.5)
    if is_login_page(page):
        wait_for_manual_login(page)
        page.goto(search_url, wait_until="domcontentloaded")
        time.sleep(2.5)

    usernames: list[str] = []
    seen: set[str] = set()
    stagnant = 0

    for round_idx in range(1, 5):
        hrefs = page.eval_on_selector_all(
            "a[href^='/'][role='link'], a[href^='/']",
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)",
        )
        before = len(usernames)
        for href in hrefs:
            m = re.match(r"^/([A-Za-z0-9._]+)/?$", href)
            if not m:
                continue
            handle = m.group(1).lower()
            if handle in RESERVED_HANDLES:
                continue
            if handle in seen:
                continue
            seen.add(handle)
            usernames.append(handle)
            if len(usernames) >= max_accounts:
                break

        print(
            f"search '{keyword}' round {round_idx}: usernames {len(usernames)}/{max_accounts}",
            flush=True,
        )
        if len(usernames) >= max_accounts:
            break
        if len(usernames) == before:
            stagnant += 1
        else:
            stagnant = 0
        if stagnant >= 4:
            break

        page.mouse.wheel(0, 2800)
        time.sleep(sleep_s + random.uniform(0.4, 1.2))

    return usernames


def collect_usernames_via_topsearch(page: Page, keyword: str, max_accounts: int) -> list[str]:
    """Use Instagram authenticated web search API via browser context cookies."""
    encoded = keyword.replace(" ", "%20")
    url = f"https://www.instagram.com/web/search/topsearch/?query={encoded}"
    resp = page.request.get(
        url,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.instagram.com/",
        },
        timeout=45000,
    )
    if resp.status != 200:
        raise RuntimeError(f"topsearch status={resp.status}")

    payload = resp.json()
    users = payload.get("users", []) if isinstance(payload, dict) else []
    usernames: list[str] = []
    seen: set[str] = set()
    for item in users:
        user = item.get("user", {}) if isinstance(item, dict) else {}
        username = str(user.get("username", "")).strip().lower()
        if not username or username in seen:
            continue
        seen.add(username)
        usernames.append(username)
        if len(usernames) >= max_accounts:
            break
    return usernames


def collect_profiles_via_topsearch(page: Page, keyword: str, max_accounts: int) -> dict[str, dict[str, Any]]:
    encoded = keyword.replace(" ", "%20")
    url = f"https://www.instagram.com/web/search/topsearch/?query={encoded}"
    resp = page.request.get(
        url,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.instagram.com/",
        },
        timeout=45000,
    )
    if resp.status != 200:
        raise RuntimeError(f"topsearch_http_{resp.status}")

    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("topsearch returned non-dict payload")
    users = payload.get("users", [])
    if not isinstance(users, list):
        raise RuntimeError("topsearch payload users is not a list")

    profiles: dict[str, dict[str, Any]] = {}
    for item in users:
        if not isinstance(item, dict):
            continue
        user = item.get("user", {})
        if not isinstance(user, dict):
            continue
        username = str(user.get("username", "")).strip().lower()
        if not username or username in RESERVED_HANDLES:
            continue

        profiles[username] = {
            "profile_url": f"https://www.instagram.com/{username}/",
            "username": username,
            "name": user.get("full_name"),
            "followers": normalize_followers_value(user.get("follower_count")),
            "followers_raw": user.get("follower_count"),
            "bio": user.get("biography"),
            "is_private": user.get("is_private"),
            "is_verified": user.get("is_verified"),
        }
        if len(profiles) >= max_accounts:
            break
    return profiles


def run() -> None:
    ensure_dirs()
    cfg = load_config()
    keywords: list[str] = cfg.get("search_keywords_browser", [])
    max_per_keyword = int(cfg.get("search_accounts_limit_per_keyword", 80))
    sleep_seconds = float(cfg.get("sleep_seconds", 1.3))
    inter_keyword_delay = float(cfg.get("inter_keyword_delay_seconds", 3.5))
    enrich_missing_via_profile = bool(cfg.get("search_enrich_missing_via_profile", False))
    headless = os.getenv("IG_PLAYWRIGHT_HEADLESS", "false").lower() == "true"

    print(
        f"Starting search-browser scrape: keywords={len(keywords)}, per_keyword={max_per_keyword}, headless={headless}",
        flush=True,
    )

    checkpoint = load_checkpoint()
    processed_keywords: set[str] = recompute_processed_keywords(checkpoint.get("keyword_logs", []))
    keyword_logs: list[dict[str, Any]] = checkpoint.get("keyword_logs", [])
    profiles_by_username: dict[str, dict[str, Any]] = checkpoint.get("profiles", {})
    for row in profiles_by_username.values():
        existing = str(row.get("source_keywords", "")).strip()
        row["source_keywords"] = existing

    with sync_playwright() as p:
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(STATE_DIR),
            headless=headless,
            viewport={"width": 1440, "height": 920},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()
        wait_for_manual_login(page)
        ensure_not_blocked(page, "initial check")

        for i, keyword in enumerate(keywords, start=1):
            if keyword in processed_keywords:
                print(f"[{i}/{len(keywords)}] keyword '{keyword}' already processed, skipping", flush=True)
                continue
            print(f"[{i}/{len(keywords)}] search keyword -> '{keyword}'", flush=True)
            try:
                ensure_not_blocked(page, f"before keyword '{keyword}'")
                topsearch_profiles: dict[str, dict[str, Any]] = {}
                topsearch_error: str | None = None
                try:
                    topsearch_profiles = collect_profiles_via_topsearch(page, keyword, max_per_keyword)
                    print(
                        f"[{i}/{len(keywords)}] '{keyword}' topsearch usernames={len(topsearch_profiles)}",
                        flush=True,
                    )
                except Exception as exc:
                    topsearch_error = str(exc)
                    print(
                        f"[{i}/{len(keywords)}] '{keyword}' topsearch unavailable ({topsearch_error}); using DOM fallback",
                        flush=True,
                    )

                if topsearch_profiles:
                    for username, profile_data in topsearch_profiles.items():
                        existing = profiles_by_username.get(username, {})
                        merged_keywords = set(
                            x for x in str(existing.get("source_keywords", "")).split("|") if x
                        )
                        merged_keywords.add(keyword)
                        profiles_by_username[username] = {
                            **existing,
                            **profile_data,
                            "source_keywords": "|".join(sorted(merged_keywords)),
                            "error": None,
                        }
                    found_count = len(topsearch_profiles)
                else:
                    users = collect_usernames_from_search(page, keyword, max_per_keyword, sleep_seconds)
                    users_set = set(users)
                    for username in users_set:
                        existing = profiles_by_username.get(username, {})
                        merged_keywords = set(
                            x for x in str(existing.get("source_keywords", "")).split("|") if x
                        )
                        merged_keywords.add(keyword)
                        profiles_by_username[username] = {
                            "profile_url": f"https://www.instagram.com/{username}/",
                            "username": username,
                            "name": existing.get("name"),
                            "followers": existing.get("followers"),
                            "followers_raw": existing.get("followers_raw"),
                            "bio": existing.get("bio"),
                            "is_private": existing.get("is_private"),
                            "is_verified": existing.get("is_verified"),
                            "source_keywords": "|".join(sorted(merged_keywords)),
                            "error": existing.get("error") or topsearch_error,
                        }
                    found_count = len(users_set)
                keyword_logs.append(
                    {"keyword": keyword, "usernames_found": found_count, "error": None}
                )
                print(
                    f"[{i}/{len(keywords)}] '{keyword}' done, usernames found={found_count}",
                    flush=True,
                )
            except Exception as exc:
                keyword_logs.append(
                    {"keyword": keyword, "usernames_found": 0, "error": str(exc)}
                )
                print(f"[{i}/{len(keywords)}] '{keyword}' failed: {exc}", flush=True)
            processed_keywords.add(keyword)
            save_checkpoint(
                {
                    "processed_keywords": sorted(processed_keywords),
                    "keyword_logs": keyword_logs,
                    "profiles": profiles_by_username,
                }
            )
            time.sleep(inter_keyword_delay + random.uniform(0.7, 1.8))

        if enrich_missing_via_profile:
            rows_to_visit = [u for u, row in profiles_by_username.items() if not row.get("followers")]
            print(f"Optional profile enrichment for {len(rows_to_visit)} missing profiles", flush=True)
            for idx, username in enumerate(rows_to_visit, start=1):
                profile_url = f"https://www.instagram.com/{username}/"
                try:
                    page.goto(profile_url, wait_until="domcontentloaded")
                    ensure_not_blocked(page, f"profile enrich @{username}")
                    time.sleep(1.0 + random.uniform(0.3, 0.9))
                    meta = parse_profile_meta(page, username)
                    row = profiles_by_username.get(username, {})
                    row["name"] = row.get("name") or meta.get("name")
                    row["followers_raw"] = row.get("followers_raw") or meta.get("followers")
                    row["followers"] = row.get("followers") or normalize_followers_value(meta.get("followers"))
                    row["bio"] = row.get("bio") or meta.get("bio")
                    row["error"] = None
                    profiles_by_username[username] = row
                except Exception as exc:
                    row = profiles_by_username.get(username, {})
                    row["error"] = str(exc)
                    profiles_by_username[username] = row
                if idx % 20 == 0 or idx == len(rows_to_visit):
                    print(f"Optional enriched {idx}/{len(rows_to_visit)} profiles", flush=True)
                time.sleep(sleep_seconds + random.uniform(0.3, 1.0))

        context.close()

    (RAW_DIR / "instagram_search_keyword_logs.json").write_text(
        json.dumps(keyword_logs, ensure_ascii=True, indent=2), encoding="utf-8"
    )

    out_csv = OUT_DIR / "instagram_search_accounts.csv"
    rows = sorted(profiles_by_username.values(), key=lambda x: str(x.get("username", "")))
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "profile_url",
                "username",
                "name",
                "followers",
                "followers_raw",
                "bio",
                "is_private",
                "is_verified",
                "source_keywords",
                "error",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Keywords processed: {len(keywords)}", flush=True)
    print(f"Unique usernames discovered: {len(profiles_by_username)}", flush=True)
    print(f"Wrote: {out_csv}", flush=True)


if __name__ == "__main__":
    run()
