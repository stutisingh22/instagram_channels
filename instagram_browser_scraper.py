from __future__ import annotations

import csv
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from playwright.sync_api import BrowserContext, Page, TimeoutError, sync_playwright


T = TypeVar("T")

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "out"
RAW_DIR = ROOT / "data" / "raw"
STATE_DIR = ROOT / ".ig_browser_state"
CHECKPOINT_FILE = RAW_DIR / "instagram_browser_checkpoint.json"


def load_config() -> dict[str, Any]:
    path = ROOT / "config.json"
    if not path.exists():
        raise RuntimeError("Missing config.json. Copy config.example.json first.")
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_dirs() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def retry(func: Callable[[], T], attempts: int = 3, base_sleep: float = 1.5) -> T:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:
            last_exc = exc
            if attempt < attempts:
                time.sleep(base_sleep * attempt + random.uniform(0.2, 0.9))
    if last_exc:
        raise last_exc
    raise RuntimeError("Retry failed unexpectedly.")


def wait_for_manual_login(page: Page) -> None:
    page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    time.sleep(2)
    if "accounts/login" in page.url or page.locator("input[name='username']").count() > 0:
        print("Login required in browser window.", flush=True)
        print("Please login manually in the opened Playwright browser.", flush=True)
        print("Waiting up to 10 minutes for successful login...", flush=True)
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(3)
            try:
                page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
                if "accounts/login" not in page.url and page.locator("input[name='username']").count() == 0:
                    break
            except Exception:
                continue
    print(f"Authenticated page URL: {page.url}", flush=True)


def is_login_page(page: Page) -> bool:
    return "accounts/login" in page.url or page.locator("input[name='username']").count() > 0


def is_challenge_page(page: Page) -> bool:
    url = page.url.lower()
    return "challenge" in url or "checkpoint" in url or "captcha" in url


def wait_for_challenge_resolution(page: Page) -> None:
    if not is_challenge_page(page):
        return
    print("Instagram challenge/checkpoint detected in browser.", flush=True)
    print("Please resolve it manually in the Playwright window.", flush=True)
    print("Waiting up to 10 minutes for challenge resolution...", flush=True)
    deadline = time.time() + 600
    while time.time() < deadline:
        time.sleep(4)
        try:
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        except Exception:
            continue
        if not is_challenge_page(page):
            break
    if is_challenge_page(page):
        raise RuntimeError("Challenge/checkpoint not resolved within timeout.")


def ensure_authenticated(page: Page, reason: str) -> None:
    if is_challenge_page(page):
        print(f"Challenge detected ({reason}).", flush=True)
        wait_for_challenge_resolution(page)
    if is_login_page(page):
        print(f"Authentication required ({reason}).", flush=True)
        wait_for_manual_login(page)
    if is_challenge_page(page):
        raise RuntimeError("Still on challenge/checkpoint page after waiting.")
    if is_login_page(page):
        raise RuntimeError("Still on login page after waiting for manual login.")


def collect_hashtag_post_links(
    page: Page, tag: str, posts_limit: int, sleep_seconds: float
) -> list[str]:
    url = f"https://www.instagram.com/explore/tags/{tag}/"
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(2)
    if is_login_page(page):
        ensure_authenticated(page, f"opening hashtag #{tag}")
        page.goto(url, wait_until="domcontentloaded")
        time.sleep(2)

    links: list[str] = []
    seen: set[str] = set()
    stagnant_rounds = 0
    max_rounds = max(20, posts_limit // 6)

    for round_idx in range(1, max_rounds + 1):
        hrefs = page.eval_on_selector_all(
            "a[href*='/p/'], a[href*='/reel/']",
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)",
        )
        page_html = page.content()
        # Fallback: Instagram often embeds links in JSON blobs before anchors fully render.
        if not hrefs:
            hrefs = re.findall(r'"/(p|reel)/([^"/]+)/"', page_html)
            hrefs = [f"/{kind}/{code}/" for kind, code in hrefs]
        if not hrefs:
            hrefs = re.findall(r'\\u002F(p|reel)\\u002F([^\\"]+)\\u002F', page_html)
            hrefs = [f"/{kind}/{code}/" for kind, code in hrefs]
        before = len(links)
        reel_count = 0
        for href in hrefs:
            m = re.match(r"^/(p|reel)/[^/]+/", href)
            if not m:
                continue
            full = f"https://www.instagram.com{m.group(0)}"
            if full not in seen:
                seen.add(full)
                links.append(full)
                if m.group(1) == "reel":
                    reel_count += 1
            if len(links) >= posts_limit:
                break
        print(
            f"#{tag} round {round_idx}: collected {len(links)}/{posts_limit} links "
            f"(reels this round: {reel_count})",
            flush=True,
        )
        if round_idx == 1:
            print(f"#{tag} page url: {page.url}", flush=True)
            print(f"#{tag} page title: {page.title()}", flush=True)
        if is_login_page(page):
            ensure_authenticated(page, f"reading hashtag #{tag} links")
            page.goto(url, wait_until="domcontentloaded")
            time.sleep(2)
            continue
        if len(links) >= posts_limit:
            break
        if len(links) == before:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
        if stagnant_rounds >= 4:
            break
        page.mouse.wheel(0, 3000)
        time.sleep(sleep_seconds + random.uniform(0.6, 1.4))
    return links[:posts_limit]


