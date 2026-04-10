#!/usr/bin/env python3
"""
Take `instagram_following_edges_run_*.csv` and add followers columns.

Input CSV columns expected:
  - source_username
  - following_username

Output CSV will add (as columns at the end):
  - source_followers
  - source_followers_raw
  - source_profile_url
  - following_followers
  - following_followers_raw
  - following_profile_url

Progress is checkpointed so you can interrupt + resume.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, sync_playwright

from instagram_profile_followers_fetch import (
    fetch_followers_for_handle,
    normalize_instagram_handle,
)
from instagram_search_browser_scraper import (
    ROOT,
    ensure_dirs,
    ensure_not_blocked,
    get_state_dir,
    wait_for_manual_login,
)

OUT_DIR = ROOT / "data" / "out"
RAW_DIR = ROOT / "data" / "raw"

DEFAULT_INPUT = ROOT / "data" / "out" / "instagram_following_edges_run_20260325_113422.csv"
DEFAULT_CHECKPOINT = RAW_DIR / "following_edges_followers_checkpoint.json"


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


def to_profile_url(username: str) -> str:
    return f"https://www.instagram.com/{username}/"


def followers_row(extras: dict[str, Any], username: str) -> dict[str, Any]:
    c = extras.get(username) or {}
    followers = c.get("followers")
    followers_raw = c.get("followers_raw")
    err = c.get("error")
    return {
        "followers": followers if followers is not None else "",
        "followers_raw": followers_raw or "",
        "_followers_error": err or "",
    }


def main() -> None:
    ensure_dirs()
    ap = argparse.ArgumentParser(description="Add followers columns to following edges CSV")
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Following edges CSV")
    ap.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT, help="Checkpoint JSON")
    ap.add_argument("--max-handles", type=int, default=0, help="For testing: fetch only N new handles")
    ap.add_argument("--max-rows", type=int, default=0, help="For testing: process only first N rows")
    ap.add_argument("--headless", action="store_true", help="Run browser headless (no login popup)")
    ap.add_argument("--headed", action="store_true", help="Alias for --no-headless (kept for convenience)")
    ap.add_argument("--live-write-every", type=int, default=25, help="Re-write output CSV every N fetches")
    ap.add_argument("--sleep", type=float, default=1.1, help="Base sleep between profile requests")
    ap.add_argument("--jitter", type=float, default=0.7, help="Extra random sleep")
    args = ap.parse_args()

    if not args.input.exists():
        raise SystemExit(f"Missing input CSV: {args.input}")

    # Default to headed so you can see the login popup if the session is not authenticated.
    headless = args.headless and not args.headed
    base_sleep = args.sleep
    jitter = args.jitter

    # Read edges CSV
    with args.input.open(newline="", encoding="utf-8") as f:
        rows: list[dict[str, Any]] = list(csv.DictReader(f))
    if args.max_rows > 0:
        rows = rows[: args.max_rows]

    for col in ["source_username", "following_username"]:
        if col not in rows[0]:
            raise SystemExit(f"Input CSV missing expected column: {col}")

    # Unique handles to fetch followers for
    handles: list[str] = []
    seen: set[str] = set()
    for r in rows:
        s = normalize_instagram_handle(r.get("source_username", "") or "")
        f = normalize_instagram_handle(r.get("following_username", "") or "")
        if s and s not in seen:
            seen.add(s)
            handles.append(s)
        if f and f not in seen:
            seen.add(f)
            handles.append(f)

    cp = load_checkpoint(args.checkpoint)
    cp_handles: dict[str, Any] = cp.get("handles", {})

    new_handles: list[str] = [h for h in handles if h not in cp_handles]
    if args.max_handles > 0:
        new_handles = new_handles[: args.max_handles]

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_csv = OUT_DIR / f"{args.input.stem}_with_followers_{ts}.csv"

    out_fields: list[str] = list(rows[0].keys())
    out_fields += [
        "source_followers",
        "source_followers_raw",
        "source_profile_url",
        "following_followers",
        "following_followers_raw",
        "following_profile_url",
        # Debug (optional but useful when a fetch fails)
        "source_followers_error",
        "following_followers_error",
    ]

    print(
        f"Following edges -> add followers: rows={len(rows)} unique_handles={len(handles)} "
        f"checkpoint_handles={len(cp_handles)} new_handles={len(new_handles)} headless={headless}",
        flush=True,
    )

    processed_count = 0
    with sync_playwright() as p:
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(get_state_dir()),
            headless=headless,
            viewport={"width": 1280, "height": 920},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()
        wait_for_manual_login(page)
        ensure_not_blocked(page, "following edges followers init")

        for handle in new_handles:
            followers, followers_raw, err = fetch_followers_for_handle(page, handle)
            cp_handles[handle] = {
                "followers": followers,
                "followers_raw": followers_raw or "",
                "error": err or "",
                "checked_at_epoch": int(time.time()),
            }
            processed_count += 1
            if processed_count % max(1, args.live_write_every) == 0:
                save_checkpoint(args.checkpoint, {"handles": cp_handles})
                # Live output rewrite using current cache
                write_rows: list[dict[str, Any]] = []
                for r in rows:
                    s = normalize_instagram_handle(r.get("source_username", "") or "") or ""
                    f = normalize_instagram_handle(r.get("following_username", "") or "") or ""
                    s_row = followers_row(cp_handles, s) if s else {"followers": "", "followers_raw": "", "_followers_error": ""}
                    f_row = followers_row(cp_handles, f) if f else {"followers": "", "followers_raw": "", "_followers_error": ""}
                    out_r = dict(r)
                    out_r.update(
                        {
                            "source_followers": s_row["followers"],
                            "source_followers_raw": s_row["followers_raw"],
                            "source_profile_url": to_profile_url(s) if s else "",
                            "following_followers": f_row["followers"],
                            "following_followers_raw": f_row["followers_raw"],
                            "following_profile_url": to_profile_url(f) if f else "",
                            "source_followers_error": s_row["_followers_error"],
                            "following_followers_error": f_row["_followers_error"],
                        }
                    )
                    write_rows.append(out_r)
                OUT_DIR.mkdir(parents=True, exist_ok=True)
                with out_csv.open("w", newline="", encoding="utf-8") as fo:
                    w = csv.DictWriter(fo, fieldnames=out_fields, extrasaction="ignore")
                    w.writeheader()
                    for wr in write_rows:
                        w.writerow(wr)
                print(f"  Live write: fetched={processed_count}/{len(new_handles)} -> {out_csv.name}", flush=True)

            time.sleep(base_sleep + random.uniform(0, jitter))

        # Final checkpoint + output write
        save_checkpoint(args.checkpoint, {"handles": cp_handles})

        final_rows: list[dict[str, Any]] = []
        for r in rows:
            s = normalize_instagram_handle(r.get("source_username", "") or "") or ""
            f = normalize_instagram_handle(r.get("following_username", "") or "") or ""
            s_row = followers_row(cp_handles, s) if s else {"followers": "", "followers_raw": "", "_followers_error": ""}
            f_row = followers_row(cp_handles, f) if f else {"followers": "", "followers_raw": "", "_followers_error": ""}
            out_r = dict(r)
            out_r.update(
                {
                    "source_followers": s_row["followers"],
                    "source_followers_raw": s_row["followers_raw"],
                    "source_profile_url": to_profile_url(s) if s else "",
                    "following_followers": f_row["followers"],
                    "following_followers_raw": f_row["followers_raw"],
                    "following_profile_url": to_profile_url(f) if f else "",
                    "source_followers_error": s_row["_followers_error"],
                    "following_followers_error": f_row["_followers_error"],
                }
            )
            final_rows.append(out_r)

        with out_csv.open("w", newline="", encoding="utf-8") as fo:
            w = csv.DictWriter(fo, fieldnames=out_fields, extrasaction="ignore")
            w.writeheader()
            for wr in final_rows:
                w.writerow(wr)

        context.close()

    print(f"Done. Output CSV: {out_csv}", flush=True)


if __name__ == "__main__":
    main()

