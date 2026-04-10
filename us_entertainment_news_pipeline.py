#!/usr/bin/env python3
"""High-quality US Hollywood / pop-culture news account pipeline (append-safe + checkpointed)."""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from playwright.sync_api import BrowserContext, Page, sync_playwright

from instagram_search_browser_scraper import (
    ROOT,
    ensure_not_blocked,
    get_state_dir,
    load_config as load_main_config,
    merge_profiles_maps,
    normalize_followers_value,
    normalize_username,
    parse_profile_meta,
    read_profiles_csv,
    wait_for_manual_login,
    write_live_csv_to_path,
)

OUT_DIR = ROOT / "data" / "out"
RAW_DIR = ROOT / "data" / "raw"
CONFIG_PATH = ROOT / "us_entertainment_news_config.json"

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


def load_cfg(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise RuntimeError(f"Missing config: {config_path}")
    return json.loads(config_path.read_text(encoding="utf-8"))


def load_checkpoint(checkpoint_path: Path) -> dict[str, Any]:
    if not checkpoint_path.exists():
        return {"profiles": {}, "processed": []}
    try:
        return json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except Exception:
        return {"profiles": {}, "processed": []}


def save_checkpoint(
    checkpoint_path: Path, profiles: dict[str, dict[str, Any]], processed: set[str]
) -> None:
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "profiles": profiles,
        "processed": sorted(processed),
        "updated_at_epoch": int(time.time()),
    }
    checkpoint_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def read_candidate_usernames(search_csv: Path, reels_csv: Path) -> tuple[list[str], dict[str, set[str]]]:
    by_u: dict[str, set[str]] = {}

    if search_csv.exists():
        with search_csv.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                u = normalize_username(str(row.get("username", "")))
                if not u:
                    continue
                by_u.setdefault(u, set()).add("search")

    if reels_csv.exists():
        with reels_csv.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                u = normalize_username(str(row.get("username", "")))
                if not u:
                    continue
                kw = str(row.get("source_keyword", "")).strip().lower()
                if kw:
                    by_u.setdefault(u, set()).add(f"reels:{kw}")
                else:
                    by_u.setdefault(u, set()).add("reels")

    usernames = sorted(by_u.keys())
    return usernames, by_u


def text_has_any(hay: str, terms: list[str]) -> bool:
    return any(t in hay for t in terms)


def term_hits(hay: str, terms: list[str]) -> list[str]:
    found: list[str] = []
    for t in terms:
        tt = str(t).strip().lower()
        if not tt:
            continue
        if tt in hay:
            found.append(tt)
    return found


def score_profile(
    username: str,
    name: str,
    bio: str,
    source_keywords: str,
    include_terms: list[str],
    soft_positive: list[str],
    exclude_terms: list[str],
) -> tuple[int, str]:
    # Excludes: handle + bio + display name + how we found them (reels keyword etc.),
    # so sports accounts in the queue are dropped before they pollute outputs.
    hay_exclude = f"{username} {name} {bio} {source_keywords}".lower()
    if text_has_any(hay_exclude, exclude_terms):
        return -5, "exclude_term"
    # Include/soft: profile-only — no credit from reel search terms.
    hay_core = f"{username} {name} {bio}".lower()
    score = 0
    if text_has_any(hay_core, include_terms):
        score += 4
    for t in soft_positive:
        if t in hay_core:
            score += 1
    return score, "ok"


