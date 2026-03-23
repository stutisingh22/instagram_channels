from __future__ import annotations

import csv
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any

import browser_cookie3
import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "out"
RAW_DIR = ROOT / "data" / "raw"


def load_config() -> dict[str, Any]:
    path = ROOT / "config.json"
    if not path.exists():
        raise RuntimeError("Missing config.json. Copy config.example.json first.")
    return json.loads(path.read_text(encoding="utf-8"))


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    ig_sessionid = os.getenv("IG_SESSIONID", "").strip()
    ig_csrftoken = os.getenv("IG_CSRFTOKEN", "").strip()
    if ig_sessionid:
        session.cookies.set("sessionid", ig_sessionid, domain=".instagram.com")
        if ig_csrftoken:
            session.cookies.set("csrftoken", ig_csrftoken, domain=".instagram.com")
            session.headers["X-CSRFToken"] = ig_csrftoken
        return session

    last_error: Exception | None = None
    loader_errors: list[str] = []
    for loader in (browser_cookie3.chrome, browser_cookie3.chromium, browser_cookie3.firefox):
        try:
            cj = loader(domain_name="instagram.com")
            if len(cj) == 0:
                loader_errors.append(f"{loader.__name__}: no cookies found")
                continue
            session.cookies.update(cj)
            return session
        except Exception as exc:
            last_error = exc
            loader_errors.append(f"{loader.__name__}: {exc}")

    detail = "; ".join(loader_errors) if loader_errors else str(last_error)
    raise RuntimeError(
        "Could not load Instagram browser cookies. "
        "Set IG_SESSIONID (and optionally IG_CSRFTOKEN) in .env as fallback. "
        f"Loader details: {detail}"
    )


def ensure_dirs() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)


def extract_post_links(html: str) -> list[str]:
    links = re.findall(r'href="(/p/[^"/]+/)"', html)
    if not links:
        # Instagram often escapes URLs inside JSON blobs.
        links = re.findall(r'"/p/([^"/]+)/"', html)
        links = [f"/p/{code}/" for code in links]
    out: list[str] = []
    seen: set[str] = set()
    for link in links:
        if link in seen:
            continue
        seen.add(link)
        out.append(f"https://www.instagram.com{link}")
    return out


def parse_username_from_post_html(html: str) -> str | None:
    patterns = [
        r'"owner"\s*:\s*\{"id":"\d+","username":"([^"]+)"',
        r'"username":"([^"]+)","is_private"',
        r'@([A-Za-z0-9._]+)\s+on Instagram',
    ]
    for pattern in patterns:
        m = re.search(pattern, html)
        if m:
            return m.group(1).strip().lower()
    return None


