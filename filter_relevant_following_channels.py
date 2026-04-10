#!/usr/bin/env python3
"""
Filter `instagram_following_edges_run_*.csv` down to relevant Instagram channels.

How:
1. Read edges CSV (source_username, following_username).
2. For each unique following_username, open profile and extract:
   - name + bio from meta description
   - followers count (followers + followers_raw)
3. Score relevance via keyword hits in (name + bio).
4. Gate by follower band (default from config.json: 9000..80000).
5. Output:
   - cleaned edges CSV (only edges where following account passed relevance)
   - profiles CSV (one row per following_username with score + matched terms)

Checkpoint:
  data/raw/following_channel_relevance_checkpoint.json
so you can interrupt/resume safely.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
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
    load_config,
    normalize_followers_value,
    parse_profile_meta,
    wait_for_manual_login,
    read_profiles_csv,
    merge_profiles_maps,
    write_live_csv_to_path,
)

OUT_DIR = ROOT / "data" / "out"
RAW_DIR = ROOT / "data" / "raw"
DEFAULT_CSV = OUT_DIR / "instagram_following_edges_run_20260325_113422.csv"
DEFAULT_CHECKPOINT_FILE = RAW_DIR / "following_channel_relevance_checkpoint.json"


def to_profile_url(username: str) -> str:
    return f"https://www.instagram.com/{username}/"


def load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"handles": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"handles": {}}


def save_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


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


def score_relevance(
    name: str | None,
    bio: str | None,
    positive: list[str],
    core: list[str],
    negative: list[str],
) -> tuple[int, list[str], str]:
    txt = f"{name or ''} {bio or ''}".lower()
    if not txt.strip():
        return 0, [], ""

    def term_in_text(term: str) -> bool:
        t = (term or "").lower().strip()
        if not t:
            return False
        # For very short tokens, use "whole word" matching to prevent substring false-positives
        # (e.g. "cia" inside "certification", "ai" inside "chair").
        if len(t) <= 3 and re.fullmatch(r"[a-z0-9]+", t):
            return re.search(rf"(?<!\\w){re.escape(t)}(?!\\w)", txt) is not None
        return t in txt

    neg_hit = ""
    for n in negative:
        if term_in_text(n):
            neg_hit = n
            break
    if neg_hit:
        return -1, [], f"excluded_by:{neg_hit}"

    matched_core: list[str] = []
    matched_pos: list[str] = []
    score = 0

    for c in core:
        if term_in_text(c):
            score += 2
            matched_core.append(c)

    for p in positive:
        # Skip anything that is already counted as core
        if p in core:
            continue
        if term_in_text(p):
            score += 1
            matched_pos.append(p)

    matched = (matched_core[:6] + matched_pos[:6])[:12]
    return score, matched, ""


def reels_keyword_confirm(page: Page, username: str, confirm_terms: list[str]) -> bool:
    """
    Lightweight confirmation: open /{username}/reels/ and check page HTML for
    any of the confirm_terms.
    """
    reels_url = f"https://www.instagram.com/{username}/reels/"
    page.goto(reels_url, wait_until="domcontentloaded", timeout=60000)
    time.sleep(1.0)
    ensure_not_blocked(page, f"reels confirm @{username}")
    html = page.content().lower()
    return any(t in html for t in confirm_terms)


def parse_edges_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out: list[dict[str, str]] = []
    for r in rows:
        s = normalize_instagram_handle(r.get("source_username", "") or r.get("source", "") or "")
        f_u = normalize_instagram_handle(r.get("following_username", "") or r.get("following", "") or "")
        if not s or not f_u:
            continue
        out.append({"source_username": s, "following_username": f_u})
    return out


def write_profiles_csv(path: Path, profiles: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not profiles:
        # still write header based on expected fields
        fieldnames = [
            "username",
            "followers",
            "followers_raw",
            "name",
            "bio",
            "relevance_score",
            "relevance_reason",
            "matched_terms",
            "in_follower_band",
            "profile_url",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
        return

    fieldnames = list(profiles[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in profiles:
            w.writerow(row)


def main() -> None:
    ensure_dirs()
    ap = argparse.ArgumentParser(description="Filter following accounts to relevant channels")
    ap.add_argument("--input", type=Path, default=DEFAULT_CSV, help="Edges CSV")
    ap.add_argument("--min-followers", type=int, default=9000, help="Follower band min")
    ap.add_argument("--max-followers", type=int, default=80000, help="Follower band max")
    ap.add_argument("--min-score", type=int, default=2, help="Minimum relevance score (bio/name based)")
    ap.add_argument("--require-band", action="store_true", help="Require follower band match (default on)")
    ap.add_argument("--allow-missing-followers", action="store_true", help="Allow relevance even if follower count missing")
    ap.add_argument("--max-followings", type=int, default=0, help="For testing: limit unique following handles")
    ap.add_argument("--reels-confirm", action="store_true", help="Confirm relevance by scanning /reels/ page HTML")
    ap.add_argument("--reels-confirm-min-score", type=int, default=2, help="Only run reels confirmation above this score")
    ap.add_argument("--reels-max-candidates", type=int, default=0, help="For testing: max handles to run reels-confirm on (0=all)")
    ap.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT_FILE, help="Checkpoint JSON path")
    ap.add_argument("--headed", action="store_true", help="Force visible browser window")
    args = ap.parse_args()

    if not args.input.exists():
        raise SystemExit(f"Missing input: {args.input}")

    # We always require the follower band unless allow-missing-followers is enabled.
    require_band = True

    headless = (os.getenv("IG_PLAYWRIGHT_HEADLESS", "false").lower() == "true") and not args.headed

    edges = parse_edges_csv(args.input)
    if not edges:
        raise SystemExit("No valid edges rows found.")

    unique_following: list[str] = []
    seen: set[str] = set()
    for r in edges:
        u = r["following_username"]
        if u not in seen:
            seen.add(u)
            unique_following.append(u)

    if args.max_followings > 0:
        unique_following = unique_following[: args.max_followings]

    checkpoint_path: Path = args.checkpoint
    cp = load_checkpoint(checkpoint_path)
    handle_cache: dict[str, Any] = cp.get("handles", {})

    ts = time.strftime("%Y%m%d_%H%M%S")
    cleaned_out = OUT_DIR / f"{args.input.stem}_relevant_followings_{args.min_followers}_{args.max_followers}_{ts}.csv"
    profiles_out = OUT_DIR / f"{args.input.stem}_following_profiles_{ts}.csv"

    # Relevance keywords.
    # core terms count double to reduce false positives.
    # (We keep this fairly broad so we don't miss US-style pages due to strict scoring.)
    core = [
        "news",
        "breaking",
        "breaking news",
        "politics",
        "election",
        "government",
        "policy",
        "media",
        "journalist",
        "reporter",
        "editor",
        "press",
        "broadcast",
        "newsletter",
        "updates",
        "investigative",
        "investigation",
        "investigator",
        "crime",
        "court",
        "justice",
        "podcast",
        "documentary",
        "world",
        "geopolitics",
        "immigration",
        "border",
        "trade",
        "economy",
        "finance",
        "business",
        "market",
        "tech policy",
        "silicon valley",
        "supreme court",
        # Avoid short ambiguous acronyms to reduce false positives.
    ]
    positive = [
        "dc",
        "capitol",
        "capitol hill",
        "white house",
        "white house correspondent",
        "congress",
        "senate",
        "house",
        "policy analyst",
        "commentary",
        "analysis",
        "report",
        "watchdog",
        "real time",
        "alerts",
        "entertainment",
        "celebrity",
        "hollywood",
        "gossip",
        "true crime",
        "legal journalist",
        "crime beat",
        "court reporter",
        "investigative journalist",
        "newsletter",
        "podcast",
        # Sports verticals (your target audience)
        "sports",
        "nfl",
        "nba",
        "mlb",
        "nhl",
        "ufc",
        "boxing",
        "college sports",
        "sports reporter",
        "sports journalist",
        "sports updates",
        "sports media",
        # Tech/news hybrids
        "tech",
        "ai",
        # Media outlet / newsroom style phrasing
        "newsroom",
        "digital newsroom",
        "online publication",
        "media outlet",
        "online news",
        "journalism",
        "editorial",
    ]
    negative = [
        # hard spam / adult / crypto
        "giveaway",
        "onlyfans",
        "fansly",
        "sex",
        "porn",
        "crypto",
        "nft",
        "telegram",
        "whatsapp",
        "cashapp",
        "cash app",
        "pump",
        "scam",
        # conversion bait
        "dm ",
        "link in bio",
        "subscribe",
        "learn more",
    ]

    print(
        f"Filtering relevant followings: edges={len(edges)} unique_following={len(unique_following)} "
        f"headless={headless} band={args.min_followers}..{args.max_followers} min_score={args.min_score}",
        flush=True,
    )

    processed = 0
    relevant_following: dict[str, dict[str, Any]] = {}
    profiles_rows: list[dict[str, Any]] = []
    reels_confirmed_count = 0

    cfg = load_config()
    append_safe_main_name = str(cfg.get("search_append_safe_main_name", "instagram_search_accounts_append_safe.csv")).strip()
    append_safe_csv = OUT_DIR / append_safe_main_name
    need_append_to_main = True

    with sync_playwright() as p:
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(get_state_dir()),
            headless=headless,
            viewport={"width": 1280, "height": 920},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page: Page = context.new_page()
        wait_for_manual_login(page)
        ensure_not_blocked(page, "relevant following filter init")

        for idx, u in enumerate(unique_following, start=1):
            if u in handle_cache:
                cached = handle_cache[u]
                # keep for profiles output too
                profiles_rows.append(cached)
                if cached.get("relevant") == "yes":
                    relevant_following[u] = cached
                continue

            # Fetch profile meta
            try:
                profile_url = to_profile_url(u)
                page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
                ensure_not_blocked(page, f"relevant filter @{u}")
                time.sleep(0.9 + random.uniform(0.2, 0.6))

                meta = parse_profile_meta(page, u)
                name = meta.get("name") or ""
                bio = meta.get("bio") or ""
                followers_raw = meta.get("followers") or ""
                followers_int = normalize_followers_value(followers_raw) if followers_raw else None

                # Fallback if followers missing in meta
                if followers_int is None:
                    html = page.content()
                    fc = follower_count_from_html(html)
                    if fc is not None:
                        followers_int = fc
                        followers_raw = followers_raw or str(fc)

                in_band = ""
                if followers_int is not None:
                    in_band = "yes" if args.min_followers <= followers_int <= args.max_followers else "no"

                score, matched_terms, neg_reason = score_relevance(
                    name=str(name) if name else "",
                    bio=str(bio) if bio else "",
                    positive=positive,
                    core=core,
                    negative=negative,
                )

                relevant = "no"
                relevance_reason = ""
                reels_ok = ""
                if score >= args.min_score and score != -1:
                    if require_band:
                        if in_band == "yes" or args.allow_missing_followers:
                            relevant = "yes"
                    else:
                        relevant = "yes"

                if neg_reason:
                    relevance_reason = neg_reason
                elif relevant == "yes":
                    relevance_reason = f"score={score};terms={','.join(matched_terms)}"
                else:
                    relevance_reason = f"score={score};band={in_band}"

                if (
                    args.reels_confirm
                    and relevant == "yes"
                    and score >= args.reels_confirm_min_score
                    and (args.reels_max_candidates <= 0 or reels_confirmed_count < args.reels_max_candidates)
                ):
                    # Run an extra confirmation against what their reels are showing.
                    # This helps avoid “keywordy bios” from personal accounts.
                    confirm_terms = [
                        "news",
                        "breaking",
                        "politics",
                        "election",
                        "media",
                        "journalist",
                        "reporter",
                        "court",
                        "crime",
                        "investigation",
                        "podcast",
                        "hollywood",
                        "celebrity",
                        "sports",
                        "entertainment",
                        "world",
                        "government",
                    ]
                    try:
                        reels_hit = reels_keyword_confirm(page, u, confirm_terms)
                        reels_ok = "reels_ok" if reels_hit else "reels_miss"
                        if not reels_hit:
                            relevant = "no"
                            relevance_reason = f"relevance_lost:{relevance_reason};reels={reels_ok}"
                    except Exception as exc:
                        # If reels page fails to load, keep the bio-based decision.
                        reels_ok = f"reels_error:{exc}"

                row = {
                    "username": u,
                    "followers": followers_int if followers_int is not None else "",
                    "followers_raw": followers_raw or "",
                    "name": name,
                    "bio": bio,
                    "relevance_score": score,
                    "relevance_reason": relevance_reason,
                    "matched_terms": ",".join(matched_terms),
                    "in_follower_band": in_band,
                    "profile_url": profile_url,
                    "relevant": relevant,
                    "cached_at_epoch": int(time.time()),
                }
            except Exception as exc:
                row = {
                    "username": u,
                    "followers": "",
                    "followers_raw": "",
                    "name": "",
                    "bio": "",
                    "relevance_score": "",
                    "relevance_reason": f"fetch_error:{str(exc)}",
                    "matched_terms": "",
                    "in_follower_band": "",
                    "profile_url": to_profile_url(u),
                    "relevant": "no",
                    "cached_at_epoch": int(time.time()),
                }

            handle_cache[u] = row
            profiles_rows.append(row)
            if row.get("relevant") == "yes":
                relevant_following[u] = row

            processed += 1
            if processed % 20 == 0:
                save_checkpoint(checkpoint_path, {"handles": handle_cache})
                print(f"  fetched={processed}/{len(unique_following)} latest_cache=@{u} relevant={row.get('relevant')}", flush=True)

        context.close()

    # Write profiles cache
    write_profiles_csv(profiles_out, profiles_rows)

    # Filter edges rows based on following relevance
    cleaned_rows: list[dict[str, Any]] = []
    for r in edges:
        fu = r["following_username"]
        rel = relevant_following.get(fu)
        if not rel:
            continue
        cleaned_rows.append(
            {
                **r,
                "following_profile_url": rel.get("profile_url", ""),
                "following_followers": rel.get("followers", ""),
                "following_followers_raw": rel.get("followers_raw", ""),
                "following_name": rel.get("name", ""),
                "following_bio": rel.get("bio", ""),
                "relevance_score": rel.get("relevance_score", ""),
                "relevance_reason": rel.get("relevance_reason", ""),
                "matched_terms": rel.get("matched_terms", ""),
                "in_follower_band": rel.get("in_follower_band", ""),
            }
        )

    cleaned_out.parent.mkdir(parents=True, exist_ok=True)
    with cleaned_out.open("w", newline="", encoding="utf-8") as f:
        if cleaned_rows:
            fieldnames = list(cleaned_rows[0].keys())
        else:
            fieldnames = list(
                {
                    "source_username": "",
                    "following_username": "",
                }.keys()
            )
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in cleaned_rows:
            w.writerow(row)

    save_checkpoint(checkpoint_path, {"handles": handle_cache})
    # Append relevant profiles into your main append-safe CSV (safe merge, no data loss).
    if need_append_to_main and relevant_following:
        base_map = read_profiles_csv(append_safe_csv)
        incoming: dict[str, dict[str, Any]] = {}
        for u, rel in relevant_following.items():
            followers_int = rel.get("followers")
            if followers_int == "":
                continue
            try:
                followers_int_norm = int(float(str(followers_int).strip()))
            except Exception:
                followers_int_norm = None
            incoming[u] = {
                "profile_url": rel.get("profile_url") or to_profile_url(u),
                "username": u,
                "name": rel.get("name") or "",
                "followers": followers_int_norm,
                "followers_raw": rel.get("followers_raw") or "",
                "bio": rel.get("bio") or "",
                "is_private": None,
                "is_verified": None,
                "source_keywords": f"following_relevance:{rel.get('matched_terms','') or rel.get('relevance_reason','')}",
                "error": "",
            }
        merged = merge_profiles_maps(base_map, incoming)
        write_live_csv_to_path(merged, append_safe_csv)

        print(f"Appended relevant profiles -> {append_safe_csv}", flush=True)

    print(f"Done. Cleaned edges -> {cleaned_out} ({len(cleaned_rows)} rows)", flush=True)
    print(f"Profiles -> {profiles_out} ({len(profiles_rows)} handles)", flush=True)
    print(f"Checkpoint -> {checkpoint_path}", flush=True)


if __name__ == "__main__":
    main()

