#!/usr/bin/env python3
"""
Send Instagram DMs by ATTACHING to your real Chrome profile via CDP.

Why:
  Playwright-launched Chromium profiles can get DM-composer throttled/blocked.
  This script connects to your already-logged-in Chrome instance started with:
    /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222

Safety:
  - Append-only CSV log (fsync per row)
  - Atomic checkpoint JSON (processed handles)
  - Kill/restart safe

Usage:
  1) Quit Chrome fully, then start with a debugging port:
       /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222
  2) Log into Instagram in that Chrome window.
  3) Run:
       .venv/bin/python chrome_dm_sender.py --input-csv dm_test_single.csv --handle-col Handles
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import time
from pathlib import Path
from typing import Any, Iterable

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "out"
RAW_DIR = ROOT / "data" / "raw"

DEFAULT_MESSAGE = (
    "Hey - I lead corporate development for LH2 Holdings, a US-based digital media company. We own and operate 20+ media brands and are actively looking to acquire pages in this space.\n"
    "Would you be open to a quick chat? We'd love to explore buying this page - we know how to grow these and have done it across a number of brands already.\n"
    "No pressure, just want to see if there's interest.\n"
    "— Stuti"
)


def normalize_handle(h: str) -> str:
    s = (h or "").strip().lower()
    if s.startswith("@"):
        s = s[1:]
    # accept full profile URLs too
    if "instagram.com/" in s:
        s = s.split("instagram.com/", 1)[1].strip("/")
        s = s.split("/", 1)[0].strip()
    return "".join(ch for ch in s if ch.isalnum() or ch in ("_", "."))


def read_handles_from_csv(path: Path, handle_col: str) -> list[str]:
    if not path.exists():
        raise RuntimeError(f"Missing input CSV: {path}")
    out: list[str] = []
    with path.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            raw = row.get(handle_col, "") or ""
            h = normalize_handle(str(raw))
            if h:
                out.append(h)
    # dedupe keep order
    return list(dict.fromkeys(out))


def ensure_parent_for_file(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def open_append_log_csv(path: Path) -> tuple[Any, csv.DictWriter]:
    ensure_parent_for_file(path)
    fieldnames = ["ts_epoch", "handle", "profile_url", "status", "error", "attempt_index"]
    new_file = (not path.exists()) or path.stat().st_size == 0
    f = path.open("a", newline="", encoding="utf-8")
    w = csv.DictWriter(f, fieldnames=fieldnames)
    if new_file:
        w.writeheader()
        f.flush()
        os.fsync(f.fileno())
    return f, w


def fsync_file_handle(f: Any) -> None:
    try:
        f.flush()
        os.fsync(f.fileno())
    except Exception:
        try:
            f.flush()
        except Exception:
            pass


def load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"processed_handles": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"processed_handles": []}


def save_checkpoint_atomic(path: Path, processed_handles: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {"processed_handles": sorted(set(processed_handles))}
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    try:
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass
    os.replace(tmp, path)


def get_context(browser: Browser) -> BrowserContext:
    # Chrome over CDP exposes at least one context.
    if browser.contexts:
        return browser.contexts[0]
    return browser.new_context()


def get_page(context: BrowserContext) -> Page:
    # Prefer an existing tab if present so you're definitely logged in.
    for p in context.pages:
        try:
            if p.url and "instagram.com" in p.url:
                return p
        except Exception:
            continue
    return context.new_page()

def _mouse_click_locator(page: Page, loc: Any, *, timeout_ms: int = 15000) -> None:
    loc.scroll_into_view_if_needed(timeout=timeout_ms)
    bb = loc.bounding_box()
    if not bb:
        raise RuntimeError("bounding_box_missing")
    x = bb["x"] + bb["width"] / 2.0 + random.uniform(-2.0, 2.0)
    y = bb["y"] + bb["height"] / 2.0 + random.uniform(-2.0, 2.0)
    page.mouse.move(x, y)
    page.mouse.click(x, y, delay=random.randint(25, 80))


def click_message_on_profile(page: Page) -> None:
    # This is the UI element the user pasted: div[role=button] with text "Message"
    selectors = [
        "div[role='button']:has-text('Message')",
        "button:has-text('Message')",
        "a:has-text('Message')",
        "div[role='button'][tabindex='0']:has-text('Message')",
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                _mouse_click_locator(page, loc)
                return
        except Exception:
            continue
    raise RuntimeError("message_button_not_found")


def wait_for_dm_composer(page: Page, timeout_s: float = 20.0) -> Any | None:
    deadline = time.time() + timeout_s
    candidates = [
        "div[role='textbox'][contenteditable='true']",
        "div[role='textbox'][contenteditable]",
        "div[role='textbox']",
        "textarea[aria-label*='Message' i]",
        "textarea",
        "div[contenteditable='true'][aria-label*='Message' i]",
        "div[contenteditable='true']",
    ]
    while time.time() < deadline:
        for sel in candidates:
            loc = page.locator(sel).first
            try:
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue
        time.sleep(0.5)
    return None


def send_dm(page: Page, *, handle: str, message: str) -> tuple[bool, str]:
    profile_url = f"https://www.instagram.com/{handle}/"
    page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)

    click_message_on_profile(page)

    tb = wait_for_dm_composer(page, timeout_s=25)
    if tb is None:
        return False, f"composer_not_found:url={page.url}"

    try:
        _mouse_click_locator(page, tb, timeout_ms=10000)
    except Exception:
        pass

    # Try fill then Enter-to-send.
    try:
        tb.fill(message, timeout=15000)
    except Exception:
        try:
            tb.type(message, delay=10)
        except Exception:
            return False, "composer_type_failed"

    try:
        page.keyboard.press("Enter")
    except Exception as exc:
        return False, f"enter_send_failed:{exc}"

    # Best-effort small wait; IG may send async.
    time.sleep(1.2)
    return True, ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Instagram DM sender via Chrome CDP (personal Chrome session)")
    ap.add_argument("--cdp-url", default="http://127.0.0.1:9222", help="Chrome remote debugging URL")
    ap.add_argument("--input-csv", required=True, help="CSV containing handles")
    ap.add_argument("--handle-col", required=True, help="Column header with handles")
    ap.add_argument("--message", default=DEFAULT_MESSAGE, help="Message to send")
    ap.add_argument("--max-dms-per-run", type=int, default=100, help="Hard cap per run")
    ap.add_argument("--log-csv", default=str(OUT_DIR / "chrome_dm_sender_log_append.csv"), help="Append-only log CSV")
    ap.add_argument(
        "--checkpoint-path",
        default=str(RAW_DIR / "chrome_dm_sender_checkpoint.json"),
        help="Checkpoint JSON path",
    )
    ap.add_argument("--reset-checkpoint", action="store_true", help="Delete checkpoint and start fresh")
    ap.add_argument("--sleep-seconds", type=float, default=2.0, help="Sleep between attempts")
    args = ap.parse_args()

    input_csv = Path(args.input_csv)
    if not input_csv.is_absolute():
        input_csv = (ROOT / input_csv).resolve()
    log_csv = Path(args.log_csv)
    if not log_csv.is_absolute():
        log_csv = (ROOT / log_csv).resolve()
    checkpoint = Path(args.checkpoint_path)
    if not checkpoint.is_absolute():
        checkpoint = (ROOT / checkpoint).resolve()

    if args.reset_checkpoint and checkpoint.exists():
        checkpoint.unlink()

    handles = read_handles_from_csv(input_csv, handle_col=args.handle_col)
    if not handles:
        raise RuntimeError("No handles found")

    cp = load_checkpoint(checkpoint)
    processed = set(cp.get("processed_handles", []))

    log_f, log_w = open_append_log_csv(log_csv)

    attempted = 0
    sent = 0
    failed = 0

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(args.cdp_url)
            context = get_context(browser)
            page = get_page(context)

            print(f"Attached to Chrome via CDP: {args.cdp_url}", flush=True)
            print(f"Input={input_csv.name} handles={len(handles)} checkpointed={len(processed)}", flush=True)

            for handle in handles:
                if handle in processed:
                    continue
                if attempted >= args.max_dms_per_run:
                    print(f"Reached max_dms_per_run={args.max_dms_per_run}. Stopping.", flush=True)
                    break

                attempted += 1
                ts = int(time.time())
                status = "failed"
                err = ""
                try:
                    ok, err = send_dm(page, handle=handle, message=args.message.replace("\\n", "\n"))
                    if ok:
                        status = "sent"
                        sent += 1
                    else:
                        status = "failed"
                        failed += 1
                except Exception as exc:
                    status = "failed"
                    err = str(exc)
                    failed += 1

                log_w.writerow(
                    {
                        "ts_epoch": ts,
                        "handle": handle,
                        "profile_url": f"https://www.instagram.com/{handle}/",
                        "status": status,
                        "error": err,
                        "attempt_index": attempted,
                    }
                )
                fsync_file_handle(log_f)
                processed.add(handle)
                save_checkpoint_atomic(checkpoint, processed_handles=processed)

                print(f"[{attempted}] @{handle} -> {status}" + (f" err={err}" if err else ""), flush=True)

                time.sleep(max(0.0, args.sleep_seconds + random.uniform(0.2, 0.9)))
    finally:
        try:
            log_f.close()
        except Exception:
            pass

    print(
        f"Done. attempted={attempted} sent={sent} failed={failed} log={log_csv} checkpoint={checkpoint}",
        flush=True,
    )


if __name__ == "__main__":
    main()