def parse_profile_info(html: str, username: str) -> dict[str, Any]:
    title_match = re.search(r"<title>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else ""

    desc_match = re.search(
        r'<meta name="description" content="(.*?)"',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    desc = desc_match.group(1).strip() if desc_match else ""

    name = None
    n = re.match(r"(.+?)\s+\(@" + re.escape(username) + r"\)", title, flags=re.IGNORECASE)
    if n:
        name = n.group(1).strip()

    followers = None
    f = re.search(r"([\d.,]+[MK]?)\s+Followers", desc, flags=re.IGNORECASE)
    if f:
        followers = f.group(1)

    bio = None
    b = re.search(r'on Instagram:\s*"(.*?)"', desc, flags=re.IGNORECASE | re.DOTALL)
    if b:
        bio = b.group(1).replace("\\n", " ").strip()

    return {
        "username": username,
        "name": name,
        "followers": followers,
        "bio": bio,
    }


def run() -> None:
    load_dotenv()
    cfg = load_config()
    ensure_dirs()
    session = build_session()

    hashtags: list[str] = cfg.get("hashtags", [])
    hashtag_posts_limit = int(cfg.get("hashtag_posts_limit", 30))
    sleep_seconds = float(cfg.get("sleep_seconds", 1.5))
    print(
        f"Starting browser-session scrape: {len(hashtags)} hashtags, "
        f"up to {hashtag_posts_limit} posts each",
        flush=True,
    )

    hashtag_usernames: dict[str, set[str]] = {}
    all_usernames: set[str] = set()
    hashtag_logs: list[dict[str, Any]] = []

    for idx, tag in enumerate(hashtags, start=1):
        hashtag_url = f"https://www.instagram.com/explore/tags/{tag}/"
        print(f"[{idx}/{len(hashtags)}] hashtag #{tag} -> fetching tag page", flush=True)
        try:
            r = session.get(hashtag_url, timeout=45)
            html = r.text
            if "/explore/search/keyword/" in r.url:
                print(
                    f"[{idx}/{len(hashtags)}] #{tag} redirected to keyword search page "
                    "(Instagram blocking direct hashtag page for scripted requests)",
                    flush=True,
                )
            if "challenge" in html.lower() or "login" in html.lower():
                print(
                    f"[{idx}/{len(hashtags)}] #{tag} page indicates login/challenge interstitial",
                    flush=True,
                )
            post_links = extract_post_links(html)[:hashtag_posts_limit]
            usernames: set[str] = set()
            print(
                f"[{idx}/{len(hashtags)}] #{tag} status={r.status_code}, "
                f"post links found={len(post_links)}",
                flush=True,
            )

            for post_idx, post_url in enumerate(post_links, start=1):
                try:
                    print(
                        f"[{idx}/{len(hashtags)}] #{tag} post {post_idx}/{len(post_links)} "
                        f"open post -> {post_url}",
                        flush=True,
                    )
                    post_resp = session.get(post_url, timeout=45)
                    uname = parse_username_from_post_html(post_resp.text)
                    if uname:
                        usernames.add(uname)
                        all_usernames.add(uname)
                        print(
                            f"[{idx}/{len(hashtags)}] #{tag} post {post_idx}/{len(post_links)} "
                            f"open post -> got @{uname} -> back",
                            flush=True,
                        )
                    else:
                        print(
                            f"[{idx}/{len(hashtags)}] #{tag} post {post_idx}/{len(post_links)} "
                            f"open post -> got @unknown -> back",
                            flush=True,
                        )
                except requests.RequestException:
                    print(
                        f"[{idx}/{len(hashtags)}] #{tag} post {post_idx}/{len(post_links)} "
                        "open post -> request failed -> back",
                        flush=True,
                    )
                    continue
                time.sleep(sleep_seconds + random.uniform(0.3, 1.0))

            hashtag_usernames[tag] = usernames
            hashtag_logs.append(
                {
                    "tag": tag,
                    "status_code": r.status_code,
                    "post_links_found": len(post_links),
                    "usernames_found": len(usernames),
                }
            )
            print(
                f"[{idx}/{len(hashtags)}] #{tag} done, usernames found={len(usernames)}",
                flush=True,
            )
        except requests.RequestException as exc:
            hashtag_logs.append(
                {
                    "tag": tag,
                    "status_code": None,
                    "post_links_found": 0,
                    "usernames_found": 0,
                    "error": str(exc),
                }
            )
            print(f"[{idx}/{len(hashtags)}] #{tag} failed: {exc}", flush=True)
        time.sleep(sleep_seconds + random.uniform(0.5, 1.2))

    (RAW_DIR / "browser_session_hashtag_logs.json").write_text(
        json.dumps(hashtag_logs, ensure_ascii=True, indent=2), encoding="utf-8"
    )

    rows: list[dict[str, Any]] = []
    usernames_sorted = sorted(all_usernames)
    print(f"Starting profile enrichment for {len(usernames_sorted)} usernames", flush=True)
    for i, username in enumerate(usernames_sorted, start=1):
        profile_url = f"https://www.instagram.com/{username}/"
        try:
            profile_resp = session.get(profile_url, timeout=45)
            if profile_resp.status_code >= 400:
                rows.append(
                    {
                        "profile_url": profile_url,
                        "username": username,
                        "name": None,
                        "followers": None,
                        "bio": None,
                        "source_hashtags": "",
                        "status_code": profile_resp.status_code,
                    }
                )
                continue

            info = parse_profile_info(profile_resp.text, username)
            source_tags = [t for t, users in hashtag_usernames.items() if username in users]
            rows.append(
                {
                    "profile_url": profile_url,
                    "username": username,
                    "name": info["name"],
                    "followers": info["followers"],
                    "bio": info["bio"],
                    "source_hashtags": "|".join(source_tags),
                    "status_code": profile_resp.status_code,
                }
            )
        except requests.RequestException:
            rows.append(
                {
                    "profile_url": profile_url,
                    "username": username,
                    "name": None,
                    "followers": None,
                    "bio": None,
                    "source_hashtags": "",
                    "status_code": None,
                }
            )
        if i % 25 == 0 or i == len(usernames_sorted):
            print(f"Enriched {i}/{len(usernames_sorted)} profiles", flush=True)
        time.sleep(sleep_seconds + random.uniform(0.4, 1.0))

    out_path = OUT_DIR / "browser_session_accounts.csv"
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "profile_url",
                "username",
                "name",
                "followers",
                "bio",
                "source_hashtags",
                "status_code",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Hashtags processed: {len(hashtags)}")
    print(f"Unique usernames discovered: {len(all_usernames)}")
    print(f"Wrote: {out_path}")


if __name__ == "__main__":
    run()
