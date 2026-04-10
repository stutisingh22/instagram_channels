from __future__ import annotations

import csv
import json
import os
import random
import re
import shutil
import time
from pathlib import Path
from typing import Any

import pandas as pd
from playwright.sync_api import BrowserContext, Page, sync_playwright


ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "out"
RAW_DIR = ROOT / "data" / "raw"
STATE_DIR = ROOT / ".ig_browser_state"
CHECKPOINT_FILE = RAW_DIR / "instagram_search_checkpoint.json"

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


def load_config() -> dict[str, Any]:
    path = ROOT / "config.json"
    if not path.exists():
        raise RuntimeError("Missing config.json. Copy config.example.json first.")
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_dirs() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def get_state_dir() -> Path:
    custom = os.getenv("IG_PLAYWRIGHT_STATE_DIR", "").strip()
    if custom:
        p = Path(custom)
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    return STATE_DIR


def is_login_page(page: Page) -> bool:
    return "accounts/login" in page.url or page.locator("input[name='username']").count() > 0


def is_challenge_page(page: Page) -> bool:
    url = page.url.lower()
    return "challenge" in url or "checkpoint" in url or "captcha" in url


def wait_for_manual_login(page: Page, navigate_home: bool = True) -> None:
    if navigate_home:
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


def ensure_not_blocked(page: Page, reason: str) -> None:
    if is_challenge_page(page):
        print(f"Challenge/checkpoint detected ({reason}). Resolve in browser window.", flush=True)
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(3)
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
            if not is_challenge_page(page):
                break
        if is_challenge_page(page):
            raise RuntimeError("Challenge/checkpoint not resolved in time.")
    if is_login_page(page):
        # Important: stay on current page and do not force navigation while user types.
        wait_for_manual_login(page, navigate_home=False)


def normalize_followers_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().replace(",", "").upper()
    m = re.match(r"^(\d+(?:\.\d+)?)([MK])?$", text)
    if not m:
        return None
    base = float(m.group(1))
    suffix = m.group(2)
    if suffix == "M":
        base *= 1_000_000
    elif suffix == "K":
        base *= 1_000
    return int(base)


def normalize_username(value: str) -> str:
    return value.strip().lower().lstrip("@")


def load_checkpoint() -> dict[str, Any]:
    if not CHECKPOINT_FILE.exists():
        return {"processed_keywords": [], "keyword_logs": [], "profiles": {}}
    try:
        return json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"processed_keywords": [], "keyword_logs": [], "profiles": {}}


def save_checkpoint(payload: dict[str, Any]) -> None:
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def write_live_csv(profiles_by_username: dict[str, dict[str, Any]]) -> None:
    out_csv = OUT_DIR / "instagram_search_accounts.csv"
    write_live_csv_to_path(profiles_by_username, out_csv)


