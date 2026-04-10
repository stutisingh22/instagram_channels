from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from playwright.sync_api import BrowserContext, Page, sync_playwright

from instagram_search_browser_scraper import (
    ensure_not_blocked,
    get_state_dir,
    normalize_username,
    wait_for_manual_login,
)

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "out"
RAW_DIR = ROOT / "data" / "raw"
DEBUG_DIR_DEFAULT = ROOT / "dm_outreach_debug"

DEFAULT_MESSAGE = "Hey - I lead corporate development for LH2 Holdings, a US-based digital media company. We own and operate 20+ media brands and are actively looking to acquire pages in this space.\nWould you be open to a quick chat? We'd love to explore buying this page - we know how to grow these and have done it across a number of brands already.\nNo pressure, just want to see if there's interest.\n— Stuti"


def load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"processed_handles": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"processed_handles": []}


def save_checkpoint_atomic(path: Path, processed_handles: Iterable[str]) -> None:
    """Atomic checkpoint update (best effort) to reduce corruption risk."""
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


def normalize_handle(h: str) -> str:
    return normalize_username(h)


def read_handles_from_csv(path: Path, handle_col: str) -> list[str]:
    if not path.exists():
        raise RuntimeError(f"Missing input CSV: {path}")
    handles: list[str] = []
    with path.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            raw = row.get(handle_col, "") or ""
            h = normalize_handle(str(raw))
            if not h:
                continue
            handles.append(h)
    # Keep order but dedupe.
    return list(dict.fromkeys(handles))


