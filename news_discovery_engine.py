#!/usr/bin/env python3
"""Append-safe, checkpointed Instagram discovery for news verticals."""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from playwright.sync_api import BrowserContext, Page, sync_playwright

from instagram_search_browser_scraper import (
    ROOT,
    ensure_not_blocked,
    get_state_dir,
    normalize_followers_value,
    normalize_username,
    wait_for_manual_login,
)

RAW_DIR = ROOT / "data" / "raw"
OUT_DIR = ROOT / "data" / "out"
DEFAULT_CONFIG = ROOT / "news_discovery_config.json"

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


def load_cfg(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"Missing config: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"processed_seeds": [], "processed_queries": [], "appended_usernames": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"processed_seeds": [], "processed_queries": [], "appended_usernames": []}


def save_checkpoint(path: Path, processed_seeds: set[str], processed_queries: set[str], appended: set[str]) -> None:
    payload = {
        "processed_seeds": sorted(processed_seeds),
        "processed_queries": sorted(processed_queries),
        "appended_usernames": sorted(appended),
        "updated_at_epoch": int(time.time()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def read_existing_usernames(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            u = normalize_username(str(row.get("username", "")))
            if u:
                out.add(u)
    return out


def append_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames = [
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
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = (not path.exists()) or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            w.writeheader()
        w.writerows(rows)


def normalize_query_key(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())


def collect_profiles_via_topsearch(page: Page, query: str, max_accounts: int) -> dict[str, dict[str, Any]]:
    encoded = query.replace(" ", "%20")
    url = f"https://www.instagram.com/web/search/topsearch/?query={encoded}&count={max_accounts}"
    resp = page.request.get(url, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=45000)
    if resp.status != 200:
        return {}
    try:
        payload = resp.json()
    except Exception:
        return {}
    users = payload.get("users", []) if isinstance(payload, dict) else []
    out: dict[str, dict[str, Any]] = {}
    for item in users if isinstance(users, list) else []:
        user = item.get("user", {}) if isinstance(item, dict) else {}
        u = normalize_username(str(user.get("username", "")))
        if not u or u in RESERVED_HANDLES:
            continue
        out[u] = {
            "profile_url": f"https://www.instagram.com/{u}/",
            "username": u,
            "name": user.get("full_name") or "",
            "followers": normalize_followers_value(user.get("follower_count")),
            "followers_raw": user.get("follower_count") or "",
            "bio": user.get("biography") or "",
            "is_private": user.get("is_private"),
            "is_verified": user.get("is_verified"),
            "source_keywords": query,
            "error": "",
        }
        if len(out) >= max_accounts:
            break
    return out


def collect_usernames_via_ddg(query: str, max_accounts: int) -> list[str]:
    try:
        resp = requests.get(
            f"https://duckduckgo.com/html/?q={quote(query + ' site:instagram.com')}",
            timeout=25,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
    except Exception:
        return []
    found = re.findall(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/?", resp.text)
    out: list[str] = []
    seen: set[str] = set()
    for raw in found:
        u = normalize_username(raw)
        if not u or u in RESERVED_HANDLES or u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= max_accounts:
            break
    return out


def collect_similar_usernames_from_profile_page(page: Page, seed_username: str, max_accounts: int) -> list[str]:
    try:
        page.goto(f"https://www.instagram.com/{seed_username}/", wait_until="domcontentloaded", timeout=45000)
        ensure_not_blocked(page, f"seed @{seed_username}")
    except Exception:
        return []
    time.sleep(1.5)
    hrefs = page.eval_on_selector_all("a[href^='/']", "els => els.map(e => e.getAttribute('href')).filter(Boolean)")
    out: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        m = re.match(r"^/([A-Za-z0-9._]+)/?$", str(href))
        if not m:
            continue
        u = normalize_username(m.group(1))
        if not u or u in RESERVED_HANDLES or u == seed_username or u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= max_accounts:
            break
    return out


def relevance_ok(row: dict[str, Any], include_terms: list[str], exclude_terms: list[str]) -> bool:
    # Include check must run against the account's own text (username/name/bio) only.
    # Previously source_keywords was in the haystack too, which contained the query itself
    # (e.g. "query:us politics news") and silently bypassed every include filter.
    username = str(row.get("username", "")).lower()
    name = str(row.get("name", "")).lower()
    bio = str(row.get("bio", "")).lower()
    source_keywords = str(row.get("source_keywords", "")).lower()
    hay_profile = f"{username} {name} {bio}"
    hay_all = f"{hay_profile} {source_keywords}"

    # Exclude: scan everything, so bad seed/query contexts (e.g. "cricket") also reject.
    if exclude_terms and any(t in hay_all for t in exclude_terms):
        return False

    # Include: only enforce when we actually have profile text to match against.
    # Minimal rows (similar-profile scraping, DDG fallback) have empty bio+name and would
    # otherwise be wrongly rejected; pass them through for the downstream scoring pipeline.
    has_profile_text = bool(name.strip() or bio.strip())
    if include_terms and has_profile_text:
        if not any(t in hay_profile for t in include_terms):
            return False
    return True


def run(config_path: Path, reset_checkpoint: bool = False) -> None:
    cfg = load_cfg(config_path)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    out_prefix = str(cfg.get("output_prefix", "news")).strip() or "news"
    candidate_csv = (ROOT / str(cfg.get("candidate_output_csv", f"data/out/{out_prefix}_candidates_append.csv"))).resolve()
    checkpoint_path = (ROOT / str(cfg.get("checkpoint_relative_path", f"data/raw/{out_prefix}_discovery_checkpoint.json"))).resolve()
    if reset_checkpoint and checkpoint_path.exists():
        checkpoint_path.unlink()
        print(f"Reset checkpoint removed: {checkpoint_path}", flush=True)

    max_accounts_per_query = int(cfg.get("max_accounts_per_query", 25))
    min_followers = int(cfg.get("min_followers", 10000))
    max_followers = int(cfg.get("max_followers", 70000))
    # Soft-band tolerance: topsearch's reported follower_count is often stale or rounded.
    # Rows outside the hard band by more than this fraction are still dropped; rows within
    # it get forwarded to the scoring pipeline which has its own precise band check.
    band_slack_fraction = float(cfg.get("band_slack_fraction", 0.5))
    # Skip the seed-loop entirely for configs where every seed is a mega-publisher
    # (politico, tmz, etc.) — topsearching those usernames only returns more mega accounts,
    # and the similar-profile HTML scrape returns reserved handles. Those configs should
    # rely on the query phase here, plus following_graph_news_expander.py for seed-based
    # graph expansion, instead of wasting cycles on zero-yield seeds.
    skip_seed_phase = bool(cfg.get("skip_seed_phase", False))
    include_terms = [str(x).strip().lower() for x in cfg.get("include_terms", []) if str(x).strip()]
    exclude_terms = [str(x).strip().lower() for x in cfg.get("exclude_terms", []) if str(x).strip()]
    inter_query_sleep = float(cfg.get("inter_query_sleep_seconds", 0.7))
    inter_seed_sleep = float(cfg.get("inter_seed_sleep_seconds", 1.0))
    state_dir_cfg = str(cfg.get("playwright_state_dir", ".ig_browser_state")).strip()
    state_dir = (ROOT / state_dir_cfg).resolve() if not Path(state_dir_cfg).is_absolute() else Path(state_dir_cfg)
    headless = bool(cfg.get("headless", False))

    # Soft follower band — anything within slack_fraction of the hard band is accepted
    # for later precise scoring. Below min_followers is always dropped (we never want
    # sub-floor accounts). Above max_followers, we accept up to max * (1 + slack) so
    # topsearch rounding noise near the ceiling doesn't silently discard yield.
    soft_min = min_followers
    soft_max = int(max_followers * (1.0 + band_slack_fraction))

    def follower_band_ok(followers: Any) -> bool:
        if not isinstance(followers, int):
            return True  # unknown count → forward to scoring stage
        return soft_min <= followers <= soft_max

    seed_accounts = [normalize_username(str(x)) for x in cfg.get("seed_accounts", []) if str(x).strip()]
    discovery_queries = [str(x).strip() for x in cfg.get("discovery_queries", []) if str(x).strip()]
    seed_accounts = [x for x in seed_accounts if x and x not in RESERVED_HANDLES]
    discovery_queries = list(dict.fromkeys(discovery_queries))

    cp = load_checkpoint(checkpoint_path)
    processed_seeds: set[str] = set(cp.get("processed_seeds", []))
    processed_queries: set[str] = set(cp.get("processed_queries", []))
    appended: set[str] = set(cp.get("appended_usernames", []))
    existing = read_existing_usernames(candidate_csv) | appended

    print(
        f"Discovery start ({out_prefix}): seeds={len(seed_accounts)}"
        f"{' (SKIPPED)' if skip_seed_phase else ''}, "
        f"queries={len(discovery_queries)}, existing={len(existing)}, "
        f"band={soft_min}..{soft_max} (hard {min_followers}..{max_followers})",
        flush=True,
    )

    with sync_playwright() as p:
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(state_dir if state_dir else get_state_dir()),
            headless=headless,
            viewport={"width": 1440, "height": 920},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()
        wait_for_manual_login(page)

        # Query phase runs FIRST — this is where the 9k-55k yield actually comes from.
        # Putting it ahead of the seed phase means the user sees results immediately
        # instead of waiting through a dead seed phase on mega-publisher configs.
        for i, query in enumerate(discovery_queries, start=1):
            qkey = normalize_query_key(query)
            if qkey in processed_queries:
                continue
            found_rows = collect_profiles_via_topsearch(page, query, max_accounts=max_accounts_per_query)
            if not found_rows:
                for u in collect_usernames_via_ddg(query, max_accounts=max_accounts_per_query):
                    found_rows[u] = {
                        "profile_url": f"https://www.instagram.com/{u}/",
                        "username": u,
                        "name": "",
                        "followers": None,
                        "followers_raw": "",
                        "bio": "",
                        "is_private": "",
                        "is_verified": "",
                        "source_keywords": f"ddg:{query}",
                        "error": "ddg_fallback_unenriched",
                    }
            batch: list[dict[str, Any]] = []
            for u, row in found_rows.items():
                if u in existing:
                    continue
                if not follower_band_ok(row.get("followers")):
                    continue
                row["source_keywords"] = f"query:{query}|{row.get('source_keywords','')}"
                if not relevance_ok(row, include_terms, exclude_terms):
                    continue
                existing.add(u)
                appended.add(u)
                batch.append(row)
            append_rows(candidate_csv, batch)
            processed_queries.add(qkey)
            save_checkpoint(checkpoint_path, processed_seeds, processed_queries, appended)
            print(f"[query {i}/{len(discovery_queries)}] {query} added={len(batch)} total={len(appended)}", flush=True)
            time.sleep(inter_query_sleep + random.uniform(0.1, 0.5))

        if skip_seed_phase:
            print(
                "Seed phase skipped (skip_seed_phase=true). "
                "Use following_graph_news_expander.py for seed-based expansion.",
                flush=True,
            )
        else:
            for i, seed in enumerate(seed_accounts, start=1):
                if seed in processed_seeds:
                    continue
                discovered = collect_similar_usernames_from_profile_page(page, seed, max_accounts=max_accounts_per_query)
                found_rows = collect_profiles_via_topsearch(page, seed, max_accounts=max_accounts_per_query)
                for u in discovered:
                    found_rows.setdefault(
                        u,
                        {
                            "profile_url": f"https://www.instagram.com/{u}/",
                            "username": u,
                            "name": "",
                            "followers": None,
                            "followers_raw": "",
                            "bio": "",
                            "is_private": "",
                            "is_verified": "",
                            "source_keywords": f"similar_to:{seed}",
                            "error": "similar_profile_minimal",
                        },
                    )
                batch: list[dict[str, Any]] = []
                for u, row in found_rows.items():
                    if u in existing:
                        continue
                    if not follower_band_ok(row.get("followers")):
                        continue
                    row["source_keywords"] = f"seed:{seed}|{row.get('source_keywords','')}"
                    if not relevance_ok(row, include_terms, exclude_terms):
                        continue
                    existing.add(u)
                    appended.add(u)
                    batch.append(row)
                append_rows(candidate_csv, batch)
                processed_seeds.add(seed)
                save_checkpoint(checkpoint_path, processed_seeds, processed_queries, appended)
                print(f"[seed {i}/{len(seed_accounts)}] @{seed} added={len(batch)} total={len(appended)}", flush=True)
                time.sleep(inter_seed_sleep + random.uniform(0.1, 0.5))

        context.close()

    print(f"Discovery done. Candidate CSV -> {candidate_csv}", flush=True)
    print(f"Checkpoint -> {checkpoint_path}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Append-safe Instagram news discovery engine")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Config JSON path")
    parser.add_argument("--reset-checkpoint", action="store_true", help="Delete checkpoint before run")
    args = parser.parse_args()
    cfg_path = Path(str(args.config)).expanduser()
    if not cfg_path.is_absolute():
        cfg_path = (ROOT / cfg_path).resolve()
    run(cfg_path, reset_checkpoint=args.reset_checkpoint)
