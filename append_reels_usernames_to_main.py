#!/usr/bin/env python3
"""
Append reel-discovered Instagram profiles into your main append-safe CSV.

Input:
  - reels CSV with columns: reel_url, reel_id, username, source_keyword, seed_url
    (e.g. `data/out/instagram_reels_for_you_live.csv`)

Output (append-safe merge):
  - updates `data/out/instagram_search_accounts_append_safe.csv` by merging new rows
  - also writes a run-specific CSV: data/out/<reels_csv_stem>_profiles_append_<ts>.csv

Checkpoint:
  - data/raw/reels_username_append_checkpoint.json
So you can interrupt safely.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, sync_playwright

from instagram_profile_followers_fetch import (
    ensure_dirs,
    follower_count_from_html,
    normalize_followers_value,
    normalize_instagram_handle,
    parse_profile_meta,
    wait_for_manual_login,
)
from instagram_search_browser_scraper import (
    ROOT,
    get_state_dir,
    load_config,
    read_profiles_csv,
    merge_profiles_maps,
    write_live_csv_to_path,
)

OUT_DIR = ROOT / "data" / "out"
RAW_DIR = ROOT / "data" / "raw"

DEFAULT_REELS_CSV = OUT_DIR / "instagram_reels_for_you_live.csv"
DEFAULT_CHECKPOINT = RAW_DIR / "reels_username_append_checkpoint.json"


def load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"handles": {}, "updated_at_epoch": int(time.time())}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"handles": {}, "updated_at_epoch": int(time.time())}


def save_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["updated_at_epoch"] = int(time.time())
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def parse_reels_csv(path: Path) -> tuple[list[str], dict[str, set[str]]]:
    usernames: list[str] = []
    kw_by_user: dict[str, set[str]] = defaultdict(set)
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            u_raw = row.get("username", "") or ""
            u = normalize_instagram_handle(u_raw)
            if not u:
                continue
            usernames.append(u)
            kw = (row.get("source_keyword", "") or "").strip()
            if kw:
                kw_by_user[u].add(kw)
    usernames = list(dict.fromkeys(usernames))
    return usernames, kw_by_user


def fetch_profile(page: Page, username: str) -> tuple[int | None, str | None, str, str, str | None]:
    """
    Returns: followers_int, followers_raw, name, bio, error
    """
    profile_url = f"https://www.instagram.com/{username}/"
    followers_int: int | None = None
    followers_raw: str | None = None
    name = ""
    bio = ""
    err: str | None = None
    try:
        page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(0.9 + random.uniform(0.2, 0.6))
        meta = parse_profile_meta(page, username)
        name = str(meta.get("name") or "")
        bio = str(meta.get("bio") or "")
        followers_raw = meta.get("followers") or None
        if followers_raw:
            followers_int = normalize_followers_value(followers_raw)
        if followers_int is None:
            html = page.content()
            fc = follower_count_from_html(html)
            if fc is not None:
                followers_int = fc
                followers_raw = str(fc) if not followers_raw else followers_raw
    except Exception as exc:
        err = str(exc)
    return followers_int, followers_raw, name, bio, err


def main() -> None:
    ensure_dirs()
    ap = argparse.ArgumentParser(description="Append reels usernames to main append-safe CSV")
    ap.add_argument("--reels-csv", type=Path, default=DEFAULT_REELS_CSV, help="reels CSV path")
    ap.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT, help="checkpoint JSON path")
    ap.add_argument("--max-usernames", type=int, default=0, help="For testing: only process first N usernames")
    ap.add_argument("--headed", action="store_true", help="Force visible browser window")
    ap.add_argument("--sleep", type=float, default=1.0, help="Base sleep between profile requests")
    args = ap.parse_args()

    reels_csv = args.reels_csv if args.reels_csv.is_absolute() else (ROOT / args.reels_csv).resolve()
    if not reels_csv.exists():
        raise SystemExit(f"Missing reels csv: {reels_csv}")

    cfg = load_config()
    append_safe_main_name = str(cfg.get("search_append_safe_main_name", "instagram_search_accounts_append_safe.csv")).strip()
    append_safe_csv = OUT_DIR / append_safe_main_name

    usernames, kw_by_user = parse_reels_csv(reels_csv)
    if args.max_usernames > 0:
        usernames = usernames[: args.max_usernames]

    cp = load_checkpoint(args.checkpoint)
    cached: dict[str, Any] = cp.get("handles", {})
    to_fetch = [u for u in usernames if u not in cached]

    print(
        f"Append reels->main: reels={reels_csv.name} usernames={len(usernames)} to_fetch={len(to_fetch)} "
        f"append_safe_csv={append_safe_csv}",
        flush=True,
    )

    headless = (os.getenv("IG_PLAYWRIGHT_HEADLESS", "false").lower() == "true") and not args.headed

    with sync_playwright() as p:
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(get_state_dir()),
            headless=headless,
            viewport={"width": 1280, "height": 920},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()
        wait_for_manual_login(page)

        processed = 0
        for u in to_fetch:
            followers_int, followers_raw, name, bio, err = fetch_profile(page, u)
            cached[u] = {
                "followers": followers_int,
                "followers_raw": followers_raw or "",
                "name": name,
                "bio": bio,
                "error": err or "",
                "checked_at_epoch": int(time.time()),
            }
            processed += 1
            if processed % 20 == 0:
                save_checkpoint(args.checkpoint, {"handles": cached})
                print(f"  cached={processed}/{len(to_fetch)} latest=@{u}", flush=True)
            time.sleep(args.sleep + random.uniform(0.15, 0.65))

        context.close()

    # Build incoming profiles map for merge.
    incoming: dict[str, dict[str, Any]] = {}
    for u in usernames:
        c = cached.get(u) or {}
        followers_int = c.get("followers")
        followers_raw = c.get("followers_raw") or ""
        incoming[u] = {
            "profile_url": f"https://www.instagram.com/{u}/",
            "username": u,
            "name": c.get("name") or "",
            "followers": followers_int,
            "followers_raw": followers_raw,
            "bio": c.get("bio") or "",
            "is_private": None,
            "is_verified": None,
            "source_keywords": "|".join(sorted(list(kw_by_user.get(u, set())))[:6]),
            "error": c.get("error") or "",
        }

    base_map = read_profiles_csv(append_safe_csv)
    merged = merge_profiles_maps(base_map, incoming)
    write_live_csv_to_path(merged, append_safe_csv)

    # Also write run-specific snapshot for inspection.
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_snapshot = OUT_DIR / f"{reels_csv.stem}_profiles_append_{ts}.csv"
    with out_snapshot.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "username",
            "followers",
            "followers_raw",
            "name",
            "bio",
            "source_keywords",
            "error",
        ]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for u in usernames:
            c = cached.get(u) or {}
            w.writerow(
                {
                    "username": u,
                    "followers": c.get("followers") or "",
                    "followers_raw": c.get("followers_raw") or "",
                    "name": c.get("name") or "",
                    "bio": c.get("bio") or "",
                    "source_keywords": "|".join(sorted(list(kw_by_user.get(u, set())))[:6]),
                    "error": c.get("error") or "",
                }
            )

    save_checkpoint(args.checkpoint, {"handles": cached})
    print(f"Done. Updated append-safe main: {append_safe_csv}")
    print(f"Snapshot: {out_snapshot}")


if __name__ == "__main__":
    main()

