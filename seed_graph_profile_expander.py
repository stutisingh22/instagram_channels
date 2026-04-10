from __future__ import annotations

import csv
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from playwright.sync_api import BrowserContext, Page, sync_playwright
import requests


ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "data" / "raw"
CONFIG_PATH = ROOT / "seed_graph_profile_expander_config.json"
CHECKPOINT_PATH = RAW_DIR / "seed_graph_profile_expander_checkpoint.json"


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


def ensure_dirs() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Missing config file: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def normalize_username(v: str) -> str:
    return str(v).strip().lower().lstrip("@")


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


def is_login_page(page: Page) -> bool:
    return "accounts/login" in page.url or page.locator("input[name='username']").count() > 0


def wait_for_manual_login(page: Page) -> None:
    page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    time.sleep(2)
    if is_login_page(page):
        print("Login required in Playwright browser window.", flush=True)
        print("Please login manually. Waiting up to 10 minutes...", flush=True)
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(3)
            if not is_login_page(page):
                break
    if is_login_page(page):
        raise RuntimeError("Still on login page after wait.")


def load_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise RuntimeError(f"CSV not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [dict(r) for r in reader]


def read_existing_usernames(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        out: set[str] = set()
        for row in reader:
            u = normalize_username(row.get("username", ""))
            if u:
                out.add(u)
        return out


def load_checkpoint() -> dict[str, Any]:
    if not CHECKPOINT_PATH.exists():
        return {"processed_seeds": [], "appended_usernames": []}
    try:
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"processed_seeds": [], "appended_usernames": []}


def save_checkpoint(processed_seeds: set[str], appended_usernames: set[str]) -> None:
    payload = {
        "processed_seeds": sorted(processed_seeds),
        "appended_usernames": sorted(appended_usernames),
    }
    CHECKPOINT_PATH.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


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
    write_header = (not path.exists()) or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def build_queries(seed_row: dict[str, str]) -> list[str]:
    username = normalize_username(seed_row.get("username", ""))
    source_keywords = str(seed_row.get("source_keywords", "")).strip()
    q: list[str] = []
    if username:
        q.extend([username, f"{username} news", f"{username} media"])
    if source_keywords:
        for part in source_keywords.split("|"):
            p = part.strip().lower()
            if p:
                q.append(p)
                q.append(f"{p} usa")
    # Keep order, dedupe.
    return list(dict.fromkeys([x for x in q if x]))


def collect_profiles_via_topsearch(page: Page, query: str, max_accounts: int) -> dict[str, dict[str, Any]]:
    encoded = query.replace(" ", "%20")
    url = f"https://www.instagram.com/web/search/topsearch/?query={encoded}&count={max_accounts}"
    resp = page.request.get(
        url,
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": "https://www.instagram.com/"},
        timeout=45000,
    )
    if resp.status != 200:
        return {}

    try:
        payload = resp.json()
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    users = payload.get("users", [])
    if not isinstance(users, list):
        return {}

    out: dict[str, dict[str, Any]] = {}
    for item in users:
        user = item.get("user", {}) if isinstance(item, dict) else {}
        username = normalize_username(user.get("username", ""))
        if not username or username in RESERVED_HANDLES:
            continue
        out[username] = {
            "profile_url": f"https://www.instagram.com/{username}/",
            "username": username,
            "name": user.get("full_name"),
            "followers": normalize_followers_value(user.get("follower_count")),
            "followers_raw": user.get("follower_count"),
            "bio": user.get("biography"),
            "is_private": user.get("is_private"),
            "is_verified": user.get("is_verified"),
            "source_keywords": query,
            "error": "",
        }
        if len(out) >= max_accounts:
            break
    return out


def collect_usernames_via_ddg(query: str, max_accounts: int) -> list[str]:
    url = f"https://duckduckgo.com/html/?q={quote(query + ' site:instagram.com')}"
    try:
        resp = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception:
        return []
    matches = re.findall(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/?", resp.text)
    out: list[str] = []
    seen: set[str] = set()
    for m in matches:
        u = normalize_username(m)
        if not u or u in RESERVED_HANDLES or u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= max_accounts:
            break
    return out


def collect_similar_usernames_from_profile_page(page: Page, seed_username: str, max_accounts: int) -> list[str]:
    profile_url = f"https://www.instagram.com/{seed_username}/"
    try:
        page.goto(profile_url, wait_until="domcontentloaded")
    except Exception:
        return []
    time.sleep(1.8)

    # Try to surface "suggested" rail by a bit of scroll.
    try:
        page.mouse.wheel(0, 1200)
        time.sleep(0.8)
    except Exception:
        pass

    hrefs = page.eval_on_selector_all(
        "a[href^='/']",
        "els => els.map(e => e.getAttribute('href')).filter(Boolean)",
    )
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


def enrich_usernames_via_topsearch(page: Page, usernames: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for u in usernames:
        if u in out:
            continue
        try:
            prof = collect_profiles_via_topsearch(page, u, max_accounts=1)
        except Exception:
            prof = {}
        if prof:
            out.update(prof)
        else:
            out[u] = {
                "profile_url": f"https://www.instagram.com/{u}/",
                "username": u,
                "name": "",
                "followers": None,
                "followers_raw": "",
                "bio": "",
                "is_private": "",
                "is_verified": "",
                "source_keywords": f"similar:{u}",
                "error": "similar_profile_minimal",
            }
        time.sleep(0.2)
    return out


def run() -> None:
    ensure_dirs()
    cfg = load_config()

    seed_csv = Path(str(cfg.get("seed_input_csv", "")).strip())
    if not seed_csv.is_absolute():
        seed_csv = (ROOT / seed_csv).resolve()
    target_csv = Path(str(cfg.get("target_append_csv", "")).strip())
    if not target_csv.is_absolute():
        target_csv = (ROOT / target_csv).resolve()

    max_seeds = int(cfg.get("max_seeds", 200))
    max_accounts_per_query = int(cfg.get("max_accounts_per_query", 20))
    min_followers = int(cfg.get("min_followers", 9000))
    max_followers = int(cfg.get("max_followers", 80000))
    inter_query_sleep = float(cfg.get("inter_query_sleep_seconds", 0.6))
    inter_seed_sleep = float(cfg.get("inter_seed_sleep_seconds", 1.0))

    seed_rows = load_csv_rows(seed_csv)[:max_seeds]
    existing_usernames = read_existing_usernames(target_csv)
    cp = load_checkpoint()
    processed_seeds = set(cp.get("processed_seeds", []))
    appended_usernames = set(cp.get("appended_usernames", []))
    existing_usernames |= appended_usernames

    print(
        f"Seed expander start: seeds={len(seed_rows)}, existing={len(existing_usernames)}, target={target_csv}",
        flush=True,
    )

    with sync_playwright() as p:
        state_dir_cfg = os.getenv("IG_PLAYWRIGHT_STATE_DIR", "").strip() or ".ig_login_only_safe_run1"
        state_dir = (ROOT / state_dir_cfg).resolve() if not Path(state_dir_cfg).is_absolute() else Path(state_dir_cfg)
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(state_dir),
            headless=False,
            viewport={"width": 1440, "height": 920},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()
        wait_for_manual_login(page)

        for i, seed in enumerate(seed_rows, start=1):
            seed_username = normalize_username(seed.get("username", ""))
            if not seed_username:
                continue
            if seed_username in processed_seeds:
                continue

            queries = build_queries(seed)
            print(f"[{i}/{len(seed_rows)}] seed @{seed_username} queries={len(queries)}", flush=True)

            append_batch: list[dict[str, Any]] = []
            added_this_seed = 0
            skip_existing = 0
            skip_band = 0
            similar_found = 0
            similar_added = 0
            query_found = 0
            query_added = 0
            # Primary path: profile-page "similar/suggested" discovery.
            try:
                similar_usernames = collect_similar_usernames_from_profile_page(
                    page, seed_username, max_accounts=max_accounts_per_query
                )
            except Exception:
                similar_usernames = []
            if similar_usernames:
                similar_found = len(similar_usernames)
                try:
                    similar_profiles = enrich_usernames_via_topsearch(page, similar_usernames)
                except Exception:
                    similar_profiles = {}
                for username, row in similar_profiles.items():
                    if username in existing_usernames:
                        skip_existing += 1
                        continue
                    followers = row.get("followers")
                    if followers is not None and (followers < min_followers or followers > max_followers):
                        skip_band += 1
                        continue
                    existing_usernames.add(username)
                    appended_usernames.add(username)
                    row["source_keywords"] = f"similar_to:{seed_username}"
                    append_batch.append(row)
                    added_this_seed += 1
                    similar_added += 1
                if append_batch:
                    append_rows(target_csv, append_batch)
                    append_batch = []

            for q in queries:
                found = collect_profiles_via_topsearch(page, q, max_accounts=max_accounts_per_query)
                query_found += len(found)
                if not found:
                    # Fallback discovery path to surface additional handles.
                    fallback_users = collect_usernames_via_ddg(q, max_accounts=max_accounts_per_query)
                    for u in fallback_users:
                        if u in existing_usernames:
                            skip_existing += 1
                            continue
                        existing_usernames.add(u)
                        appended_usernames.add(u)
                        append_batch.append(
                            {
                                "profile_url": f"https://www.instagram.com/{u}/",
                                "username": u,
                                "name": "",
                                "followers": "",
                                "followers_raw": "",
                                "bio": "",
                                "is_private": "",
                                "is_verified": "",
                                "source_keywords": q,
                                "error": "ddg_fallback_unenriched",
                            }
                        )
                        added_this_seed += 1
                        query_added += 1
                for username, row in found.items():
                    if username in existing_usernames:
                        skip_existing += 1
                        continue
                    followers = row.get("followers")
                    if followers is not None and (followers < min_followers or followers > max_followers):
                        skip_band += 1
                        continue
                    existing_usernames.add(username)
                    appended_usernames.add(username)
                    append_batch.append(row)
                    added_this_seed += 1
                    query_added += 1
                if append_batch:
                    append_rows(target_csv, append_batch)
                    append_batch = []
                time.sleep(inter_query_sleep + random.uniform(0.1, 0.5))

            processed_seeds.add(seed_username)
            save_checkpoint(processed_seeds, appended_usernames)
            print(
                f"[{i}/{len(seed_rows)}] seed @{seed_username} done; "
                f"similar_found={similar_found}, similar_added={similar_added}, "
                f"query_found={query_found}, query_added={query_added}, added={added_this_seed}, "
                f"skip_existing={skip_existing}, skip_band={skip_band}, total_appended={len(appended_usernames)}",
                flush=True,
            )
            time.sleep(inter_seed_sleep + random.uniform(0.1, 0.6))

        context.close()

    print("Seed expander done.", flush=True)


if __name__ == "__main__":
    run()
