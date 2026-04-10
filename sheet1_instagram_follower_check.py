#!/usr/bin/env python3
"""
Live Instagram follower pass for `instagram handles - Sheet1.csv` (and similar).

Adds ig_followers / ig_followers_raw from the logged-in Playwright session, marks
in_follower_band (yes/no) using search_min_followers / search_max_followers from
config.json (override with IG_FOLLOWER_BAND_MIN / IG_FOLLOWER_BAND_MAX).

Writes:
  data/out/instagram_handles_sheet1_ig_followers_<ts>.csv  (full sheet + new columns)
  data/out/instagram_handles_sheet1_in_band_<lo>_<hi>_<ts>.csv (subset where in band)

Checkpoint: data/raw/sheet1_instagram_follower_checkpoint.json (resume unless --no-resume)
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

from instagram_profile_followers_fetch import fetch_followers_for_handle, normalize_instagram_handle
from instagram_search_browser_scraper import (
    ROOT,
    ensure_dirs,
    ensure_not_blocked,
    get_state_dir,
    load_config,
    wait_for_manual_login,
)

OUT_DIR = ROOT / "data" / "out"
RAW_DIR = ROOT / "data" / "raw"
DEFAULT_SHEET = ROOT / "instagram handles - Sheet1.csv"
CHECKPOINT_FILE = RAW_DIR / "sheet1_instagram_follower_checkpoint.json"

EXTRA_KEYS = [
    "ig_followers",
    "ig_followers_raw",
    "ig_profile_url",
    "in_follower_band",
    "followers_sheet_vs_ig",
    "ig_fetch_error",
    "checked_at_epoch",
]


def load_checkpoint() -> dict[str, Any]:
    if not CHECKPOINT_FILE.exists():
        return {"handles": {}}
    try:
        data = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
        if "handles" not in data:
            data["handles"] = {}
        return data
    except Exception:
        return {"handles": {}}


def save_checkpoint(handles: dict[str, Any]) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at_epoch": int(time.time()), "handles": handles}
    CHECKPOINT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def parse_sheet_followers(val: Any) -> int | None:
    if val is None or str(val).strip() == "":
        return None
    s = str(val).strip().replace(",", "").replace(" ", "")
    try:
        return int(float(s))
    except ValueError:
        return None


def band_label(count: int | None, lo: int, hi: int) -> str:
    if count is None:
        return ""
    if lo <= count <= hi:
        return "yes"
    return "no"


def merge_extras(
    *,
    un: str,
    ig: int | None,
    ig_raw: str | None,
    err: str | None,
    lo: int,
    hi: int,
    sheet_fc: int | None,
    ts: int,
) -> dict[str, Any]:
    delta = ""
    if sheet_fc is not None and ig is not None:
        delta = str(ig - sheet_fc)
    return {
        "ig_followers": ig if ig is not None else "",
        "ig_followers_raw": ig_raw or "",
        "ig_profile_url": f"https://www.instagram.com/{un}/" if un else "",
        "in_follower_band": band_label(ig, lo, hi),
        "followers_sheet_vs_ig": delta,
        "ig_fetch_error": err or "",
        "checked_at_epoch": ts,
    }


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> None:
    ensure_dirs()
    ap = argparse.ArgumentParser(description="Live IG follower check for Sheet1 CSV")
    ap.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_SHEET,
        help="Input CSV path (default: instagram handles - Sheet1.csv)",
    )
    ap.add_argument("--max-rows", type=int, default=0, help="Process only first N data rows (0 = all)")
    ap.add_argument("--no-resume", action="store_true", help="Do not use checkpoint cache for skips")
    ap.add_argument("--headed", action="store_true", help="Force visible browser window for manual login")
    args = ap.parse_args()

    inp = args.input if args.input.is_absolute() else (ROOT / args.input).resolve()
    if not inp.exists():
        raise SystemExit(f"Missing input: {inp}")

    cfg = load_config()
    lo = int(os.getenv("IG_FOLLOWER_BAND_MIN", cfg.get("search_min_followers", 9000)))
    hi = int(os.getenv("IG_FOLLOWER_BAND_MAX", cfg.get("search_max_followers", 80000)))
    headless = (os.getenv("IG_PLAYWRIGHT_HEADLESS", "false").lower() == "true") and not args.headed
    sleep_s = float(os.getenv("IG_PROFILE_FETCH_SLEEP", "1.2"))
    flush_every = max(1, int(os.getenv("IG_SHEET1_FLUSH_EVERY", "20")))

    cp = load_checkpoint()
    handle_cache: dict[str, Any] = dict(cp.get("handles", {}))

    with inp.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        base_fields = list(reader.fieldnames or [])
        rows = list(reader)

    if args.max_rows > 0:
        rows = rows[: args.max_rows]

    for k in EXTRA_KEYS:
        if k not in base_fields:
            base_fields.append(k)
    out_fields = base_fields

    out_ts = time.strftime("%Y%m%d_%H%M%S")
    full_out = OUT_DIR / f"instagram_handles_sheet1_ig_followers_{out_ts}.csv"
    band_out = OUT_DIR / f"instagram_handles_sheet1_in_band_{lo}_{hi}_{out_ts}.csv"

    # Build initial merged rows (resume from checkpoint / empty extras)
    merged: list[dict[str, Any]] = []
    pending_indices: list[int] = []

    for i, row in enumerate(rows):
        raw_u = row.get("Username") or row.get("username") or ""
        un = normalize_instagram_handle(str(raw_u))
        sheet_fc = parse_sheet_followers(row.get("Followers Count") or row.get("Followers"))
        if not un:
            ex = merge_extras(
                un="",
                ig=None,
                ig_raw=None,
                err="bad_username",
                lo=lo,
                hi=hi,
                sheet_fc=sheet_fc,
                ts=int(time.time()),
            )
            merged.append({**row, **ex})
            continue
        if not args.no_resume and un in handle_cache:
            c = handle_cache[un]
            ig = c.get("ig_followers")
            if isinstance(ig, str) and ig.isdigit():
                ig = int(ig)
            elif ig is not None:
                try:
                    ig = int(ig)
                except (TypeError, ValueError):
                    ig = None
            ex = merge_extras(
                un=un,
                ig=ig,
                ig_raw=c.get("ig_followers_raw") or "",
                err=c.get("ig_fetch_error") or None,
                lo=lo,
                hi=hi,
                sheet_fc=sheet_fc,
                ts=int(c.get("checked_at_epoch") or time.time()),
            )
            merged.append({**row, **ex})
            continue
        merged.append({**row})  # placeholder; filled after fetch
        pending_indices.append(i)

    print(
        f"Sheet: {inp} | rows={len(rows)} | pending_fetch={len(pending_indices)} | band={lo}..{hi} | "
        f"resume={not args.no_resume} | out={full_out.name}",
        flush=True,
    )

    if not pending_indices:
        save_checkpoint(handle_cache)
        write_csv(full_out, out_fields, merged)
        band_rows = [r for r in merged if str(r.get("in_follower_band", "")).lower() == "yes"]
        write_csv(band_out, out_fields, band_rows)
        print("Nothing to fetch (everything in checkpoint). Wrote CSVs from cache.", flush=True)
        print(f"Full: {full_out}", flush=True)
        print(f" In-band: {band_out} ({len(band_rows)} rows)", flush=True)
        return

    with sync_playwright() as p:
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(get_state_dir()),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()
        wait_for_manual_login(page)
        ensure_not_blocked(page, "sheet1 follower check")

        for n, i in enumerate(pending_indices, start=1):
            row = rows[i]
            raw_u = row.get("Username") or row.get("username") or ""
            un = normalize_instagram_handle(str(raw_u))
            assert un
            sheet_fc = parse_sheet_followers(row.get("Followers Count") or row.get("Followers"))

            ig, ig_raw, err = fetch_followers_for_handle(page, un)
            ts = int(time.time())
            handle_cache[un] = {
                "ig_followers": ig,
                "ig_followers_raw": ig_raw or "",
                "ig_fetch_error": err or "",
                "checked_at_epoch": ts,
            }
            ex = merge_extras(
                un=un,
                ig=ig,
                ig_raw=ig_raw,
                err=err,
                lo=lo,
                hi=hi,
                sheet_fc=sheet_fc,
                ts=ts,
            )
            merged[i] = {**row, **ex}

            print(
                f"[{n}/{len(pending_indices)}] @{un} ig={ig} band={ex['in_follower_band']!r}",
                flush=True,
            )
            if n % flush_every == 0:
                save_checkpoint(handle_cache)
                write_csv(full_out, out_fields, merged)
                band_rows = [r for r in merged if str(r.get("in_follower_band", "")).lower() == "yes"]
                write_csv(band_out, out_fields, band_rows)
                print(f"  checkpoint + CSV flush ({n} fetches)", flush=True)

            time.sleep(sleep_s + random.uniform(0.15, 0.75))

        context.close()

    save_checkpoint(handle_cache)
    write_csv(full_out, out_fields, merged)
    band_rows = [r for r in merged if str(r.get("in_follower_band", "")).lower() == "yes"]
    write_csv(band_out, out_fields, band_rows)

    print(f"Done. Full: {full_out} ({len(merged)} rows)", flush=True)
    print(f" In-band ({lo}-{hi}): {band_out} ({len(band_rows)} rows)", flush=True)
    print(f" Checkpoint: {CHECKPOINT_FILE}", flush=True)


if __name__ == "__main__":
    main()
