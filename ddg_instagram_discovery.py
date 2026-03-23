from __future__ import annotations

import csv
import json
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "out"
RAW_DIR = ROOT / "data" / "raw"


def load_config() -> dict[str, Any]:
    path = ROOT / "ddg_config.json"
    if not path.exists():
        raise RuntimeError("Missing ddg_config.json. Copy ddg_config.example.json first.")
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_dirs() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)


def normalize_instagram_profile_url(url: str) -> str | None:
    if "instagram.com/" not in url:
        return None
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None
    first = parts[0].lower()
    if first in {"p", "reel", "explore", "stories", "accounts", "about"}:
        return None
    username = parts[0]
    return f"https://www.instagram.com/{username}/"


def extract_username(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    return parts[0].lower() if parts else None


def ddg_search_instagram_urls(
    query: str,
    max_results: int,
    timeout: int = 30,
    page_size: int = 30,
    page_sleep_seconds: float = 1.2,
    retries_per_page: int = 3,
    retry_backoff_seconds: float = 2.0,
) -> list[str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        )
    }
    urls: list[str] = []
    seen: set[str] = set()
    q = quote_plus(query)
    start = 0

    hosts = ("https://duckduckgo.com", "https://html.duckduckgo.com")

    while len(urls) < max_results:
        resp = None
        last_exc: Exception | None = None
        for attempt in range(1, retries_per_page + 1):
            for host in hosts:
                url = f"{host}/html/?q={q}&s={start}"
                try:
                    resp = requests.get(url, headers=headers, timeout=timeout)
                    resp.raise_for_status()
                    last_exc = None
                    break
                except requests.RequestException as exc:
                    last_exc = exc
            if resp is not None:
                break
            sleep_for = retry_backoff_seconds * attempt
            time.sleep(sleep_for)

        if resp is None:
            print(
                f"Warning: DDG page request failed for query '{query}' at offset {start} "
                f"after {retries_per_page} retries: {last_exc}"
            )
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        page_links = soup.select("a.result__a")
        if not page_links:
            break

        added_on_page = 0
        for a in page_links:
            href = a.get("href", "").strip()
            normalized = normalize_instagram_profile_url(href)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            urls.append(normalized)
            added_on_page += 1
            if len(urls) >= max_results:
                break

        if added_on_page == 0:
            break

        start += page_size
        time.sleep(page_sleep_seconds)

    return urls[:max_results]


def parse_profile_page(profile_url: str, timeout: int = 30) -> dict[str, Any]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(profile_url, headers=headers, timeout=timeout)
    if resp.status_code >= 400:
        return {
            "profile_url": profile_url,
            "username": extract_username(profile_url),
            "name": None,
            "followers": None,
            "bio": None,
            "status_code": resp.status_code,
            "error": "http_error",
        }

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text(strip=True) if soup.title else ""
    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_desc = meta_desc_tag["content"] if meta_desc_tag and meta_desc_tag.has_attr("content") else ""

    followers = None
    m = re.search(r"([\d.,]+[MK]?)\s+Followers", meta_desc, flags=re.IGNORECASE)
    if m:
        followers = m.group(1)

    name = None
    m2 = re.match(r"(.+?)\s+\(@", title)
    if m2:
        name = m2.group(1).strip()

    username = extract_username(profile_url)
    bio = None
    m3 = re.search(r"on Instagram:\s*\"(.*?)\"", meta_desc, flags=re.IGNORECASE | re.DOTALL)
    if m3:
        bio = m3.group(1).replace("\n", " ").strip()

    return {
        "profile_url": profile_url,
        "username": username,
        "name": name,
        "followers": followers,
        "bio": bio,
        "status_code": resp.status_code,
        "error": None,
    }


def run() -> None:
    cfg = load_config()
    ensure_dirs()

    queries: list[str] = cfg.get("queries", [])
    max_per_query = int(cfg.get("max_results_per_query", 25))
    ddg_page_size = int(cfg.get("ddg_page_size", 30))
    ddg_page_sleep = float(cfg.get("ddg_page_sleep_seconds", 1.2))
    ddg_timeout = int(cfg.get("ddg_timeout_seconds", 45))
    ddg_retries_per_page = int(cfg.get("ddg_retries_per_page", 3))
    ddg_retry_backoff = float(cfg.get("ddg_retry_backoff_seconds", 2.0))
    required_keywords = [str(x).lower() for x in cfg.get("required_profile_keywords_anywhere", [])]
    sleep_min = float(cfg.get("sleep_min_seconds", 1.0))
    sleep_max = float(cfg.get("sleep_max_seconds", 2.0))

    discovered: dict[str, dict[str, Any]] = {}
    search_logs: list[dict[str, Any]] = []

    for query in queries:
        try:
            urls = ddg_search_instagram_urls(
                query,
                max_results=max_per_query,
                timeout=ddg_timeout,
                page_size=ddg_page_size,
                page_sleep_seconds=ddg_page_sleep,
                retries_per_page=ddg_retries_per_page,
                retry_backoff_seconds=ddg_retry_backoff,
            )
            search_logs.append({"query": query, "results_count": len(urls), "urls": urls, "error": None})
            for u in urls:
                discovered.setdefault(
                    u, {"profile_url": u, "username": extract_username(u), "source_queries": []}
                )
                discovered[u]["source_queries"].append(query)
        except Exception as exc:
            search_logs.append({"query": query, "results_count": 0, "urls": [], "error": str(exc)})
            print(f"Warning: query failed and was skipped: {query} -> {exc}")
        time.sleep(random.uniform(sleep_min, sleep_max))

    (RAW_DIR / "ddg_search_results.json").write_text(
        json.dumps(search_logs, ensure_ascii=True, indent=2), encoding="utf-8"
    )

    enriched_rows: list[dict[str, Any]] = []
    for profile_url, row in discovered.items():
        profile_data = parse_profile_page(profile_url)
        profile_data["source_queries"] = " | ".join(sorted(set(row["source_queries"])))
        enriched_rows.append(profile_data)
        time.sleep(random.uniform(sleep_min, sleep_max))

    if required_keywords:
        filtered_rows: list[dict[str, Any]] = []
        for row in enriched_rows:
            haystack = " ".join(
                [
                    str(row.get("username") or ""),
                    str(row.get("name") or ""),
                    str(row.get("bio") or ""),
                    str(row.get("source_queries") or ""),
                ]
            ).lower()
            if all(keyword in haystack for keyword in required_keywords):
                filtered_rows.append(row)
    else:
        filtered_rows = enriched_rows

    out_csv = OUT_DIR / "ddg_instagram_accounts.csv"
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "profile_url",
                "username",
                "name",
                "followers",
                "bio",
                "source_queries",
                "status_code",
                "error",
            ],
        )
        writer.writeheader()
        writer.writerows(filtered_rows)

    print(f"Queries run: {len(queries)}")
    print(f"Profiles found: {len(discovered)}")
    print(f"Profiles after required keyword filter: {len(filtered_rows)}")
    print(f"Wrote: {out_csv}")


if __name__ == "__main__":
    run()