def ensure_parent_for_file(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def open_append_log_csv(path: Path) -> tuple[Any, csv.DictWriter]:
    """Open CSV in append mode and create header if file is new."""
    ensure_parent_for_file(path)
    fieldnames = [
        "ts_epoch",
        "handle",
        "instagram_url",
        "status",
        "error",
        "attempt_index",
        "dry_run",
    ]
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


def try_get_textbox(page: Page) -> Any | None:
    # Instagram DM UI changes frequently; keep selectors broad and flexible.
    candidates = [
        "div[role='textbox'][contenteditable='true']",
        "div[role='textbox'][contenteditable]",
        "div[role='textbox']",
        "div[contenteditable='plaintext-only']",
        "textarea",
        "input[aria-label*='Message' i]",
        "input[placeholder*='Message' i]",
        "textarea[placeholder*='Message' i]",
        "textarea[placeholder*='message' i]",
        # Common aria-label patterns
        "textarea[aria-label*='Message' i]",
        "div[contenteditable='true'][aria-label*='Message' i]",
        "div[role='textbox'][aria-label*='Message' i]",
        # testid-like patterns
        "div[contenteditable='true'][data-testid*='message' i]",
        "div[contenteditable='true'][data-testid*='chat' i]",
        # Broad contenteditable fallback
        "div[contenteditable='true']",
        "div[contenteditable]",
        "div[aria-label*='Message' i][contenteditable]",
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        try:
            # Some IG DM composers exist in the DOM but aren't immediately visible;
            # returning it lets click/fill handle visibility timing.
            if loc.count() > 0:
                return loc
        except Exception:
            pass
    return None


def active_message_dialog(page: Page) -> Any | None:
    """Return the visible message dialog container if present."""
    candidates = [
        "div[role='dialog']",
        "div[aria-modal='true']",
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def try_get_textbox_in_scope(scope: Any) -> Any | None:
    """Find message composer textbox under a specific container."""
    if scope is None:
        return None
    candidates = [
        "div[role='textbox'][contenteditable='true']",
        "div[role='textbox'][contenteditable]",
        "div[role='textbox']",
        "textarea",
        "textarea[aria-label*='Message' i]",
        "div[contenteditable='true'][aria-label*='Message' i]",
        "div[contenteditable='true']",
    ]
    for sel in candidates:
        loc = scope.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def dm_debug_selector_counts(page: Page, selectors: list[str], *, max_per_sel: int = 3) -> str:
    """Return compact debug string with locator counts/visibility."""
    parts: list[str] = []
    for sel in selectors[:200]:
        try:
            loc = page.locator(sel).first
            cnt = loc.count()
            vis = False
            if cnt > 0:
                try:
                    vis = loc.is_visible()
                except Exception:
                    vis = False
            parts.append(f"{sel}={cnt}:{'v' if vis else 'h'}")
        except Exception as exc:
            parts.append(f"{sel}=err")
        if len(parts) >= max_per_sel:
            break
    return ",".join(parts)


def dm_selector_probe(page: Page) -> dict[str, str]:
    # A probe of whether common DM UI elements exist.
    composer_selectors = [
        "div[role='textbox'][contenteditable='true']",
        "div[role='textbox']",
        "textarea",
        "textarea[aria-label*='Message' i]",
        "div[contenteditable='true'][aria-label*='Message' i]",
        "div[role='textbox'][aria-label*='Message' i]",
        "div[contenteditable='true'][data-testid*='message' i]",
        "div[contenteditable='true']",
    ]
    msg_button_selectors = [
        "a:has-text('Message')",
        "button:has-text('Message')",
        "a:has-text('Messages')",
        "button:has-text('Messages')",
        "a[aria-label*='Message' i]",
        "button[aria-label*='Message' i]",
        "[role='button'][aria-label*='Message' i]",
        "div[role='button']:has-text('Message')",
        "a[href*='/direct/']",
        "a[href*='/direct/t/']",
    ]
    body_snippet = ""
    try:
        body_snippet = page.locator("body").inner_text(timeout=3000)[:300]
    except Exception:
        body_snippet = ""
    return {
        "url": str(getattr(page, "url", "")) or "",
        "composer_probe": dm_debug_selector_counts(page, composer_selectors, max_per_sel=8),
        "msg_button_probe": dm_debug_selector_counts(page, msg_button_selectors, max_per_sel=8),
        "body_snippet": body_snippet.replace("\n", " ").replace("\r", " "),
    }


def verification_tokens_from_message(message: str) -> list[str]:
    m = message or ""
    tokens: list[str] = []
    # Use business-specific token(s) likely to appear verbatim in the bubble.
    if "LH2" in m:
        tokens.append("LH2 Holdings")
        tokens.append("LH2")
    if "Stuti" in m:
        tokens.append("Stuti")
    # Fallback: first 2 alnum words >=4 chars
    if not tokens:
        words = re.findall(r"[A-Za-z0-9]{4,}", m)
        for w in words[:2]:
            tokens.append(w)
    # Dedupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        tt = t.strip()
        if tt and tt.lower() not in seen:
            seen.add(tt.lower())
            out.append(tt)
    return out


def get_text_from_locator(loc: Any) -> str:
    if loc is None:
        return ""
    try:
        # Locator may represent contenteditable; inner_text is best-effort.
        return (loc.inner_text(timeout=2000) or "").strip()
    except Exception:
        pass
    try:
        return (loc.text_content(timeout=2000) or "").strip()
    except Exception:
        return ""


def verify_message_sent(
    page: Page,
    *,
    tokens: list[str],
    timeout_s: int = 12,
) -> tuple[bool, str]:
    """Best-effort verification: look for your tokens and composer emptied (when detectable)."""
    tokens_l = [t.lower() for t in tokens if t]
    if not tokens_l:
        return False, "no_verification_tokens"

    deadline = time.time() + timeout_s
    last_reason = "token_not_found_yet"
    while time.time() < deadline:
        html_l = ""
        try:
            html_l = page.content().lower()
        except Exception:
            html_l = ""

        token_found = any(t in html_l for t in tokens_l)
        if token_found:
            tb = try_get_textbox(page)
            if tb is not None:
                tb_text = get_text_from_locator(tb)
                if tb_text:
                    last_reason = "token_found_but_composer_not_empty"
                    time.sleep(0.6)
                    continue
            return True, "token_found_and_composer_empty_or_unavailable"

        # Sometimes there is a toast that contains generic words.
        if "sent" in html_l or "message sent" in html_l:
            last_reason = "generic_sent_indicator_seen"

        time.sleep(0.6)

    return False, last_reason


def dump_page_debug(
    page: Page,
    *,
    debug_dir: Path,
    handle: str,
    stage: str,
) -> None:
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        html_path = debug_dir / f"{handle}__{stage}__{ts}.html"
        png_path = debug_dir / f"{handle}__{stage}__{ts}.png"
        try:
            page.screenshot(path=str(png_path), full_page=True)
        except Exception:
            pass
        try:
            html = page.content()
            # Truncate huge HTML for safety.
            html = html[:400_000]
            html_path.write_text(html, encoding="utf-8")
        except Exception:
            pass
    except Exception:
        pass


def try_click_any(page: Page, selectors: list[str], *, timeout_ms: int = 5000) -> bool:
    """Try clicking the first visible/clickable element among selectors."""
    for sel in selectors[:100]:
        try:
            loc = page.locator(sel).first
            if loc.count() <= 0:
                continue
            try:
                loc.scroll_into_view_if_needed(timeout=timeout_ms)
            except Exception:
                pass
            # Don't require visibility; click often auto-scrolls.
            loc.click(timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False


def try_click_any_in_scope(scope: Any, selectors: list[str], *, timeout_ms: int = 5000) -> bool:
    """Click first visible element among selectors, but scoped under a root locator."""
    for sel in selectors[:200]:
        try:
            loc = scope.locator(sel).first
            if loc.count() <= 0:
                continue
            try:
                loc.scroll_into_view_if_needed(timeout=timeout_ms)
            except Exception:
                pass
            loc.click(timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False


def click_profile_message_button(page: Page, handle: str) -> bool:
    """
    Click the "Message"/"Messages" CTA on the profile header (next to Follow/Following).

    Important: we keep this scoped to the profile `header` so we don't accidentally
    click the handle list inside the DM inbox UI.
    """
    expected_profile_sub = f"/{handle}/"
    try:
        current_url = str(getattr(page, "url", "") or "")
    except Exception:
        current_url = ""

    # Ensure we are on the profile page. If not, navigate once and re-check.
    if expected_profile_sub not in current_url:
        profile_url = f"https://www.instagram.com/{handle}/"
        page.goto(profile_url, wait_until="domcontentloaded", timeout=20000)

    # Find the "Follow/Following" button first, then click the "Message/Messages"
    # button that appears on the same header row to the right of it.
    # Avoid clicking deep inbox UI elements by preferring smaller y (top-most),
    # but allow enough range so bounding box y isn't the limiting factor.
    max_y_for_header_ui = 5000

    follow_candidates = [
        "button:has-text('Follow')",
        "button:has-text('Following')",
        "a:has-text('Follow')",
        "a:has-text('Following')",
        "div[role='button']:has-text('Follow')",
        "div[role='button']:has-text('Following')",
        "button[aria-label*='Follow' i]",
        "button[aria-label*='Following' i]",
        "button[aria-label*='Follow' i]",
        "button[aria-label*='Following' i]",
    ]
    follow_loc = None
    follow_x = None
    follow_y = None
    for sel in follow_candidates:
        locs = page.locator(sel)
        try:
            cnt = locs.count()
        except Exception:
            cnt = 0
        for i in range(min(cnt, 6)):
            loc = locs.nth(i)
            try:
                if not loc.is_visible():
                    continue
                bb = loc.bounding_box()
                if bb is None:
                    continue
                y = bb.get("y")
                x = bb.get("x")
                if y is None or x is None:
                    continue
                # Avoid picking follow buttons from other UI regions (inbox list, etc).
                if y is not None and y > max_y_for_header_ui:
                    continue
                if follow_y is None or y < follow_y:
                    follow_loc = loc
                    follow_x = x
                    follow_y = y
            except Exception:
                continue

    msg_candidates = [
        "button:has-text('Message')",
        "button:has-text('Messages')",
        "a:has-text('Message')",
        "a:has-text('Messages')",
        "div[role='button']:has-text('Message')",
        "div[role='button']:has-text('Messages')",
        "button[aria-label*='Message' i]",
        "button[aria-label*='Messages' i]",
        "a[aria-label*='Message' i]",
        "div[role='button'][aria-label*='Message' i]",
    ]

    best_msg = None
    best_score = None
    for sel in msg_candidates:
        locs = page.locator(sel)
        try:
            cnt = locs.count()
        except Exception:
            cnt = 0
        for i in range(min(cnt, 8)):
            loc = locs.nth(i)
            try:
                if not loc.is_visible():
                    continue
                bb = loc.bounding_box()
                if bb is None:
                    continue
                x = bb.get("x")
                y = bb.get("y")
                if y is None:
                    continue

                if follow_y is not None:
                    # Same header row: y close to follow button.
                    if abs(y - follow_y) > 40:
                        continue
                    # To the right of follow.
                    if follow_x is not None and x is not None and x <= follow_x:
                        continue

                # If we don't know follow coords, keep it near the top to avoid inbox UIs.
                if follow_y is None and y > max_y_for_header_ui:
                    continue

                # Prefer smallest horizontal distance from the follow button.
                right_dist = (x - follow_x) if (x is not None and follow_x is not None) else 0
                score = (
                    abs(y - follow_y) if follow_y is not None else y,
                    right_dist,
                )
                if best_score is None or score < best_score:
                    best_score = score
                    best_msg = loc
            except Exception:
                continue

    if best_msg is None:
        # Fallback: click the top-most visible message CTA on the page, but only
        # in the upper half to avoid clicking inbox list items.
        fallback_best = None
        fallback_y = None
        for sel in msg_candidates:
            locs = page.locator(sel)
            try:
                cnt = locs.count()
            except Exception:
                cnt = 0
            for i in range(min(cnt, 10)):
                loc = locs.nth(i)
                try:
                    if not loc.is_visible():
                        continue
                    bb = loc.bounding_box()
                    if bb is None:
                        continue
                    y = bb.get("y")
                    x = bb.get("x")
                    if y is None or y > max_y_for_header_ui:
                        continue
                    if fallback_y is None or y < fallback_y:
                        fallback_y = y
                        fallback_best = loc
                except Exception:
                    continue
        if fallback_best is None:
            return False
        best_msg = fallback_best
    try:
        best_msg.scroll_into_view_if_needed(timeout=15000)
    except Exception:
        pass
    try:
        best_msg.click(timeout=15000)
        return True
    except Exception:
        return False


def click_top_profile_message_cta(page: Page, handle: str) -> bool:
    """
    Click top-most visible Message/Messages CTA on the profile page.
    Returns True when click action succeeds.
    """
    expected_profile_sub = f"/{handle}/"
    try:
        current_url = str(getattr(page, "url", "") or "")
    except Exception:
        current_url = ""
    if expected_profile_sub not in current_url:
        profile_url = f"https://www.instagram.com/{handle}/"
        page.goto(profile_url, wait_until="domcontentloaded", timeout=20000)

    msg_candidates = [
        "button:has-text('Message')",
        "button:has-text('Messages')",
        "a:has-text('Message')",
        "a:has-text('Messages')",
        "div[role='button']:has-text('Message')",
        "div[role='button']:has-text('Messages')",
        "button[aria-label*='Message' i]",
        "button[aria-label*='Messages' i]",
        "a[aria-label*='Message' i]",
        "div[role='button'][aria-label*='Message' i]",
    ]

    best_loc = None
    best_y = None
    for sel in msg_candidates:
        locs = page.locator(sel)
        try:
            cnt = locs.count()
        except Exception:
            cnt = 0
        for i in range(min(cnt, 6)):
            loc = locs.nth(i)
            try:
                if not loc.is_visible():
                    continue
                bb = loc.bounding_box()
                if bb is None:
                    continue
                y = bb.get("y")
                if y is None:
                    continue
                if best_y is None or y < best_y:
                    best_y = y
                    best_loc = loc
            except Exception:
                continue

    if best_loc is None:
        return False
    try:
        best_loc.scroll_into_view_if_needed(timeout=10000)
    except Exception:
        pass
    try:
        best_loc.click(timeout=10000)
        return True
    except Exception:
        return False


def fast_block_guard(page: Page, *, context: str) -> None:
    """Fail fast if Instagram shows challenge/checkpoint/login wall."""
    url = str(getattr(page, "url", "")) or ""
    u = url.lower()
    if any(x in u for x in ("challenge", "checkpoint", "captcha")):
        raise RuntimeError(f"blocked_by_instagram:{context}:url={url}")
    if "accounts/login" in u or "login" in u and "instagram.com" in u:
        raise RuntimeError(f"login_required:{context}:url={url}")


def wait_for_composer(page: Page, *, timeout_s: int = 20) -> Any | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        tb = try_get_textbox(page)
        if tb is not None:
            return tb
        time.sleep(0.5)
    return None


def try_get_send_button(page: Page) -> Any | None:
    candidates = [
        "button[type='submit']",
        "button:has-text('Send')",
        "div[role='button']:has-text('Send')",
        "span:has-text('Send')",
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            pass
    return None


def try_get_send_button_in_scope(scope: Any) -> Any | None:
    if scope is None:
        return None
    candidates = [
        "button[type='submit']",
        "button:has-text('Send')",
        "div[role='button']:has-text('Send')",
        "span:has-text('Send')",
    ]
    for sel in candidates:
        loc = scope.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def open_dm_thread(page: Page, handle: str) -> None:
    # Basic flow:
    # 1) Go to profile
    # 2) Click the correct "Message/Messages" CTA next to Follow/Following
    # 3) Wait for the composer textbox to mount
    profile_url = f"https://www.instagram.com/{handle}/"
    page.goto(profile_url, wait_until="domcontentloaded", timeout=20000)
    fast_block_guard(page, context=f"dm_profile @{handle}")

    clicked = click_profile_message_button(page, handle)
    if not clicked:
        probe = dm_selector_probe(page)
        raise RuntimeError(
            "profile_message_button_not_found:"
            f"url={probe.get('url','')}:"
            f"msgbtn={probe.get('msg_button_probe','')}"
        )

    tb = wait_for_composer(page, timeout_s=8)
    if tb is not None:
        return

    # Some IG variants need a second click on the same profile CTA.
    _ = click_profile_message_button(page, handle)
    time.sleep(0.8)
    tb = wait_for_composer(page, timeout_s=12)
    if tb is not None:
        return

    # Advance next-step buttons, but avoid clicking any "Send" actions early.
    modal_deadline = time.time() + 35
    modal_selectors = [
        "button:has-text('Continue')",
        "button:has-text('Next')",
        "button:has-text('Request')",
        "button:has-text('Send message request')",
        "button[aria-label*='Send message request' i]",
        "button[aria-label*='Request' i]",
        "div[role='button']:has-text('Continue')",
        "div[role='button']:has-text('Next')",
        "span:has-text('Continue')",
        "span:has-text('Request')",
        "span:has-text('Send message request')",
        "a:has-text('Send message request')",
    ]

    while time.time() < modal_deadline:
        tb2 = try_get_textbox(page)
        if tb2 is not None:
            return
        try_click_any(page, modal_selectors, timeout_ms=2000)
        time.sleep(0.7)

    probe = dm_selector_probe(page)
    raise RuntimeError(
        "composer_not_appeared_after_modal:"
        f"url={probe.get('url','')}:"
        f"composer={probe.get('composer_probe','')}:"
        f"msgbtn={probe.get('msg_button_probe','')}"
    )


def send_message(page: Page, *, handle: str, message: str) -> tuple[bool, str]:
    """Return (ok, error_reason).

    Only returns ok=True when verification passes (message text appears in-thread).
    """
    tokens = verification_tokens_from_message(message)
    textbox = try_get_textbox(page)
    if textbox is None:
        textbox = wait_for_composer(page, timeout_s=20)

    if textbox is None:
        probe = dm_selector_probe(page)
        err = (
            "dm_composer_not_found:"
            f"url={probe.get('url','')}:"
            f"composer={probe.get('composer_probe','')}:"
            f"msgbtn={probe.get('msg_button_probe','')}:"
            f"body={probe.get('body_snippet','')}"
        )
        return False, err

    try:
        textbox.click(timeout=15000)
    except Exception:
        pass

    # For textarea we can fill; for contenteditable, fill usually works too.
    try:
        textbox.fill(message, timeout=15000)
    except Exception:
        try:
            textbox.type(message, delay=10)
        except Exception:
            return False, "textbox_fill_failed"

    deadline = time.time() + 15
    last_err: str = ""
    while time.time() < deadline:
        try:
            send_btn = try_get_send_button(page)
            if send_btn is not None and send_btn.is_visible():
                send_btn.click(timeout=15000)
                ok, reason = verify_message_sent(page, tokens=tokens, timeout_s=12)
                if ok:
                    return True, ""
                return False, f"verify_failed:{reason}"
        except Exception as exc:
            last_err = str(exc)
            time.sleep(0.4)
    return False, f"send_click_failed:{last_err}" if last_err else "send_click_failed"


def run(
    *,
    input_csv: Path,
    handle_col: str,
    message: str,
    max_dms_per_run: int,
    batch_size: int,
    batch_sleep_seconds: int,
    checkpoint_path: Path,
    log_csv_path: Path,
    playwright_state_dir: Path,
    dry_run: bool,
    reset_checkpoint: bool,
    pause_after_login: bool,
    resume_file: Path,
    debug_dir: Path,
) -> None:
    # Allow passing literal "\n" sequences from the CLI.
    message = message.replace("\\n", "\n")
    handles = read_handles_from_csv(input_csv, handle_col=handle_col)
    if not handles:
        raise RuntimeError(f"No handles found in {input_csv} column {handle_col}")

    if reset_checkpoint and checkpoint_path.exists():
        checkpoint_path.unlink()
        print(f"Reset checkpoint removed: {checkpoint_path}", flush=True)

    cp = load_checkpoint(checkpoint_path)
    processed = set(cp.get("processed_handles", []))

    out_total = 0
    out_sent = 0
    out_failed = 0

    ensure_parent_for_file(log_csv_path)
    log_f, log_w = open_append_log_csv(log_csv_path)

    try:
        with sync_playwright() as p:
            context: BrowserContext = p.chromium.launch_persistent_context(
                user_data_dir=str(playwright_state_dir),
                headless=False,
                viewport={"width": 1440, "height": 920},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.new_page()

            wait_for_manual_login(page)
            ensure_not_blocked(page, "dm_outreach init")
            if pause_after_login:
                # If stdin isn't interactive (common when running in background),
                # use a file-based resume gate instead of input().
                is_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
                if is_tty:
                    print(
                        "Paused after login. Finish login in the browser, then press Enter here to continue...",
                        flush=True,
                    )
                    _ = input()
                else:
                    print(
                        f"Paused after login (non-interactive). Create file to resume: {resume_file}",
                        flush=True,
                    )
                    resume_file.parent.mkdir(parents=True, exist_ok=True)
                    while not resume_file.exists():
                        time.sleep(1.0)

            attempt_index = 0
            for idx, handle in enumerate(handles, start=1):
                if handle in processed:
                    continue
                attempt_index += 1
                if out_total >= max_dms_per_run:
                    print(f"Reached max_dms_per_run={max_dms_per_run}. Stopping.", flush=True)
                    break

                ts = int(time.time())
                instagram_url = f"https://www.instagram.com/{handle}/"
                status = "failed"
                err = ""

                try:
                    open_dm_thread(page, handle)
                    if dry_run:
                        # Validate composer exists.
                        tb = wait_for_composer(page, timeout_s=25)
                        if tb is None:
                            status = "dry_run_failed"
                            probe = dm_selector_probe(page)
                            err = (
                                "dm_composer_not_found:"
                                f"url={probe.get('url','')}:"
                                f"composer={probe.get('composer_probe','')}:"
                                f"msgbtn={probe.get('msg_button_probe','')}"
                            )
                            dump_page_debug(
                                page,
                                debug_dir=debug_dir,
                                handle=handle,
                                stage="dryrun_composer_missing",
                            )
                        else:
                            status = "dry_run_ok"
                    else:
                        ok, send_err = send_message(page, handle=handle, message=message)
                        if ok:
                            status = "sent"
                            err = ""
                            out_sent += 1
                        else:
                            status = "failed"
                            err = send_err
                            out_failed += 1

                except Exception as exc:
                    status = "failed"
                    err = str(exc)
                    out_failed += 1
                    dump_page_debug(
                        page,
                        debug_dir=debug_dir,
                        handle=handle,
                        stage="exception_open_dm_thread",
                    )

                # Append log row and fsync it.
                log_w.writerow(
                    {
                        "ts_epoch": ts,
                        "handle": handle,
                        "instagram_url": instagram_url,
                        "status": status,
                        "error": err,
                        "attempt_index": attempt_index,
                        "dry_run": "true" if dry_run else "false",
                    }
                )
                fsync_file_handle(log_f)

                processed.add(handle)
                save_checkpoint_atomic(checkpoint_path, processed_handles=processed)

                out_total += 1
                print(
                    f"[{idx}/{len(handles)}] @{handle} -> {status}" + (f" err={err}" if err else ""),
                    flush=True,
                )

                # Batch sleeping policy.
                if out_total % batch_size == 0 and batch_sleep_seconds > 0:
                    print(
                        f"Batch pause: sent_or_failed={out_total} sleeping {batch_sleep_seconds}s...",
                        flush=True,
                    )
                    time.sleep(batch_sleep_seconds)

                # Tiny jitter to reduce burst patterns.
                time.sleep(random.uniform(0.3, 1.0))

            context.close()
    finally:
        try:
            log_f.close()
        except Exception:
            pass

    print(
        f"DM outreach done. total_attempted={out_total} sent={out_sent} failed={out_failed} "
        f"log={log_csv_path}",
        flush=True,
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Instagram DM outreach automation (append-only log + checkpointed)")
    ap.add_argument("--input-csv", required=True, help="Shortlist CSV containing handles")
    ap.add_argument("--handle-col", required=True, help="Column header containing Instagram handles/urls")
    ap.add_argument("--message", default=DEFAULT_MESSAGE, help="Message to send")
    ap.add_argument("--max-dms-per-run", type=int, default=100, help="Hard cap per run")
    ap.add_argument("--batch-size", type=int, default=50, help="Pause after this many attempts")
    ap.add_argument("--batch-sleep-seconds", type=int, default=60, help="Seconds to sleep after each batch")
    ap.add_argument(
        "--log-csv",
        default=str(OUT_DIR / "instagram_dm_outreach_log_append.csv"),
        help="Append-only log CSV path",
    )
    ap.add_argument(
        "--checkpoint-path",
        default=str(RAW_DIR / "instagram_dm_outreach_checkpoint.json"),
        help="Checkpoint JSON path",
    )
    ap.add_argument(
        "--playwright-state-dir",
        default=str(ROOT / ".ig_browser_state_dm_outreach"),
        help="Dedicated browser profile dir",
    )
    ap.add_argument("--dry-run", action="store_true", help="Do not click send; only validate composer")
    ap.add_argument("--reset-checkpoint", action="store_true", help="Delete checkpoint and start fresh")
    ap.add_argument(
        "--pause-after-login",
        action="store_true",
        help="After initial login flow, pause (no navigation) until you press Enter. Useful when login pages auto-refresh.",
    )
    ap.add_argument(
        "--resume-file",
        default=str(ROOT / "dm_outreach_resume_go.flag"),
        help="If --pause-after-login is used and stdin is non-interactive, wait until this file exists.",
    )
    ap.add_argument(
        "--debug-dir",
        default=str(DEBUG_DIR_DEFAULT),
        help="Directory for failure-only debug screenshots/HTML dumps.",
    )
    args = ap.parse_args()

    input_csv = Path(args.input_csv)
    if not input_csv.is_absolute():
        input_csv = (ROOT / input_csv).resolve()

    log_csv_path = Path(args.log_csv)
    if not log_csv_path.is_absolute():
        log_csv_path = (ROOT / log_csv_path).resolve()

    checkpoint_path = Path(args.checkpoint_path)
    if not checkpoint_path.is_absolute():
        checkpoint_path = (ROOT / checkpoint_path).resolve()

    playwright_state_dir = Path(args.playwright_state_dir)
    if not playwright_state_dir.is_absolute():
        playwright_state_dir = (ROOT / playwright_state_dir).resolve()

    resume_file = Path(args.resume_file)
    if not resume_file.is_absolute():
        resume_file = (ROOT / resume_file).resolve()

    debug_dir = Path(args.debug_dir)
    if not debug_dir.is_absolute():
        debug_dir = (ROOT / debug_dir).resolve()

    run(
        input_csv=input_csv,
        handle_col=args.handle_col,
        message=args.message,
        max_dms_per_run=args.max_dms_per_run,
        batch_size=args.batch_size,
        batch_sleep_seconds=args.batch_sleep_seconds,
        checkpoint_path=checkpoint_path,
        log_csv_path=log_csv_path,
        playwright_state_dir=playwright_state_dir,
        dry_run=args.dry_run,
        reset_checkpoint=args.reset_checkpoint,
        pause_after_login=args.pause_after_login,
        resume_file=resume_file,
        debug_dir=debug_dir,
    )