def extract_username_from_post(page: Page) -> str | None:
    selectors = [
        "header a[href^='/'][role='link']",
        "header a[href^='/']",
        "article header a[href^='/']",
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        if loc.count() == 0:
            continue
        href = loc.get_attribute("href")
        if not href:
            continue
        m = re.match(r"^/([A-Za-z0-9._]+)/?$", href)
        if m and m.group(1).lower() not in {"p", "reel", "explore"}:
            return m.group(1).lower()

    html = page.content()
    m = re.search(r'"owner"\s*:\s*\{"id":"\d+","username":"([^"]+)"', html)
    if m:
        return m.group(1).lower()
    return None


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


def normalize_followers_value(value: str | None) -> int | None:
    if not value:
        return None
    text = value.strip().replace(",", "").upper()
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
        return {"processed_tags": [], "hashtag_logs": [], "hashtag_usernames": {}}
    try:
        return json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"processed_tags": [], "hashtag_logs": [], "hashtag_usernames": {}}


def save_checkpoint(payload: dict[str, Any]) -> None:
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )


def run() -> None:
    ensure_dirs()
    cfg = load_config()
    hashtags: list[str] = cfg.get("hashtags", [])
    posts_limit = int(cfg.get("hashtag_posts_limit", 30))
    sleep_seconds = float(cfg.get("sleep_seconds", 1.5))
    inter_hashtag_delay = float(cfg.get("inter_hashtag_delay_seconds", 4.0))
    headless = os.getenv("IG_PLAYWRIGHT_HEADLESS", "false").lower() == "true"

    print(
        f"Starting browser automation scrape: hashtags={len(hashtags)}, "
        f"posts_limit={posts_limit}, headless={headless}",
        flush=True,
    )

    checkpoint = load_checkpoint()
    processed_tags: set[str] = set(checkpoint.get("processed_tags", []))
    hashtag_logs: list[dict[str, Any]] = checkpoint.get("hashtag_logs", [])
    hashtag_usernames: dict[str, set[str]] = {
        k: set(v) for k, v in checkpoint.get("hashtag_usernames", {}).items()
    }
    all_usernames: set[str] = set()
    for users in hashtag_usernames.values():
        all_usernames.update(users)

    with sync_playwright() as p:
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(STATE_DIR),
            headless=headless,
            viewport={"width": 1440, "height": 920},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()

        wait_for_manual_login(page)
        ensure_authenticated(page, "initial check")

        for idx, tag in enumerate(hashtags, start=1):
            if tag in processed_tags:
                print(f"[{idx}/{len(hashtags)}] hashtag #{tag} already processed, skipping", flush=True)
                continue
            print(f"[{idx}/{len(hashtags)}] hashtag #{tag} -> open", flush=True)
            usernames_for_tag: set[str] = set()
            try:
                links = retry(
                    lambda: collect_hashtag_post_links(page, tag, posts_limit, sleep_seconds),
                    attempts=3,
                )
                print(
                    f"[{idx}/{len(hashtags)}] #{tag} collected {len(links)} posts",
                    flush=True,
                )

                for post_idx, post_url in enumerate(links, start=1):
                    print(
                        f"[{idx}/{len(hashtags)}] #{tag} post {post_idx}/{len(links)} open post -> {post_url}",
                        flush=True,
                    )
                    try:
                        retry(lambda: page.goto(post_url, wait_until="domcontentloaded"), attempts=3)
                        time.sleep(1.1 + random.uniform(0.2, 0.8))
                        username = extract_username_from_post(page)
                        if username:
                            usernames_for_tag.add(username)
                            all_usernames.add(username)
                            print(
                                f"[{idx}/{len(hashtags)}] #{tag} post {post_idx}/{len(links)} "
                                f"open post -> got @{username} -> back",
                                flush=True,
                            )
                        else:
                            print(
                                f"[{idx}/{len(hashtags)}] #{tag} post {post_idx}/{len(links)} "
                                "open post -> got @unknown -> back",
                                flush=True,
                            )
                    except TimeoutError as exc:
                        print(
                            f"[{idx}/{len(hashtags)}] #{tag} post {post_idx}/{len(links)} "
                            f"open post -> failed ({exc}) -> back",
                            flush=True,
                        )
                    time.sleep(sleep_seconds + random.uniform(0.3, 1.0))

                hashtag_usernames[tag] = usernames_for_tag
                hashtag_logs.append(
                    {
                        "tag": tag,
                        "posts_collected": len(links),
                        "usernames_found": len(usernames_for_tag),
                        "error": None,
                    }
                )
            except Exception as exc:
                hashtag_usernames[tag] = usernames_for_tag
                hashtag_logs.append(
                    {
                        "tag": tag,
                        "posts_collected": 0,
                        "usernames_found": 0,
                        "error": str(exc),
                    }
                )
                print(f"[{idx}/{len(hashtags)}] #{tag} failed: {exc}", flush=True)
            processed_tags.add(tag)
            checkpoint_payload = {
                "processed_tags": sorted(processed_tags),
                "hashtag_logs": hashtag_logs,
                "hashtag_usernames": {k: sorted(v) for k, v in hashtag_usernames.items()},
            }
            save_checkpoint(checkpoint_payload)
            time.sleep(inter_hashtag_delay + random.uniform(0.8, 2.2))

        # Enrichment
        rows: list[dict[str, Any]] = []
        usernames_sorted = sorted(all_usernames)
        print(f"Starting profile enrichment for {len(usernames_sorted)} usernames", flush=True)
        for i, username in enumerate(usernames_sorted, start=1):
            profile_url = f"https://www.instagram.com/{username}/"
            try:
                retry(lambda: page.goto(profile_url, wait_until="domcontentloaded"), attempts=3)
                time.sleep(1.0 + random.uniform(0.3, 0.9))
                meta = parse_profile_meta(page, username)
                source_tags = [t for t, users in hashtag_usernames.items() if username in users]
                rows.append(
                    {
                        "profile_url": profile_url,
                        "username": username,
                        "name": meta["name"],
                        "followers": normalize_followers_value(meta["followers"]),
                        "followers_raw": meta["followers"],
                        "bio": meta["bio"],
                        "source_hashtags": "|".join(source_tags),
                        "error": None,
                    }
                )
            except Exception as exc:
                rows.append(
                    {
                        "profile_url": profile_url,
                        "username": username,
                        "name": None,
                        "followers": None,
                        "followers_raw": None,
                        "bio": None,
                        "source_hashtags": "",
                        "error": str(exc),
                    }
                )

            if i % 20 == 0 or i == len(usernames_sorted):
                print(f"Enriched {i}/{len(usernames_sorted)} profiles", flush=True)
            time.sleep(sleep_seconds + random.uniform(0.4, 1.1))

        context.close()

    # Persist outputs
    (RAW_DIR / "instagram_browser_hashtag_logs.json").write_text(
        json.dumps(hashtag_logs, ensure_ascii=True, indent=2), encoding="utf-8"
    )
    out_csv = OUT_DIR / "instagram_browser_accounts.csv"
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
                "source_hashtags",
                "error",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Hashtags processed: {len(hashtags)}", flush=True)
    print(f"Unique usernames discovered: {len(all_usernames)}", flush=True)
    print(f"Wrote: {out_csv}", flush=True)


if __name__ == "__main__":
    run()
