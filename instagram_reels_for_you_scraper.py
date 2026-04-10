from __future__ import annotations

import csv
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, sync_playwright


ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "out"
RAW_DIR = ROOT / "data" / "raw"
DEFAULT_STATE_DIR = ROOT / ".ig_browser_state_reels"
DEFAULT_CONFIG = ROOT / "reels_for_you_config.json"
CHECKPOINT_FILE = RAW_DIR / "instagram_reels_for_you_checkpoint.json"


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"Missing config file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_dirs(state_dir: Path) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)


def is_login_page(page: Page) -> bool:
    return "accounts/login" in page.url or page.locator("input[name='username']").count() > 0


def wait_for_manual_login(page: Page) -> None:
    page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    time.sleep(2)
    if is_login_page(page):
        print("Login required in Playwright browser window.", flush=True)
        print("Please login manually. Waiting up to 10 minutes...", flush=True)
        print("Note: this script will NOT auto-refresh while you type credentials.", flush=True)
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(3)
            if not is_login_page(page):
                break
    if is_login_page(page):
        raise RuntimeError("Still on login page after wait.")
    print(f"Authenticated page URL: {page.url}", flush=True)


def startup_login_pause(page: Page, pause_seconds: int) -> None:
    if pause_seconds <= 0:
        return
    page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded")
    print(
        f"Startup login pause enabled: {pause_seconds}s. Please login now; scraper will wait without navigating.",
        flush=True,
    )
    deadline = time.time() + pause_seconds
    while time.time() < deadline:
        remaining = int(deadline - time.time())
        if remaining % 15 == 0:
            print(f"Waiting for manual login... {remaining}s left", flush=True)
        time.sleep(1)


def normalize_keyword(value: str) -> str:
    text = value.strip()
    while text.startswith("#"):
        text = text[1:]
    return text.strip().lower()


def base_keyword_key(value: str) -> str:
    k = normalize_keyword(value)
    # Treat US-qualified variants as same logical keyword for checkpoint continuity.
    for suffix in (" us", " usa", " united states"):
        if k.endswith(suffix):
            k = k[: -len(suffix)].strip()
    if k.startswith("us "):
        k = k[3:].strip()
    return k


def qualify_keyword_for_us(keyword: str, enabled: bool) -> str:
    if not enabled:
        return keyword
    k = keyword.strip().lower()
    if not k:
        return k
    if any(t in k for t in (" us", "usa", "united states", "america", "american")):
        return k
    broad_terms = (
        "breaking news",
        "news",
        "politics",
        "election",
        "media",
        "channel",
    )
    if any(t in k for t in broad_terms):
        if "news" in k:
            return f"{k} us"
        return f"us {k}"
    return k


def normalize_username(value: str) -> str:
    u = value.strip().lower().lstrip("@")
    if u.startswith("www."):
        u = u[4:]
    if "." in u:
        base, tail = u.rsplit(".", 1)
        if tail in {"com", "net", "org", "io", "co", "in", "tv", "news"} and base:
            u = base
    return u


def is_likely_valid_username(value: str) -> bool:
    u = normalize_username(value)
    if not re.fullmatch(r"[a-z0-9._]{2,30}", u):
        return False
    blocked_tokens = ("gmail", "yahoo", "hotmail", "outlook", "mail")
    if any(tok in u for tok in blocked_tokens):
        return False
    return True


def load_keywords_from_excel(path_value: str, sheet_name: str, limit: int = 0) -> list[str]:
    try:
        import pandas as pd  # type: ignore
    except Exception as exc:
        print(f"Excel keyword load skipped (pandas unavailable): {exc}", flush=True)
        return []

    path = Path(path_value)
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    if not path.exists():
        print(f"Excel keyword file not found: {path}", flush=True)
        return []

    want_all = False
    sname = str(sheet_name or "").strip().lower()
    if not sname or sname in {"all", "*", "both"}:
        want_all = True

    dfs: list[Any] = []
    try:
        if want_all:
            workbook = pd.ExcelFile(path)
            if not workbook.sheet_names:
                return []
            for sh in workbook.sheet_names:
                dfs.append(pd.read_excel(path, sheet_name=sh))
        else:
            dfs.append(pd.read_excel(path, sheet_name=sheet_name))
    except Exception as exc:
        # If a specific sheet fails, fall back to reading all sheets.
        try:
            workbook = pd.ExcelFile(path)
            for sh in workbook.sheet_names:
                dfs.append(pd.read_excel(path, sheet_name=sh))
        except Exception:
            print(f"Excel read failed for {path} ({exc})", flush=True)
            return []

    out: list[str] = []
    for df in dfs:
        for col in df.columns:
            for v in df[col].dropna().tolist():
                k = normalize_keyword(str(v))
                if k:
                    out.append(k)

    deduped = list(dict.fromkeys(out))
    if limit > 0:
        deduped = deduped[:limit]
    return deduped