def safe_external_link(page: Page, username: str) -> str:
    # Attempt multiple selectors; Instagram changes this frequently.
    selectors = [
        "header a[rel='me']",
        "header a[target='_blank']",
        "main a[href^='http']",
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        if loc.count() == 0:
            continue
        href = loc.get_attribute("href")
        if not href:
            continue
        if "instagram.com" in href:
            continue
        return href.strip()
    # Fallback from page html
    html = page.content()
    # naive extract of first external url not pointing to instagram
    for m in re.findall(r'https?://[^"\'\s<>]+', html):
        if "instagram.com" in m:
            continue
        return m
    return ""


def extract_emails_from_text(text: str, max_count: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in EMAIL_RE.findall(text or ""):
        e = m.strip().lower().rstrip(".,;:!?")
        if e in seen:
            continue
        seen.add(e)
        out.append(e)
        if len(out) >= max_count:
            break
    return out


def fetch_site_emails(url: str, timeout_s: int, max_count: int) -> list[str]:
    if not url:
        return []
    try:
        resp = requests.get(url, timeout=timeout_s, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code >= 400:
            return []
        html = resp.text[:250000]
        emails = extract_emails_from_text(html, max_count=max_count)
        if emails:
            return emails

        # Fallback: try contact path on same host
        p = urlparse(url)
        if not p.scheme or not p.netloc:
            return []
        contact_url = f"{p.scheme}://{p.netloc}/contact"
        resp2 = requests.get(contact_url, timeout=timeout_s, headers={"User-Agent": "Mozilla/5.0"})
        if resp2.status_code < 400:
            return extract_emails_from_text(resp2.text[:250000], max_count=max_count)
    except Exception:
        return []
    return []


def profile_row_for_append(row: dict[str, Any], source_keywords: str) -> dict[str, Any]:
    return {
        "profile_url": row.get("profile_url") or "",
        "username": row.get("username") or "",
        "name": row.get("name") or "",
        "followers": row.get("followers"),
        "followers_raw": row.get("followers_raw") or "",
        "bio": row.get("bio") or "",
        "is_private": row.get("is_private"),
        "is_verified": row.get("is_verified"),
        "source_keywords": source_keywords,
        "error": row.get("error") or "",
    }


def run(*, config_path: Path = CONFIG_PATH, reset_checkpoint: bool = False) -> None:
    cfg = load_cfg(config_path)
    main_cfg = load_main_config()

    ck_rel = str(cfg.get("checkpoint_relative_path", "") or "").strip()
    checkpoint_path = (ROOT / ck_rel).resolve() if ck_rel else _DEFAULT_CHECKPOINT.resolve()
    if reset_checkpoint and checkpoint_path.exists():
        checkpoint_path.unlink()
        print(f"Reset checkpoint removed: {checkpoint_path}", flush=True)

    out_prefix = str(cfg.get("output_prefix", "us_hollywood_popculture_news") or "us_hollywood_popculture_news").strip()
    source_tag = str(cfg.get("vertical", out_prefix) or out_prefix).strip()

    follower_min = int(cfg.get("follower_min", 10000))
    follower_max = int(cfg.get("follower_max", 70000))
    high_quality_min_score = int(cfg.get("high_quality_min_score", 6))
    review_min_score = int(cfg.get("review_min_score", 4))
    require_public = bool(cfg.get("require_public", True))
    high_quality_require_email = bool(cfg.get("high_quality_require_email", False))
    high_quality_require_external_url = bool(cfg.get("high_quality_require_external_url", False))
    include_terms = [str(x).strip().lower() for x in cfg.get("include_terms", []) if str(x).strip()]
    soft_positive = [str(x).strip().lower() for x in cfg.get("soft_positive_terms", []) if str(x).strip()]
    exclude_terms = [str(x).strip().lower() for x in cfg.get("exclude_terms", []) if str(x).strip()]
    hard_include_terms = [str(x).strip().lower() for x in cfg.get("hard_include_terms", []) if str(x).strip()]
    hard_include_min_hits = int(cfg.get("hard_include_min_hits", 1))
    flush_every = int(cfg.get("flush_every", 20))
    sleep_s = float(cfg.get("inter_profile_sleep_seconds", 0.8))
    site_timeout = int(cfg.get("site_fetch_timeout_seconds", 15))
    email_max = int(cfg.get("email_max_per_profile", 4))
    allow_missing_followers_to_review = bool(cfg.get("allow_missing_followers_to_review", True))
    us_require_match = bool(cfg.get("us_require_match", True))
    us_min_hits = int(cfg.get("us_min_hits", 1))
    us_exclude_hits_reject = bool(cfg.get("us_exclude_hits_reject", True))
    us_terms = [str(x).strip().lower() for x in cfg.get("us_terms", []) if str(x).strip()]
    non_us_terms = [str(x).strip().lower() for x in cfg.get("non_us_terms", []) if str(x).strip()]

    search_csv = (ROOT / str(cfg.get("input_search_csv", "data/out/instagram_search_accounts_append_safe.csv"))).resolve()
    reels_csv = (ROOT / str(cfg.get("input_reels_csv", "data/out/instagram_reels_for_you_live.csv"))).resolve()

    append_safe_main_name = str(main_cfg.get("search_append_safe_main_name", "instagram_search_accounts_append_safe.csv")).strip()
    append_safe_csv = OUT_DIR / append_safe_main_name

    usernames, source_map = read_candidate_usernames(search_csv, reels_csv)
    if not usernames:
        raise RuntimeError("No candidate usernames found from configured input CSVs.")

    ts = time.strftime("%Y%m%d_%H%M%S")
    run_all_csv = OUT_DIR / f"{out_prefix}_profiles_run_{ts}.csv"
    run_high_csv = OUT_DIR / f"{out_prefix}_high_quality_{ts}.csv"
    run_review_csv = OUT_DIR / f"{out_prefix}_review_queue_{ts}.csv"
    run_emails_csv = OUT_DIR / f"{out_prefix}_emails_{ts}.csv"
    live_all_csv = OUT_DIR / f"{out_prefix}_profiles_live.csv"
    live_high_csv = OUT_DIR / f"{out_prefix}_high_quality_live.csv"
    live_review_csv = OUT_DIR / f"{out_prefix}_review_queue_live.csv"
    live_emails_csv = OUT_DIR / f"{out_prefix}_emails_live.csv"

    cp = load_checkpoint(checkpoint_path)
    profiles_by_username: dict[str, dict[str, Any]] = cp.get("profiles", {})
    processed: set[str] = set(cp.get("processed", []))

    state_dir_cfg = str(cfg.get("playwright_state_dir", ".ig_browser_state")).strip()
    state_dir = (ROOT / state_dir_cfg).resolve() if not Path(state_dir_cfg).is_absolute() else Path(state_dir_cfg)
    headless = bool(cfg.get("headless", False))

    print(
        f"Hollywood/pop-culture news pipeline ({source_tag}): candidates={len(usernames)}, "
        f"already_processed={len(processed)}, band={follower_min}..{follower_max}, headless={headless}",
        flush=True,
    )
    print(f"Outputs prefix={out_prefix}, checkpoint={checkpoint_path}", flush=True)

    def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "profile_url",
            "username",
            "name",
            "followers",
            "followers_raw",
            "bio",
            "is_private",
            "is_verified",
            "external_url",
            "email",
            "email_source",
            "quality_score",
            "quality_status",
            "quality_reason",
            "us_hits",
            "non_us_hits",
            "source_keywords",
            "error",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def persist_live_outputs() -> None:
        all_rows = sorted(profiles_by_username.values(), key=lambda r: str(r.get("username", "")))
        high_rows = [r for r in all_rows if str(r.get("quality_status")) == "high_quality"]
        review_rows = [r for r in all_rows if str(r.get("quality_status")) == "review"]
        email_rows = [r for r in all_rows if str(r.get("email", "")).strip()]
        write_rows(live_all_csv, all_rows)
        write_rows(live_high_csv, high_rows)
        write_rows(live_review_csv, review_rows)
        write_rows(live_emails_csv, email_rows)

        # Keep append-safe main updated side-by-side during run.
        base_map = read_profiles_csv(append_safe_csv)
        incoming = {
            r["username"]: profile_row_for_append(
                r, source_keywords=f"{source_tag}|{r.get('source_keywords','')}"
            )
            for r in high_rows
            if r.get("username")
        }
        merged = merge_profiles_maps(base_map, incoming)
        write_live_csv_to_path(merged, append_safe_csv)

    with sync_playwright() as p:
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(state_dir if state_dir else get_state_dir()),
            headless=headless,
            viewport={"width": 1440, "height": 920},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()
        # Keep login flow intact (same logic as working scrapers).
        wait_for_manual_login(page)
        ensure_not_blocked(page, "entertainment pipeline init")

        for i, u in enumerate(usernames, start=1):
            if u in processed:
                continue
            profile_url = f"https://www.instagram.com/{u}/"
            row: dict[str, Any] = {
                "username": u,
                "profile_url": profile_url,
                "name": "",
                "bio": "",
                "followers": None,
                "followers_raw": "",
                "is_private": "",
                "is_verified": "",
                "external_url": "",
                "email": "",
                "email_source": "",
                "quality_score": 0,
                "quality_status": "review",
                "quality_reason": "",
                "source_keywords": "|".join(sorted(source_map.get(u, set()))[:10]),
                "error": "",
            }
            try:
                page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
                ensure_not_blocked(page, f"profile @{u}")
                time.sleep(1.0 + random.uniform(0.2, 0.7))

                meta = parse_profile_meta(page, u)
                row["name"] = str(meta.get("name") or "")
                row["bio"] = str(meta.get("bio") or "")
                row["followers_raw"] = str(meta.get("followers") or "")
                row["followers"] = normalize_followers_value(row["followers_raw"]) if row["followers_raw"] else None

                # Fallback follower parse from HTML blobs
                if row["followers"] is None:
                    html = page.content()
                    m = re.search(r'"edge_followed_by"\s*:\s*\{\s*"count"\s*:\s*(\d+)', html)
                    if m:
                        row["followers"] = int(m.group(1))
                        row["followers_raw"] = row["followers_raw"] or str(row["followers"])

                html_l = page.content().lower()
                row["is_private"] = "true" if "this account is private" in html_l else "false"
                row["is_verified"] = "true" if '"is_verified":true' in html_l else "false"
                row["external_url"] = safe_external_link(page, u)

                score, reason = score_profile(
                    username=u,
                    name=row["name"],
                    bio=row["bio"],
                    source_keywords=str(row.get("source_keywords") or ""),
                    include_terms=include_terms,
                    soft_positive=soft_positive,
                    exclude_terms=exclude_terms,
                )
                row["quality_score"] = score

                followers = row.get("followers")
                in_band = isinstance(followers, int) and follower_min <= followers <= follower_max
                public_ok = str(row.get("is_private", "")).lower() != "true"
                us_hay = " ".join(
                    [
                        u.lower(),
                        str(row.get("name", "")).lower(),
                        str(row.get("bio", "")).lower(),
                        str(row.get("external_url", "")).lower(),
                        str(row.get("source_keywords", "")).lower(),
                    ]
                )
                us_hits = term_hits(us_hay, us_terms)
                non_us_hits = term_hits(us_hay, non_us_terms)
                hard_hits = term_hits(us_hay, hard_include_terms)
                row["us_hits"] = ",".join(us_hits[:8])
                row["non_us_hits"] = ",".join(non_us_hits[:8])
                us_ok = (len(us_hits) >= max(1, us_min_hits)) if us_require_match else True
                hard_ok = len(hard_hits) >= max(1, hard_include_min_hits)

                if score < 0:
                    row["quality_status"] = "rejected"
                    row["quality_reason"] = reason
                elif us_exclude_hits_reject and non_us_hits and not us_hits:
                    row["quality_status"] = "rejected"
                    row["quality_reason"] = "non_us_signal"
                elif not us_ok:
                    row["quality_status"] = "rejected"
                    row["quality_reason"] = "us_match_fail"
                elif not hard_ok:
                    row["quality_status"] = "rejected"
                    row["quality_reason"] = "hollywood_match_fail"
                elif (followers is None) and allow_missing_followers_to_review:
                    row["quality_status"] = "review"
                    row["quality_reason"] = "missing_followers"
                elif not in_band:
                    row["quality_status"] = "rejected"
                    row["quality_reason"] = "follower_band_fail"
                elif require_public and not public_ok:
                    row["quality_status"] = "rejected"
                    row["quality_reason"] = "private_account"
                elif score >= high_quality_min_score:
                    if high_quality_require_external_url and not str(row.get("external_url") or "").strip():
                        row["quality_status"] = "review"
                        row["quality_reason"] = "missing_external_url"
                    elif high_quality_require_email and not str(row.get("email") or "").strip():
                        row["quality_status"] = "review"
                        row["quality_reason"] = "missing_email"
                    else:
                        row["quality_status"] = "high_quality"
                        row["quality_reason"] = "strict_pass"
                elif score >= review_min_score:
                    row["quality_status"] = "review"
                    row["quality_reason"] = "borderline_score"
                else:
                    row["quality_status"] = "rejected"
                    row["quality_reason"] = "low_score"

                # Email extraction: bio first, then site.
                bio_emails = extract_emails_from_text(row["bio"], max_count=email_max)
                if bio_emails:
                    row["email"] = "|".join(bio_emails[:email_max])
                    row["email_source"] = "instagram_bio"
                else:
                    site_emails = fetch_site_emails(row["external_url"], timeout_s=site_timeout, max_count=email_max)
                    if site_emails:
                        row["email"] = "|".join(site_emails[:email_max])
                        row["email_source"] = "external_site"
            except Exception as exc:
                row["error"] = str(exc)
                row["quality_status"] = "review"
                row["quality_reason"] = "fetch_error"

            profiles_by_username[u] = row
            processed.add(u)

            if len(processed) % max(1, flush_every) == 0:
                save_checkpoint(checkpoint_path, profiles_by_username, processed)
                persist_live_outputs()
                print(
                    f"processed={len(processed)}/{len(usernames)} latest=@{u} status={row.get('quality_status')}",
                    flush=True,
                )
            time.sleep(sleep_s + random.uniform(0.1, 0.5))

        context.close()

    # Final checkpoint
    save_checkpoint(checkpoint_path, profiles_by_username, processed)
    persist_live_outputs()

    all_rows = sorted(profiles_by_username.values(), key=lambda r: str(r.get("username", "")))
    high_rows = [r for r in all_rows if str(r.get("quality_status")) == "high_quality"]
    review_rows = [r for r in all_rows if str(r.get("quality_status")) == "review"]
    email_rows = [r for r in all_rows if str(r.get("email", "")).strip()]

    write_rows(run_all_csv, all_rows)
    write_rows(run_high_csv, high_rows)
    write_rows(run_review_csv, review_rows)
    write_rows(run_emails_csv, email_rows)

    # Append-safe merge into main profiles file (high-quality only).
    base_map = read_profiles_csv(append_safe_csv)
    incoming = {
        r["username"]: profile_row_for_append(
            r, source_keywords=f"{source_tag}|{r.get('source_keywords','')}"
        )
        for r in high_rows
        if r.get("username")
    }
    merged = merge_profiles_maps(base_map, incoming)
    write_live_csv_to_path(merged, append_safe_csv)

    print(f"Wrote all profiles: {run_all_csv} ({len(all_rows)})", flush=True)
    print(f"Wrote high quality: {run_high_csv} ({len(high_rows)})", flush=True)
    print(f"Wrote review queue: {run_review_csv} ({len(review_rows)})", flush=True)
    print(f"Wrote email rows: {run_emails_csv} ({len(email_rows)})", flush=True)
    print(f"Appended high-quality profiles -> {append_safe_csv}", flush=True)
    print(f"Checkpoint -> {checkpoint_path}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hollywood / pop-culture news profile pipeline")
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help="Path to vertical config JSON (default: us_entertainment_news_config.json).",
    )
    parser.add_argument(
        "--reset-checkpoint",
        action="store_true",
        help="Delete the configured checkpoint before running (full re-score with current keywords).",
    )
    args = parser.parse_args()
    cfg_path = Path(str(args.config)).expanduser()
    if not cfg_path.is_absolute():
        cfg_path = (ROOT / cfg_path).resolve()
    run(config_path=cfg_path, reset_checkpoint=args.reset_checkpoint)

