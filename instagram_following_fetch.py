#!/usr/bin/env python3
"""Fetch accounts followed by one or more Instagram usernames."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, sync_playwright

from instagram_profile_followers_fetch import normalize_instagram_handle
from instagram_search_browser_scraper import (
    ROOT,
    ensure_dirs,
    ensure_not_blocked,
    get_state_dir,
    wait_for_manual_login,
)

OUT_DIR = ROOT / "data" / "out"
RAW_DIR = ROOT / "data" / "raw"
CHECKPOINT_FILE = RAW_DIR / "instagram_following_fetch_checkpoint.json"
DEFAULT_INPUT = ROOT / "instagram handles - Sheet1.csv"

RESERVED = {
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


def load_checkpoint() -> dict[str, Any]:
    if not CHECKPOINT_FILE.exists():
        return {"processed_sources": [], "edges": []}
    try:
        return json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"processed_sources": [], "edges": []}


def save_checkpoint(processed_sources: set[str], edges: list[dict[str, str]]) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "processed_sources": sorted(processed_sources),
        "edges": edges,
        "updated_at_epoch": int(time.time()),
    }
    CHECKPOINT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def write_edges_csv(path: Path, edges: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["source_username", "following_username"])
        w.writeheader()
        for row in edges:
            w.writerow(row)


def dedupe_edges(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        key = (r.get("source_username", ""), r.get("following_username", ""))
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        out.append({"source_username": key[0], "following_username": key[1]})
    return out


def parse_handles_from_csv(path: Path, limit: int = 0) -> list[str]:
    with path.open(newline="", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        handles: list[str] = []
        for row in rdr:
            raw = row.get("Username") or row.get("username") or ""
            u = normalize_instagram_handle(raw)
            if not u:
                continue
            handles.append(u)
            if limit > 0 and len(handles) >= limit:
                break
    return list(dict.fromkeys(handles))


def extract_following_handles(page: Page, source_username: str, max_accounts: int) -> list[str]:
    source_profile = f"https://www.instagram.com/{source_username}/"
    page.goto(source_profile, wait_until="domcontentloaded", timeout=60000)
    ensure_not_blocked(page, f"source @{source_username}")
    time.sleep(1.2)

    following_link = page.locator(f"a[href='/{source_username}/following/']").first
    if following_link.count() == 0:
        following_link = page.locator("a[href$='/following/']").first
    if following_link.count() == 0:
        raise RuntimeError("following_link_not_found")
    following_link.click(timeout=15000)

    dialog = page.locator("div[role='dialog']").first
    dialog.wait_for(timeout=20000)
    time.sleep(0.8)

    out: list[str] = []
    seen: set[str] = set()
    stagnant = 0
    rounds = max(15, max_accounts // 10)

    for _ in range(rounds):
        hrefs = dialog.locator("a[href^='/']").evaluate_all(
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
        )
        before = len(out)
        for href in hrefs:
            m = re.match(r"^/([A-Za-z0-9._]+)/?$", str(href))
            if not m:
                continue
            u = m.group(1).lower()
            if not u or u in RESERVED or u == source_username or u in seen:
                continue
            seen.add(u)
            out.append(u)
            if len(out) >= max_accounts:
                break
        if len(out) >= max_accounts:
            break
        if len(out) == before:
            stagnant += 1
        else:
            stagnant = 0
        if stagnant >= 4:
            break
        dialog.evaluate("el => { el.scrollTop = el.scrollHeight; }")
        time.sleep(0.9)

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return out


def main() -> None:
    ensure_dirs()
    ap = argparse.ArgumentParser(description="Fetch following lists for given Instagram usernames")
    ap.add_argument("--username", default="", help="Single source username (or Instagram URL)")
    ap.add_argument("--input-csv", type=Path, default=DEFAULT_INPUT, help="CSV with Username column")
    ap.add_argument("--limit-sources", type=int, default=0, help="Limit number of source accounts from CSV")
    ap.add_argument("--max-following-per-source", type=int, default=300, help="Max following usernames per source")
    ap.add_argument("--no-resume", action="store_true", help="Ignore processed sources from checkpoint")
    ap.add_argument("--headed", action="store_true", help="Force visible browser window")
    args = ap.parse_args()

    one = normalize_instagram_handle(args.username)
    if one:
        sources = [one]
    else:
        inp = args.input_csv if args.input_csv.is_absolute() else (ROOT / args.input_csv).resolve()
        if not inp.exists():
            raise SystemExit(f"Missing input CSV: {inp}")
        sources = parse_handles_from_csv(inp, limit=args.limit_sources)
    if not sources:
        raise SystemExit("No valid source usernames provided.")

    cp = load_checkpoint()
    processed = set(cp.get("processed_sources", []))
    edges = dedupe_edges(list(cp.get("edges", [])))

    if args.no_resume:
        processed = set()
        edges = []

    run_id = time.strftime("%Y%m%d_%H%M%S")
    out_csv = OUT_DIR / f"instagram_following_edges_run_{run_id}.csv"
    headless = (os.getenv("IG_PLAYWRIGHT_HEADLESS", "false").lower() == "true") and not args.headed

    print(
        f"Following fetch start: sources={len(sources)} max_per_source={args.max_following_per_source} "
        f"resume={not args.no_resume} headless={headless}",
        flush=True,
    )

    with sync_playwright() as p:
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(get_state_dir()),
            headless=headless,
            viewport={"width": 1280, "height": 920},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()
        wait_for_manual_login(page)
        ensure_not_blocked(page, "following fetch init")

        for idx, source in enumerate(sources, start=1):
            if source in processed:
                print(f"[{idx}/{len(sources)}] @{source} already processed, skipping", flush=True)
                continue
            try:
                following = extract_following_handles(page, source, args.max_following_per_source)
                print(f"[{idx}/{len(sources)}] @{source} following_found={len(following)}", flush=True)
                for f in following:
                    edges.append({"source_username": source, "following_username": f})
            except Exception as exc:
                print(f"[{idx}/{len(sources)}] @{source} failed: {exc}", flush=True)
            processed.add(source)
            edges = dedupe_edges(edges)
            save_checkpoint(processed, edges)
            write_edges_csv(out_csv, edges)
        context.close()

    print(f"Wrote {len(edges)} edges -> {out_csv}", flush=True)
    print(f"Checkpoint -> {CHECKPOINT_FILE}", flush=True)


if __name__ == "__main__":
    main()