def load_checkpoint() -> dict[str, Any]:
    if not CHECKPOINT_FILE.exists():
        return {"rows": [], "seen_reel_ids": [], "processed_keywords": []}
    try:
        return json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"rows": [], "seen_reel_ids": [], "processed_keywords": []}


def save_checkpoint(rows: list[dict[str, Any]], seen_reel_ids: set[str], processed_keywords: set[str]) -> None:
    payload = {
        "rows": rows,
        "seen_reel_ids": sorted(seen_reel_ids),
        "processed_keywords": sorted(processed_keywords),
    }
    CHECKPOINT_FILE.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def write_live_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["reel_url", "reel_id", "username", "source_keyword", "seed_url"],
        )
        writer.writeheader()
        writer.writerows(rows)


def read_existing_seed_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                out.append(
                    {
                        "source_keyword": row.get("source_keyword", ""),
                        "seed_index": row.get("seed_index", ""),
                        "seed_total": row.get("seed_total", ""),
                        "seed_url": row.get("seed_url", ""),
                    }
                )
    except Exception:
        return []
    return out


def write_seed_links_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["source_keyword", "seed_index", "seed_total", "seed_url"],
        )
        writer.writeheader()
        writer.writerows(rows)


def _extract_reel_or_post_links(page: Page, max_links: int, seen: set[str], links: list[str]) -> None:
    hrefs = page.eval_on_selector_all(
        "a[href*='/reel/'], a[href*='/p/']",
        "els => els.map(e => e.getAttribute('href')).filter(Boolean)",
    )
    for href in hrefs:
        m = re.match(r"^/(reel|p)/([^/]+)/", href)
        if not m:
            continue
        full = f"https://www.instagram.com/{m.group(1)}/{m.group(2)}/"
        if full in seen:
            continue
        seen.add(full)
        links.append(full)
        if len(links) >= max_links:
            break


def _scroll_collect(page: Page, keyword: str, max_seed_reels: int, max_rounds: int) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    stagnant_rounds = 0
    for round_idx in range(1, max_rounds + 1):
        before = len(links)
        _extract_reel_or_post_links(page, max_seed_reels, seen, links)
        print(f"keyword '{keyword}' round {round_idx}: seed reels {len(links)}/{max_seed_reels}", flush=True)
        if len(links) >= max_seed_reels:
            break
        if len(links) == before:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
        if stagnant_rounds >= 3:
            break
        page.mouse.wheel(0, 3000)
        time.sleep(1.0 + random.uniform(0.4, 0.9))
    return links[:max_seed_reels]


def collect_seed_reels_from_search(page: Page, keyword: str, max_seed_reels: int, max_rounds: int) -> list[str]:
    candidates = [
        f"https://www.instagram.com/explore/search/keyword/?q={keyword.replace(' ', '%20')}",
        f"https://www.instagram.com/explore/search/keyword/?q=%23{keyword.replace(' ', '')}",
        f"https://www.instagram.com/explore/tags/{keyword.replace(' ', '')}/",
    ]
    for url in candidates:
        page.goto(url, wait_until="domcontentloaded")
        time.sleep(2.0)
        if is_login_page(page):
            wait_for_manual_login(page)
            page.goto(url, wait_until="domcontentloaded")
            time.sleep(2.0)
        links = _scroll_collect(page, keyword, max_seed_reels=max_seed_reels, max_rounds=max_rounds)
        if links:
            return links
    return []


