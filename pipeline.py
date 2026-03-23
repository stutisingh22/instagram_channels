from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import instaloader
import pandas as pd
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "data" / "raw"
OUT_DIR = ROOT / "data" / "out"


@dataclass
class Config:
    seeds: list[str]
    hashtags: list[str]
    hashtag_posts_limit: int
    following_depth: int
    following_per_account_limit: int
    media_bio_keywords: list[str]
    min_followers: int
    allowed_categories: list[str]
    us_keywords: list[str]
    sleep_seconds: float


def read_config(path: Path) -> Config:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return Config(**data)


def ensure_dirs() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def norm_handle(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lower().lstrip("@")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True, indent=2)


def login_instagram() -> instaloader.Instaloader:
    username = os.getenv("IG_USERNAME", "").strip()
    password = os.getenv("IG_PASSWORD", "").strip()
    session_file = os.getenv("IG_SESSION_FILE", "ig_session").strip() or "ig_session"

    loader = instaloader.Instaloader(
        download_pictures=False,
        download_video_thumbnails=False,
        download_videos=False,
        download_comments=False,
        save_metadata=False,
    )

    try:
        loader.load_session_from_file(username, filename=session_file)
        print(f"Loaded Instagram session from {session_file}")
        return loader
    except Exception:
        pass

    if not username or not password:
        raise RuntimeError(
            "Missing IG_USERNAME/IG_PASSWORD and no valid saved session found."
        )

    loader.login(username, password)
    loader.save_session_to_file(filename=session_file)
    print(f"Logged in and saved session to {session_file}")
    return loader


def run_discovery(config: Config, loader: instaloader.Instaloader) -> pd.DataFrame:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    for seed in config.seeds:
        n = norm_handle(seed)
        if n and n not in seen:
            seen.add(n)
            candidates.append({"username": n, "source": "seed", "via": "seed", "depth": 0})

    for tag in config.hashtags:
        try:
            hashtag = instaloader.Hashtag.from_name(loader.context, tag)
            for i, post in enumerate(hashtag.get_posts()):
                if i >= config.hashtag_posts_limit:
                    break
                owner = norm_handle(post.owner_username)
                if owner and owner not in seen:
                    seen.add(owner)
                    candidates.append(
                        {"username": owner, "source": "hashtag", "via": tag, "depth": 0}
                    )
            time.sleep(config.sleep_seconds)
        except Exception as exc:
            print(f"Warning: hashtag crawl failed for #{tag}: {exc}")

    queue: deque[tuple[str, int]] = deque((norm_handle(s), 0) for s in config.seeds if norm_handle(s))
    expanded: set[str] = set()

    while queue:
        current, depth = queue.popleft()
        if current in expanded:
            continue
        expanded.add(current)

        try:
            profile = instaloader.Profile.from_username(loader.context, current)
        except Exception as exc:
            print(f"Warning: cannot load profile @{current}: {exc}")
            continue

        if depth >= config.following_depth:
            continue

        try:
            for i, followee in enumerate(profile.get_followees()):
                if i >= config.following_per_account_limit:
                    break
                handle = norm_handle(followee.username)
                if not handle:
                    continue
                if handle not in seen:
                    seen.add(handle)
                    candidates.append(
                        {
                            "username": handle,
                            "source": "following_graph",
                            "via": current,
                            "depth": depth + 1,
                        }
                    )
                if depth + 1 < config.following_depth:
                    queue.append((handle, depth + 1))
            time.sleep(config.sleep_seconds)
        except Exception as exc:
            print(f"Warning: followee crawl failed for @{current}: {exc}")

    if not candidates:
        raise RuntimeError("Discovery produced zero candidates.")

    df = pd.DataFrame(candidates).drop_duplicates(subset=["username"])
    df.to_csv(OUT_DIR / "discovered_accounts.csv", index=False)
    return df


def run_enrichment(
    config: Config, loader: instaloader.Instaloader, discovered_df: pd.DataFrame
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for username in discovered_df["username"].tolist():
        try:
            p = instaloader.Profile.from_username(loader.context, username)
            rows.append(
                {
                    "username": username,
                    "full_name": p.full_name,
                    "biography": p.biography,
                    "external_url": p.external_url,
                    "category": getattr(p, "business_category_name", None) or "",
                    "followers": p.followers,
                    "following": p.followees,
                    "is_verified": p.is_verified,
                    "is_private": p.is_private,
                }
            )
            time.sleep(config.sleep_seconds)
        except Exception as exc:
            print(f"Warning: enrich failed for @{username}: {exc}")

    enrich_df = pd.DataFrame(rows).drop_duplicates(subset=["username"])
    write_json(RAW_DIR / "profiles_enriched.json", rows)
    merged = discovered_df.merge(enrich_df, on="username", how="left")
    merged.to_csv(OUT_DIR / "enriched_accounts.csv", index=False)
    return merged


def apply_filters(config: Config, df: pd.DataFrame) -> pd.DataFrame:
    working = df.copy()
    working["followers"] = pd.to_numeric(working["followers"], errors="coerce").fillna(0).astype(int)
    working["category_lc"] = working["category"].fillna("").str.lower()
    working["biography_lc"] = working["biography"].fillna("").str.lower()
    working["full_name_lc"] = working["full_name"].fillna("").str.lower()
    working["external_url_lc"] = working["external_url"].fillna("").str.strip().str.lower()
    working["source_lc"] = working["source"].fillna("").str.lower()

    allowed = [x.lower() for x in config.allowed_categories]
    us_words = [w.lower() for w in config.us_keywords]
    media_words = [w.lower() for w in config.media_bio_keywords]

    category_ok = working["category_lc"].apply(lambda c: any(k in c for k in allowed))
    media_text_ok = (
        working["biography_lc"].apply(lambda b: any(k in b for k in media_words))
        | working["full_name_lc"].apply(lambda n: any(k in n for k in media_words))
    )
    website_empty = working["external_url_lc"].eq("")
    followers_ok = working["followers"] >= config.min_followers

    us_pattern = re.compile("|".join(re.escape(w) for w in us_words), re.IGNORECASE) if us_words else None
    us_ok = working["biography_lc"].apply(lambda x: bool(us_pattern.search(x)) if us_pattern else True)

    final = working[
        (category_ok | media_text_ok)
        & website_empty
        & followers_ok
        & us_ok
    ].copy()

    final["score"] = (
        3
        + 2 * (final["category_lc"].apply(lambda c: any(k in c for k in allowed))).astype(int)
        + 2 * (final["source_lc"].eq("following_graph")).astype(int)
        + 2 * followers_ok.loc[final.index].astype(int)
        + 2 * us_ok.loc[final.index].astype(int)
    )
    final = final.sort_values(["score", "followers"], ascending=[False, False])
    final.to_csv(OUT_DIR / "filtered_accounts.csv", index=False)
    return final


def main() -> int:
    load_dotenv()
    ensure_dirs()

    config_path = ROOT / "config.json"
    if not config_path.exists():
        print("Missing config.json. Copy config.example.json to config.json and edit values.")
        return 1

    config = read_config(config_path)
    try:
        loader = login_instagram()
        discovered = run_discovery(config, loader)
        enriched = run_enrichment(config, loader, discovered)
        filtered = apply_filters(config, enriched)
    except Exception as exc:
        print(f"Pipeline failed: {exc}")
        return 2

    print(f"Discovered handles: {len(discovered)}")
    print(f"Enriched profiles: {len(enriched)}")
    print(f"Filtered targets: {len(filtered)}")
    print(f"Outputs written to: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
