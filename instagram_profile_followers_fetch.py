#!/usr/bin/env python3
"""Fetch username + follower fields for explicit Instagram profile URLs (separate CSV)."""

from __future__ import annotations

import csv
import os
import random
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.sync_api import BrowserContext, Page, sync_playwright

from instagram_search_browser_scraper import (
    ROOT,
    ensure_dirs,
    ensure_not_blocked,
    get_state_dir,
    normalize_followers_value,
    parse_profile_meta,
    wait_for_manual_login,
)

OUT_DIR = ROOT / "data" / "out"

# Default batch from user (override with INSTAGRAM_PROFILE_URLS_FILE=path/to.txt one URL per line).
DEFAULT_PROFILE_URLS: list[str] = [
    "https://www.instagram.com/bluevoterfeed?igsh=MXQ4YzdqeHhwZTBpaA==",
    "https://www.instagram.com/geo_grandmasters?igsh=Z29xMHRpaG5xdmxo",
    "https://www.instagram.com/globalxcode?igsh=dzBzM2lsMXlmaTUw",
    "https://www.instagram.com/thepatrioticcucumber?igsh=OGdma3IxeDI0bW45",
    "https://www.instagram.com/mastan_vlog_?igsh=b3ByYWJ1aG14OHZn",
    "https://www.instagram.com/breakingpoint.360?igsh=czAwbTI4cTlreHBh",
    "https://www.instagram.com/viralbreaking_news?igsh=NG5iN2h3NHVpYXNv",
    "https://www.instagram.com/bradyisbased?igsh=ZjVjOWczb2l6dThl",
    "https://www.instagram.com/theeconscoop?igsh=aDZzZnJ1bm9iZWpu",
    "https://www.instagram.com/daiily_pulse?igsh=MTVlNWI0bHdtaGIzbQ==",
    "https://www.instagram.com/ahadreports?igsh=cmVmc2pubGxxOTU3",
    "https://www.instagram.com/therepublicanbarbie?igsh=MThsN3dxdG5nanJoZg==",
    "https://www.instagram.com/michicon002?igsh=MW1nd3Vyamc0Y3dwaQ==",
    "https://www.instagram.com/karmanyanews?igsh=MXZuNDBqNmE4YWVnYw==",
    "https://www.instagram.com/geopoliticalupdates.in?igsh=bWt0anI1ZGpyb3Ru",
]


def username_from_profile_url(url: str) -> str | None:
    raw = unquote(url.strip())
    if not raw:
        return None
    p = urlparse(raw)
    path = (p.path or "").strip("/")
    if not path:
        return None
    seg = path.split("/")[0]
    if not seg or seg in {"explore", "accounts", "reels", "p", "reel"}:
        return None
    return seg.lower()


def follower_count_from_html(html: str) -> int | None:
    for pat in (
        r'"edge_followed_by"\s*:\s*\{\s*"count"\s*:\s*(\d+)',
        r'"follower_count"\s*:\s*(\d+)',
    ):
        m = re.search(pat, html)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def normalize_instagram_handle(raw: str) -> str | None:
    """Turn a cell value into a lowercase handle (strips @, optional profile URL)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    low = s.lower()
    if "instagram.com" in low:
        u = username_from_profile_url(s if s.startswith("http") else f"https://{s}")
        return u
    s = s.lstrip("@").split("/")[0].split("?")[0].strip().lower()
    if not s or s in {"explore", "accounts", "reels", "p", "reel", "stories"}:
        return None
    return s


def fetch_followers_for_handle(
    page: Page,
    username: str,
    *,
    sleep_after_load: bool = True,
) -> tuple[int | None, str | None, str | None]:
    """
    Visit instagram.com/{username}/ and return (followers_int, followers_raw, error_message).
    """
    profile_url = f"https://www.instagram.com/{username}/"
    err: str | None = None
    followers_raw: str | None = None
    followers_num: int | None = None
    try:
        page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
        ensure_not_blocked(page, f"profile @{username}")
        if sleep_after_load:
            time.sleep(0.8 + random.uniform(0.2, 0.7))
        meta = parse_profile_meta(page, username)
        followers_raw = meta.get("followers")
        if followers_raw:
            followers_num = normalize_followers_value(followers_raw)
        if followers_num is None:
            html = page.content()
            fc = follower_count_from_html(html)
            if fc is not None:
                followers_num = fc
                followers_raw = str(fc) if not followers_raw else followers_raw
    except Exception as exc:
        err = str(exc)
    return followers_num, followers_raw, err


def load_urls_from_env_or_file() -> list[str]:
    path_env = os.getenv("INSTAGRAM_PROFILE_URLS_FILE", "").strip()
    if path_env:
        p = Path(path_env)
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()]
        return [ln for ln in lines if ln and not ln.startswith("#")]
    return DEFAULT_PROFILE_URLS


def main() -> None:
    ensure_dirs()
    urls = load_urls_from_env_or_file()
    headless = os.getenv("IG_PLAYWRIGHT_HEADLESS", "false").lower() == "true"
    sleep_s = float(os.getenv("IG_PROFILE_FETCH_SLEEP", "1.2"))
    out_name = os.getenv(
        "IG_PROFILE_FETCH_OUT",
        f"manual_channels_followers_{time.strftime('%Y%m%d_%H%M%S')}.csv",
    )
    out_csv = OUT_DIR / out_name
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, str | int | None]] = []

    with sync_playwright() as p:
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(get_state_dir()),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()
        wait_for_manual_login(page)
        ensure_not_blocked(page, "profile fetch start")

        for i, url in enumerate(urls, start=1):
            un = username_from_profile_url(url)
            if not un:
                rows.append({"username": "", "followers": "", "followers_raw": "", "_err": "bad_url", "_src": url})
                print(f"[{i}/{len(urls)}] skip bad url: {url}", flush=True)
                continue
            followers_num, followers_raw, err = fetch_followers_for_handle(page, un)
            if err:
                print(f"[{i}/{len(urls)}] @{un} error: {err}", flush=True)

            rows.append(
                {
                    "username": un,
                    "followers": followers_num if followers_num is not None else "",
                    "followers_raw": followers_raw or "",
                    "_err": err or "",
                    "_src": url,
                }
            )
            if err:
                print(f"[{i}/{len(urls)}] @{un} error: {err}", flush=True)
            print(
                f"[{i}/{len(urls)}] @{un} followers={followers_num} raw={followers_raw!r}",
                flush=True,
            )
            time.sleep(sleep_s + random.uniform(0.2, 0.8))

        context.close()

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["username", "followers", "followers_raw"])
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "username": r.get("username", ""),
                    "followers": r.get("followers", ""),
                    "followers_raw": r.get("followers_raw", ""),
                }
            )

    print(f"Wrote {len(rows)} rows -> {out_csv}", flush=True)


if __name__ == "__main__":
    main()