def write_live_csv_to_path(profiles_by_username: dict[str, dict[str, Any]], out_csv: Path) -> None:
    rows = sorted(profiles_by_username.values(), key=lambda x: str(x.get("username", "")))
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
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
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def read_profiles_csv(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            username = normalize_username(str(row.get("username", "")))
            if not username:
                continue
            out[username] = {
                "profile_url": row.get("profile_url"),
                "username": username,
                "name": row.get("name"),
                "followers": normalize_followers_value(row.get("followers")),
                "followers_raw": row.get("followers_raw"),
                "bio": row.get("bio"),
                "is_private": row.get("is_private"),
                "is_verified": row.get("is_verified"),
                "source_keywords": row.get("source_keywords"),
                "error": row.get("error"),
            }
    return out


def merge_profiles_maps(
    base: dict[str, dict[str, Any]],
    incoming: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    merged = dict(base)
    for username, row in incoming.items():
        u = normalize_username(username)
        if not u:
            continue
        prev = merged.get(u, {})
        merged_keywords = merge_source_keywords(prev.get("source_keywords", ""), str(row.get("source_keywords", "")))
        merged[u] = {
            "profile_url": row.get("profile_url") or prev.get("profile_url") or f"https://www.instagram.com/{u}/",
            "username": u,
            "name": row.get("name") or prev.get("name"),
            "followers": row.get("followers") if row.get("followers") is not None else prev.get("followers"),
            "followers_raw": row.get("followers_raw") or prev.get("followers_raw"),
            "bio": row.get("bio") or prev.get("bio"),
            "is_private": row.get("is_private") if row.get("is_private") is not None else prev.get("is_private"),
            "is_verified": row.get("is_verified") if row.get("is_verified") is not None else prev.get("is_verified"),
            "source_keywords": merged_keywords,
            "error": row.get("error") if row.get("error") not in (None, "") else prev.get("error"),
        }
    return merged


def profile_relevance_ok(
    row: dict[str, Any],
    include_terms: list[str],
    exclude_terms: list[str],
) -> bool:
    hay = " ".join(
        [
            str(row.get("username", "")).lower(),
            str(row.get("name", "")).lower(),
            str(row.get("bio", "")).lower(),
            str(row.get("source_keywords", "")).lower(),
        ]
    )
    if include_terms and not any(t in hay for t in include_terms):
        return False
    if exclude_terms and any(t in hay for t in exclude_terms):
        return False
    return True


def filter_profiles(
    profiles_by_username: dict[str, dict[str, Any]],
    min_followers: int,
    max_followers: int,
    include_terms: list[str],
    exclude_terms: list[str],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for username, row in profiles_by_username.items():
        followers = row.get("followers")
        if followers is None:
            followers = normalize_followers_value(row.get("followers_raw"))
        if followers is None:
            continue
        if followers < min_followers or followers > max_followers:
            continue
        if not profile_relevance_ok(row, include_terms, exclude_terms):
            continue
        out[username] = row
    return out


def merge_source_keywords(existing_value: str, keyword: str) -> str:
    parts = [x.strip() for x in str(existing_value).split("|") if x.strip()]
    s = set(parts)
    s.add(keyword)
    return "|".join(sorted(s))


def csv_data_row_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        lines = f.readlines()
    return max(0, len(lines) - 1)


def write_main_csv_if_progress(
    profiles_by_username: dict[str, dict[str, Any]],
    main_csv: Path,
    run_csv: Path,
) -> None:
    new_count = len(profiles_by_username)
    current_count = csv_data_row_count(main_csv)
    # Never clobber the main file with empty data.
    if new_count == 0:
        print(
            f"Main CSV not updated (new_count=0, current_count={current_count}).",
            flush=True,
        )
        return
    # Avoid replacing main with a smaller set from a partial/bad run.
    if current_count > 0 and new_count < current_count:
        print(
            f"Main CSV not updated (new_count={new_count} < current_count={current_count}). "
            f"Run CSV remains at {run_csv}",
            flush=True,
        )
        return
    if main_csv.exists():
        backup = OUT_DIR / f"{main_csv.stem}.bak.csv"
        shutil.copy2(main_csv, backup)
    write_live_csv_to_path(profiles_by_username, main_csv)
    print(
        f"Main CSV updated safely: {new_count} rows -> {main_csv}",
        flush=True,
    )


def normalize_keyword(value: str) -> str:
    text = value.strip()
    while text.startswith("#"):
        text = text[1:]
    return text.strip().lower()


def load_keywords_from_excel(path: Path, sheet_name: str | None = None) -> list[str]:
    if not path.exists():
        return []
    kwargs: dict[str, Any] = {}
    if sheet_name:
        kwargs["sheet_name"] = sheet_name
    df = pd.read_excel(path, **kwargs)
    if df.empty:
        return []
    keywords: list[str] = []
    for col in df.columns:
        for cell in df[col].dropna().astype(str).tolist():
            k = normalize_keyword(cell)
            if k:
                keywords.append(k)
    # Keep order while deduping
    return list(dict.fromkeys(keywords))


def load_keywords_from_json(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    keywords: list[str] = []
    for item in payload:
        k = normalize_keyword(str(item))
        if k:
            keywords.append(k)
    return list(dict.fromkeys(keywords))


def recompute_processed_keywords(keyword_logs: list[dict[str, Any]]) -> set[str]:
    processed: set[str] = set()
    for row in keyword_logs:
        keyword = str(row.get("keyword", "")).strip()
        if not keyword:
            continue
        found = int(row.get("usernames_found", 0) or 0)
        err = row.get("error")
        if found > 0 and not err:
            processed.add(keyword)
    return processed


def parse_profile_meta(page: Page, username: str) -> dict[str, Any]:
    title = page.title() or ""
    desc = ""
    try:
        desc = page.locator("meta[name='description']").first.get_attribute("content") or ""
    except Exception:
        desc = ""

    name = None
    t = re.match(r"(.+?)\s+\(@" + re.escape(username) + r"\)", title, flags=re.IGNORECASE)
    if t:
        name = t.group(1).strip()

    followers = None
    f = re.search(r"([\d.,]+[MK]?)\s+Followers", desc, flags=re.IGNORECASE)
    if f:
        followers = f.group(1)

    bio = None
    b = re.search(r'on Instagram:\s*"(.*?)"', desc, flags=re.IGNORECASE | re.DOTALL)
    if b:
        bio = b.group(1).replace("\\n", " ").strip()

    return {"name": name, "followers": followers, "bio": bio}


def collect_usernames_from_search(page: Page, keyword: str, max_accounts: int, sleep_s: float) -> list[str]:
    search_url = f"https://www.instagram.com/explore/search/keyword/?q={keyword.replace(' ', '%20')}"
    page.goto(search_url, wait_until="domcontentloaded")
    time.sleep(2.5)
    if is_login_page(page):
        wait_for_manual_login(page)
        page.goto(search_url, wait_until="domcontentloaded")
        time.sleep(2.5)

    usernames: list[str] = []
    seen: set[str] = set()
    stagnant = 0

    for round_idx in range(1, 5):
        hrefs = page.eval_on_selector_all(
            "a[href^='/'][role='link'], a[href^='/']",
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)",
        )
        before = len(usernames)
        for href in hrefs:
            m = re.match(r"^/([A-Za-z0-9._]+)/?$", href)
            if not m:
                continue
            handle = m.group(1).lower()
            if handle in RESERVED_HANDLES:
                continue
            if handle in seen:
                continue
            seen.add(handle)
            usernames.append(handle)
            if len(usernames) >= max_accounts:
                break

        print(
            f"search '{keyword}' round {round_idx}: usernames {len(usernames)}/{max_accounts}",
            flush=True,
        )
        if len(usernames) >= max_accounts:
            break
        if len(usernames) == before:
            stagnant += 1
        else:
            stagnant = 0
        if stagnant >= 4:
            break

        page.mouse.wheel(0, 2800)
        time.sleep(sleep_s + random.uniform(0.4, 1.2))

    return usernames


def collect_usernames_via_topsearch(page: Page, keyword: str, max_accounts: int) -> list[str]:
    """Use Instagram authenticated web search API via browser context cookies."""
    encoded = keyword.replace(" ", "%20")
    url = f"https://www.instagram.com/web/search/topsearch/?query={encoded}&count={max_accounts}"
    resp = page.request.get(
        url,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.instagram.com/",
        },
        timeout=45000,
    )
    if resp.status != 200:
        raise RuntimeError(f"topsearch status={resp.status}")

    payload = resp.json()
    users = payload.get("users", []) if isinstance(payload, dict) else []
    usernames: list[str] = []
    seen: set[str] = set()
    for item in users:
        user = item.get("user", {}) if isinstance(item, dict) else {}
        username = str(user.get("username", "")).strip().lower()
        if not username or username in seen:
            continue
        seen.add(username)
        usernames.append(username)
        if len(usernames) >= max_accounts:
            break
    return usernames


def collect_profiles_via_topsearch(page: Page, keyword: str, max_accounts: int) -> dict[str, dict[str, Any]]:
    encoded = keyword.replace(" ", "%20")
    url = f"https://www.instagram.com/web/search/topsearch/?query={encoded}&count={max_accounts}"
    resp = page.request.get(
        url,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.instagram.com/",
        },
        timeout=45000,
    )
    if resp.status != 200:
        raise RuntimeError(f"topsearch_http_{resp.status}")

    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("topsearch returned non-dict payload")
    users = payload.get("users", [])
    if not isinstance(users, list):
        raise RuntimeError("topsearch payload users is not a list")

    profiles: dict[str, dict[str, Any]] = {}
    for item in users:
        if not isinstance(item, dict):
            continue
        user = item.get("user", {})
        if not isinstance(user, dict):
            continue
        username = str(user.get("username", "")).strip().lower()
        if not username or username in RESERVED_HANDLES:
            continue

        profiles[username] = {
            "profile_url": f"https://www.instagram.com/{username}/",
            "username": username,
            "name": user.get("full_name"),
            "followers": normalize_followers_value(user.get("follower_count")),
            "followers_raw": user.get("follower_count"),
            "bio": user.get("biography"),
            "is_private": user.get("is_private"),
            "is_verified": user.get("is_verified"),
        }
        if len(profiles) >= max_accounts:
            break
    return profiles


def run() -> None:
    ensure_dirs()
    cfg = load_config()
    keywords: list[str] = [normalize_keyword(str(x)) for x in cfg.get("search_keywords_browser", []) if str(x).strip()]
    excel_path_cfg = str(cfg.get("search_keywords_excel_path", "")).strip()
    if excel_path_cfg:
        excel_path = (ROOT / excel_path_cfg).resolve() if not Path(excel_path_cfg).is_absolute() else Path(excel_path_cfg)
        excel_sheet = str(cfg.get("search_keywords_excel_sheet", "")).strip() or None
        excel_keywords = load_keywords_from_excel(excel_path, sheet_name=excel_sheet)
        if excel_keywords:
            keywords = list(dict.fromkeys(excel_keywords))
            print(
                f"Loaded {len(keywords)} keywords from Excel: {excel_path}",
                flush=True,
            )
    extra_keywords_path_cfg = str(cfg.get("search_keywords_extra_path", "")).strip()
    if extra_keywords_path_cfg:
        extra_path = (
            (ROOT / extra_keywords_path_cfg).resolve()
            if not Path(extra_keywords_path_cfg).is_absolute()
            else Path(extra_keywords_path_cfg)
        )
        extra_keywords = load_keywords_from_json(extra_path)
        if extra_keywords:
            keywords = list(dict.fromkeys([*keywords, *extra_keywords]))
            print(
                f"Loaded {len(extra_keywords)} extra keywords from JSON: {extra_path}",
                flush=True,
            )
    max_per_keyword = int(cfg.get("search_accounts_limit_per_keyword", 80))
    sleep_seconds = float(cfg.get("sleep_seconds", 1.3))
    inter_keyword_delay = float(cfg.get("inter_keyword_delay_seconds", 3.5))
    enrich_missing_via_profile = bool(cfg.get("search_enrich_missing_via_profile", False))
    min_followers_filter = int(cfg.get("search_min_followers", 9000))
    max_followers_filter = int(cfg.get("search_max_followers", 50000))
    include_terms = [str(x).strip().lower() for x in cfg.get("search_relevance_keywords", []) if str(x).strip()]
    exclude_terms = [str(x).strip().lower() for x in cfg.get("search_exclude_keywords", []) if str(x).strip()]
    accept_per_keyword_cap = int(cfg.get("search_accept_per_keyword_cap", 20))
    append_safe_mode = bool(cfg.get("search_append_safe_mode", False))
    append_safe_main_name = str(cfg.get("search_append_safe_main_name", "instagram_search_accounts_append_safe.csv")).strip()
    headless = os.getenv("IG_PLAYWRIGHT_HEADLESS", "false").lower() == "true"

    print(
        f"Starting search-browser scrape: keywords={len(keywords)}, per_keyword={max_per_keyword}, headless={headless}",
        flush=True,
    )
    run_id = time.strftime("%Y%m%d_%H%M%S")
    run_csv = OUT_DIR / f"instagram_search_accounts_run_{run_id}.csv"
    main_csv = OUT_DIR / "instagram_search_accounts.csv"
    append_safe_csv = OUT_DIR / append_safe_main_name
    filtered_run_csv = OUT_DIR / f"instagram_search_accounts_filtered_9k_50k_run_{run_id}.csv"
    filtered_main_csv = OUT_DIR / "instagram_search_accounts_filtered_9k_50k.csv"
    print(f"Run-specific CSV: {run_csv}", flush=True)

    checkpoint = load_checkpoint()
    processed_keywords: set[str] = recompute_processed_keywords(checkpoint.get("keyword_logs", []))
    keyword_logs: list[dict[str, Any]] = checkpoint.get("keyword_logs", [])
    profiles_by_username: dict[str, dict[str, Any]] = checkpoint.get("profiles", {})
    for row in profiles_by_username.values():
        existing = str(row.get("source_keywords", "")).strip()
        row["source_keywords"] = existing

    with sync_playwright() as p:
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(get_state_dir()),
            headless=headless,
            viewport={"width": 1440, "height": 920},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()
        wait_for_manual_login(page)
        ensure_not_blocked(page, "initial check")

        try:
            for i, keyword in enumerate(keywords, start=1):
                if keyword in processed_keywords:
                    print(f"[{i}/{len(keywords)}] keyword '{keyword}' already processed, skipping", flush=True)
                    continue
                print(f"[{i}/{len(keywords)}] search keyword -> '{keyword}'", flush=True)
                try:
                    ensure_not_blocked(page, f"before keyword '{keyword}'")
                    topsearch_profiles: dict[str, dict[str, Any]] = {}
                    topsearch_error: str | None = None
                    try:
                        topsearch_profiles = collect_profiles_via_topsearch(page, keyword, max_per_keyword)
                        print(
                            f"[{i}/{len(keywords)}] '{keyword}' topsearch usernames={len(topsearch_profiles)}",
                            flush=True,
                        )
                    except Exception as exc:
                        topsearch_error = str(exc)
                        print(
                            f"[{i}/{len(keywords)}] '{keyword}' topsearch unavailable ({topsearch_error}); using DOM fallback",
                            flush=True,
                        )

                    if topsearch_profiles:
                        accepted_for_keyword = 0
                        for username, profile_data in topsearch_profiles.items():
                            uname = normalize_username(username)
                            existing = profiles_by_username.get(uname, {})
                            merged = {
                                **existing,
                                **profile_data,
                                "username": uname,
                                "profile_url": f"https://www.instagram.com/{uname}/",
                                "source_keywords": merge_source_keywords(existing.get("source_keywords", ""), keyword),
                                "error": None,
                            }
                            followers_now = merged.get("followers")
                            if followers_now is None:
                                followers_now = normalize_followers_value(merged.get("followers_raw"))
                                merged["followers"] = followers_now
                            # Controlled mid-tier acceptance: keep unknowns for enrichment, accept in-band directly.
                            in_band_or_unknown = followers_now is None or (
                                min_followers_filter <= int(followers_now) <= max_followers_filter
                            )
                            if in_band_or_unknown:
                                profiles_by_username[uname] = merged
                                accepted_for_keyword += 1
                            if accept_per_keyword_cap > 0 and accepted_for_keyword >= accept_per_keyword_cap:
                                break
                        found_count = len(topsearch_profiles)
                    else:
                        users = collect_usernames_from_search(page, keyword, max_per_keyword, sleep_seconds)
                        users_set = set(users)
                        accepted_for_keyword = 0
                        for username in users_set:
                            uname = normalize_username(username)
                            existing = profiles_by_username.get(uname, {})
                            profiles_by_username[uname] = {
                                "profile_url": f"https://www.instagram.com/{uname}/",
                                "username": uname,
                                "name": existing.get("name"),
                                "followers": existing.get("followers"),
                                "followers_raw": existing.get("followers_raw"),
                                "bio": existing.get("bio"),
                                "is_private": existing.get("is_private"),
                                "is_verified": existing.get("is_verified"),
                                "source_keywords": merge_source_keywords(existing.get("source_keywords", ""), keyword),
                                "error": existing.get("error") or topsearch_error,
                            }
                            accepted_for_keyword += 1
                            if accept_per_keyword_cap > 0 and accepted_for_keyword >= accept_per_keyword_cap:
                                break
                        found_count = len(users_set)
                    keyword_logs.append(
                        {"keyword": keyword, "usernames_found": found_count, "error": None}
                    )
                    print(
                        f"[{i}/{len(keywords)}] '{keyword}' done, usernames found={found_count}",
                        flush=True,
                    )
                except Exception as exc:
                    keyword_logs.append(
                        {"keyword": keyword, "usernames_found": 0, "error": str(exc)}
                    )
                    print(f"[{i}/{len(keywords)}] '{keyword}' failed: {exc}", flush=True)
                processed_keywords.add(keyword)
                save_checkpoint(
                    {
                        "processed_keywords": sorted(processed_keywords),
                        "keyword_logs": keyword_logs,
                        "profiles": profiles_by_username,
                    }
                )
                write_live_csv_to_path(profiles_by_username, run_csv)
                if append_safe_mode:
                    base_map = read_profiles_csv(append_safe_csv)
                    merged_map = merge_profiles_maps(base_map, profiles_by_username)
                    write_live_csv_to_path(merged_map, append_safe_csv)
                else:
                    write_main_csv_if_progress(profiles_by_username, main_csv, run_csv)
                filtered = filter_profiles(
                    profiles_by_username,
                    min_followers=min_followers_filter,
                    max_followers=max_followers_filter,
                    include_terms=include_terms,
                    exclude_terms=exclude_terms,
                )
                write_live_csv_to_path(filtered, filtered_run_csv)
                # Keep filtered main file always present/updated, even when 0 rows.
                write_live_csv_to_path(filtered, filtered_main_csv)
                print(
                    f"Run CSV updated: {len(profiles_by_username)} rows -> {run_csv}",
                    flush=True,
                )
                time.sleep(inter_keyword_delay + random.uniform(0.7, 1.8))
        except KeyboardInterrupt:
            print("Interrupted by user. Saving checkpoint and live CSV before exit...", flush=True)
            save_checkpoint(
                {
                    "processed_keywords": sorted(processed_keywords),
                    "keyword_logs": keyword_logs,
                    "profiles": profiles_by_username,
                }
            )
            write_live_csv_to_path(profiles_by_username, run_csv)
            if append_safe_mode:
                base_map = read_profiles_csv(append_safe_csv)
                merged_map = merge_profiles_maps(base_map, profiles_by_username)
                write_live_csv_to_path(merged_map, append_safe_csv)
            else:
                write_main_csv_if_progress(profiles_by_username, main_csv, run_csv)
            filtered = filter_profiles(
                profiles_by_username,
                min_followers=min_followers_filter,
                max_followers=max_followers_filter,
                include_terms=include_terms,
                exclude_terms=exclude_terms,
            )
            write_live_csv_to_path(filtered, filtered_run_csv)
            write_live_csv_to_path(filtered, filtered_main_csv)
            context.close()
            return

        if enrich_missing_via_profile:
            rows_to_visit = [u for u, row in profiles_by_username.items() if not row.get("followers")]
            print(f"Optional profile enrichment for {len(rows_to_visit)} missing profiles", flush=True)
            for idx, username in enumerate(rows_to_visit, start=1):
                profile_url = f"https://www.instagram.com/{username}/"
                try:
                    page.goto(profile_url, wait_until="domcontentloaded")
                    ensure_not_blocked(page, f"profile enrich @{username}")
                    time.sleep(1.0 + random.uniform(0.3, 0.9))
                    meta = parse_profile_meta(page, username)
                    row = profiles_by_username.get(username, {})
                    row["name"] = row.get("name") or meta.get("name")
                    row["followers_raw"] = row.get("followers_raw") or meta.get("followers")
                    row["followers"] = row.get("followers") or normalize_followers_value(meta.get("followers"))
                    row["bio"] = row.get("bio") or meta.get("bio")
                    row["error"] = None
                    profiles_by_username[username] = row
                except Exception as exc:
                    row = profiles_by_username.get(username, {})
                    row["error"] = str(exc)
                    profiles_by_username[username] = row
                if idx % 10 == 0 or idx == len(rows_to_visit):
                    # Persist enrichment progress live so output files update continuously.
                    save_checkpoint(
                        {
                            "processed_keywords": sorted(processed_keywords),
                            "keyword_logs": keyword_logs,
                            "profiles": profiles_by_username,
                        }
                    )
                    write_live_csv_to_path(profiles_by_username, run_csv)
                    if append_safe_mode:
                        base_map = read_profiles_csv(append_safe_csv)
                        merged_map = merge_profiles_maps(base_map, profiles_by_username)
                        write_live_csv_to_path(merged_map, append_safe_csv)
                    else:
                        write_main_csv_if_progress(profiles_by_username, main_csv, run_csv)
                    filtered_live = filter_profiles(
                        profiles_by_username,
                        min_followers=min_followers_filter,
                        max_followers=max_followers_filter,
                        include_terms=include_terms,
                        exclude_terms=exclude_terms,
                    )
                    write_live_csv_to_path(filtered_live, filtered_run_csv)
                    write_live_csv_to_path(filtered_live, filtered_main_csv)
                    print(
                        f"Optional enriched {idx}/{len(rows_to_visit)} profiles; live files updated",
                        flush=True,
                    )
                time.sleep(sleep_seconds + random.uniform(0.3, 1.0))

        context.close()

    (RAW_DIR / "instagram_search_keyword_logs.json").write_text(
        json.dumps(keyword_logs, ensure_ascii=True, indent=2), encoding="utf-8"
    )

    write_live_csv_to_path(profiles_by_username, run_csv)
    if append_safe_mode:
        base_map = read_profiles_csv(append_safe_csv)
        merged_map = merge_profiles_maps(base_map, profiles_by_username)
        write_live_csv_to_path(merged_map, append_safe_csv)
    else:
        write_main_csv_if_progress(profiles_by_username, main_csv, run_csv)
    filtered = filter_profiles(
        profiles_by_username,
        min_followers=min_followers_filter,
        max_followers=max_followers_filter,
        include_terms=include_terms,
        exclude_terms=exclude_terms,
    )
    write_live_csv_to_path(filtered, filtered_run_csv)
    write_live_csv_to_path(filtered, filtered_main_csv)

    print(f"Keywords processed: {len(keywords)}", flush=True)
    print(f"Unique usernames discovered: {len(profiles_by_username)}", flush=True)
    print(f"Filtered 9k-50k relevant usernames: {len(filtered)}", flush=True)
    print(f"Wrote run CSV: {run_csv}", flush=True)
    print(f"Main CSV path: {main_csv}", flush=True)
    print(f"Filtered CSV path: {filtered_main_csv}", flush=True)


if __name__ == "__main__":
    run()