def collect_seed_reels_from_explore(page: Page, max_seed_reels: int, max_rounds: int) -> list[str]:
    page.goto("https://www.instagram.com/explore/reels/", wait_until="domcontentloaded")
    time.sleep(2.0)
    if is_login_page(page):
        wait_for_manual_login(page)
        page.goto("https://www.instagram.com/explore/reels/", wait_until="domcontentloaded")
        time.sleep(2.0)
    return _scroll_collect(page, "explore_fallback", max_seed_reels=max_seed_reels, max_rounds=max_rounds)


def extract_reel_record(page: Page, source_keyword: str) -> dict[str, Any] | None:
    url = page.url
    m = re.search(r"/(reel|p)/([^/]+)/", url)
    if not m:
        return None
    reel_id = m.group(2)

    username = None
    html = page.content()

    # 1) Prefer OG metadata when available (very stable on /p/ and /reel/ pages).
    og_desc = page.locator("meta[property='og:description']").first.get_attribute("content")
    if og_desc:
        mu = re.search(r"@([A-Za-z0-9._]+)", og_desc)
        if mu:
            username = mu.group(1).lower()

    # 2) Try scoped header links from the post/reel article only.
    for sel in (
        "main article header a[href^='/']",
        "article header a[href^='/']",
    ):
        if username:
            break
        loc = page.locator(sel).first
        if loc.count() == 0:
            continue
        href = loc.get_attribute("href")
        if not href:
            continue
        mu = re.match(r"^/([A-Za-z0-9._]+)/?$", href)
        if mu and mu.group(1).lower() not in {"reel", "p", "explore"}:
            username = mu.group(1).lower()
            break

    # 3) Parse owner tied to this exact shortcode/code in embedded JSON.
    if not username:
        # Parse owner for the current shortcode to avoid unrelated usernames.
        rid = re.escape(reel_id)
        owner_patterns = [
            rf'"shortcode":"{rid}".{{0,40000}}?"owner":\{{"id":"\d+","username":"([^"]+)"',
            rf'"code":"{rid}".{{0,40000}}?"user":\{{"id":"\d+","username":"([^"]+)"',
        ]
        for pat in owner_patterns:
            mu = re.search(pat, html, flags=re.DOTALL)
            if mu:
                username = mu.group(1).lower()
                break

    if not username:
        return None

    return {
        "reel_url": url,
        "reel_id": reel_id,
        "username": username,
        "source_keyword": source_keyword,
    }


def advance_to_next_reel(page: Page) -> None:
    # Try keyboard navigation first, then clickable next controls.
    page.keyboard.press("ArrowDown")
    page.keyboard.press("PageDown")
    for sel in (
        "button[aria-label*='Next']",
        "button svg[aria-label*='Next']",
        "div[role='button'][aria-label*='Next']",
    ):
        btn = page.locator(sel).first
        if btn.count() > 0:
            try:
                btn.click(timeout=1200)
                break
            except Exception:
                pass
    time.sleep(1.2 + random.uniform(0.2, 0.7))


def goto_with_retry(page: Page, url: str, attempts: int = 3, base_sleep: float = 1.2) -> bool:
    for i in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded")
            return True
        except Exception as exc:
            print(f"goto failed ({i}/{attempts}) for {url}: {exc}", flush=True)
            if i == attempts:
                return False
            time.sleep(base_sleep * i)
    return False


def run() -> None:
    cfg = load_config(DEFAULT_CONFIG)
    keywords = [normalize_keyword(str(x)) for x in cfg.get("keywords", []) if str(x).strip()]
    excel_path = str(cfg.get("keywords_excel_path", "")).strip()
    excel_sheet = str(cfg.get("keywords_excel_sheet", "Sheet1")).strip()
    excel_limit = int(cfg.get("keywords_excel_limit", 0))
    if excel_path:
        excel_keywords = load_keywords_from_excel(excel_path, excel_sheet, limit=excel_limit)
        keywords.extend(excel_keywords)
    keywords = list(dict.fromkeys([k for k in keywords if k]))
    us_qualify_keywords = bool(cfg.get("us_qualify_broad_keywords", False))
    keywords = [qualify_keyword_for_us(k, enabled=us_qualify_keywords) for k in keywords]
    keywords = list(dict.fromkeys([k for k in keywords if k]))
    max_seed_reels = int(cfg.get("max_seed_reels_per_keyword", 5))
    reels_per_seed = int(cfg.get("reels_to_collect_per_seed", 25))
    max_seed_scroll_rounds = int(cfg.get("max_seed_scroll_rounds", 12))
    use_explore_fallback = bool(cfg.get("use_explore_fallback", True))
    inter_keyword_delay = float(cfg.get("inter_keyword_delay_seconds", 1.2))
    login_pause_seconds = int(cfg.get("startup_login_pause_seconds", 90))
    enforce_unique_profile_per_keyword = bool(cfg.get("enforce_unique_profile_per_keyword", False))
    max_unique_profiles_per_keyword = int(cfg.get("max_unique_profiles_per_keyword", 0))
    drop_likely_invalid_usernames = bool(cfg.get("drop_likely_invalid_usernames", False))

    env_state_dir = os.getenv("IG_PLAYWRIGHT_STATE_DIR", "").strip()
    state_dir_cfg = env_state_dir or str(cfg.get("playwright_state_dir", "")).strip()
    state_dir = (
        (ROOT / state_dir_cfg).resolve()
        if state_dir_cfg and not Path(state_dir_cfg).is_absolute()
        else Path(state_dir_cfg) if state_dir_cfg else DEFAULT_STATE_DIR
    )
    headless = bool(cfg.get("headless", False))

    ensure_dirs(state_dir)

    run_id = time.strftime("%Y%m%d_%H%M%S")
    out_csv = OUT_DIR / f"instagram_reels_for_you_{run_id}.csv"
    out_json = RAW_DIR / f"instagram_reels_for_you_{run_id}.json"
    live_csv = OUT_DIR / "instagram_reels_for_you_live.csv"
    seed_links_csv = OUT_DIR / "instagram_reels_seed_links_live.csv"

    print(
        f"Starting reels-for-you scrape: keywords={len(keywords)}, seed_per_keyword={max_seed_reels}, "
        f"reels_per_seed={reels_per_seed}, headless={headless}",
        flush=True,
    )

    checkpoint = load_checkpoint()
    all_rows: list[dict[str, Any]] = checkpoint.get("rows", [])
    seen_reel_ids: set[str] = set(checkpoint.get("seen_reel_ids", []))
    processed_keywords_raw: set[str] = set(checkpoint.get("processed_keywords", []))
    processed_keyword_keys: set[str] = {base_keyword_key(k) for k in processed_keywords_raw}
    seen_usernames_by_keyword: dict[str, set[str]] = {}
    for r in all_rows:
        kw = str(r.get("source_keyword", "")).strip().lower()
        un = normalize_username(str(r.get("username", "")))
        if kw and un:
            seen_usernames_by_keyword.setdefault(kw, set()).add(un)
    seed_rows = read_existing_seed_rows(seed_links_csv)
    seen_seed_urls = {str(r.get("seed_url", "")) for r in seed_rows}
    # Create/refresh live artifacts immediately so user can monitor from start.
    write_live_csv(live_csv, all_rows)
    save_checkpoint(all_rows, seen_reel_ids, processed_keywords_raw)

    with sync_playwright() as p:
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(state_dir),
            headless=headless,
            viewport={"width": 1440, "height": 920},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()
        startup_login_pause(page, login_pause_seconds)
        wait_for_manual_login(page)

        for idx, keyword in enumerate(keywords, start=1):
            keyword_key = base_keyword_key(keyword)
            if keyword_key in processed_keyword_keys:
                print(f"[{idx}/{len(keywords)}] keyword '{keyword}' already processed, skipping", flush=True)
                continue
            print(f"[{idx}/{len(keywords)}] keyword '{keyword}' -> collecting seed reels", flush=True)
            seeds = collect_seed_reels_from_search(
                page,
                keyword,
                max_seed_reels=max_seed_reels,
                max_rounds=max_seed_scroll_rounds,
            )
            if not seeds and use_explore_fallback:
                print(f"[{idx}/{len(keywords)}] keyword '{keyword}' no seeds, trying explore fallback", flush=True)
                seeds = collect_seed_reels_from_explore(
                    page,
                    max_seed_reels=max_seed_reels,
                    max_rounds=max_seed_scroll_rounds,
                )
            print(f"[{idx}/{len(keywords)}] keyword '{keyword}' seed reels found={len(seeds)}", flush=True)

            for seed_idx, seed_url in enumerate(seeds, start=1):
                if seed_url not in seen_seed_urls:
                    seen_seed_urls.add(seed_url)
                    seed_rows.append(
                        {
                            "source_keyword": keyword,
                            "seed_index": seed_idx,
                            "seed_total": len(seeds),
                            "seed_url": seed_url,
                        }
                    )
                    write_seed_links_csv(seed_links_csv, seed_rows)
                print(
                    f"[{idx}/{len(keywords)}] keyword '{keyword}' seed {seed_idx}/{len(seeds)} -> {seed_url}",
                    flush=True,
                )
                ok = goto_with_retry(page, seed_url, attempts=3, base_sleep=1.5)
                if not ok:
                    print(
                        f"[{idx}/{len(keywords)}] keyword '{keyword}' seed {seed_idx}: skipping after retries",
                        flush=True,
                    )
                    continue
                time.sleep(1.4 + random.uniform(0.3, 0.8))

                for step in range(1, reels_per_seed + 1):
                    rec = extract_reel_record(page, source_keyword=keyword)
                    if rec:
                        rec["username"] = normalize_username(str(rec.get("username", "")))
                        rid = rec["reel_id"]
                        if drop_likely_invalid_usernames and not is_likely_valid_username(rec["username"]):
                            advance_to_next_reel(page)
                            continue
                        if enforce_unique_profile_per_keyword:
                            known = seen_usernames_by_keyword.setdefault(keyword, set())
                            if rec["username"] in known:
                                advance_to_next_reel(page)
                                continue
                        if rid not in seen_reel_ids:
                            seen_reel_ids.add(rid)
                            seen_usernames_by_keyword.setdefault(keyword, set()).add(rec["username"])
                            rec["seed_url"] = seed_url
                            all_rows.append(rec)
                            print(
                                f"[{idx}/{len(keywords)}] '{keyword}' seed {seed_idx} step {step}: "
                                f"got @{rec['username']} reel_id={rid}",
                                flush=True,
                            )
                            write_live_csv(live_csv, all_rows)
                            save_checkpoint(all_rows, seen_reel_ids, processed_keywords_raw)
                            if (
                                enforce_unique_profile_per_keyword
                                and max_unique_profiles_per_keyword > 0
                                and len(seen_usernames_by_keyword.get(keyword, set())) >= max_unique_profiles_per_keyword
                            ):
                                print(
                                    f"[{idx}/{len(keywords)}] keyword '{keyword}' reached unique profile target "
                                    f"{max_unique_profiles_per_keyword}",
                                    flush=True,
                                )
                                break
                    advance_to_next_reel(page)
                if (
                    enforce_unique_profile_per_keyword
                    and max_unique_profiles_per_keyword > 0
                    and len(seen_usernames_by_keyword.get(keyword, set())) >= max_unique_profiles_per_keyword
                ):
                    break
            processed_keywords_raw.add(keyword)
            processed_keyword_keys.add(keyword_key)
            save_checkpoint(all_rows, seen_reel_ids, processed_keywords_raw)
            write_live_csv(live_csv, all_rows)
            time.sleep(inter_keyword_delay)

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["reel_url", "reel_id", "username", "source_keyword", "seed_url"],
        )
        writer.writeheader()
        writer.writerows(all_rows)

    out_json.write_text(json.dumps(all_rows, ensure_ascii=True, indent=2), encoding="utf-8")
    print(f"Rows collected: {len(all_rows)}", flush=True)
    print(f"Wrote CSV: {out_csv}", flush=True)
    print(f"Wrote JSON: {out_json}", flush=True)


if __name__ == "__main__":
    run()
