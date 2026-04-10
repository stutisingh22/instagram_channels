#!/usr/bin/env python3
"""
Bulk discovery via Instagram following-graph BFS (seeds → who they follow → …).

- Appends one CSV row per discovered account as soon as it is seen (no end-of-run dump).
- Appends one edge row per (source → followee) for audit/resume.
- Checkpoints after each expanded source so kills/resumes are safe.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, sync_playwright

from instagram_following_fetch import RESERVED, extract_following_handles
from instagram_search_browser_scraper import (
    ROOT,
    ensure_not_blocked,
    get_state_dir,
    normalize_followers_value,
    normalize_username,
    wait_for_manual_login,
)

RAW_DIR = ROOT / "data" / "raw"
OUT_DIR = ROOT / "data" / "out"

CANDIDATE_FIELDS = [
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
EDGE_FIELDS = ["source_username", "following_username", "source_depth", "vertical"]


def load_cfg(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"Missing config: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "processed_expansion": [],
            "queue": [],
            "appended_usernames": [],
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "processed_expansion": [],
            "queue": [],
            "appended_usernames": [],
        }


def save_checkpoint(
    path: Path,
    *,
    processed_expansion: set[str],
    queue: deque[tuple[str, int]],
    appended: set[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "processed_expansion": sorted(processed_expansion),
        "queue": [[u, d] for u, d in queue],
        "appended_usernames": sorted(appended),
        "updated_at_epoch": int(time.time()),
    }
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def usernames_from_candidate_csv(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            u = normalize_username(str(row.get("username", "")))
            if u:
                out.add(u)
    return out


def _open_append_csv(path: Path, fieldnames: list[str]) -> tuple[Any, csv.DictWriter]:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = (not path.exists()) or path.stat().st_size == 0
    f = path.open("a", newline="", encoding="utf-8")
    w = csv.DictWriter(f, fieldnames=fieldnames)
    if new_file:
        w.writeheader()
        f.flush()
        os.fsync(f.fileno())
    return f, w


def append_candidate_row(
    file_handle: Any,
    writer: csv.DictWriter,
    *,
    username: str,
    source_tag: str,
    via_source: str,
    depth: int,
    enrich: dict[str, Any] | None,
    vertical: str,
) -> None:
    u = normalize_username(username)
    base = {
        "profile_url": f"https://www.instagram.com/{u}/",
        "username": u,
        "name": "",
        "followers": None,
        "followers_raw": "",
        "bio": "",
        "is_private": "",
        "is_verified": "",
        "source_keywords": f"following_graph|{vertical}|via:{via_source}|d{depth}|{source_tag}",
        "error": "",
    }
    if enrich:
        base.update(enrich)
    writer.writerow(base)
    file_handle.flush()
    os.fsync(file_handle.fileno())


def append_edge_row(
    file_handle: Any,
    writer: csv.DictWriter,
    *,
    source: str,
    following: str,
    depth: int,
    vertical: str,
) -> None:
    writer.writerow(
        {
            "source_username": normalize_username(source),
            "following_username": normalize_username(following),
            "source_depth": depth,
            "vertical": vertical,
        }
    )
    file_handle.flush()
    os.fsync(file_handle.fileno())


def enrich_username_topsearch(page: Any, username: str) -> dict[str, Any]:
    """Single-account topsearch enrich; best-effort."""
    try:
        q = username.replace(" ", "%20")
        url = f"https://www.instagram.com/web/search/topsearch/?query={q}&count=5"
        resp = page.request.get(url, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=45000)
        if resp.status != 200:
            return {}
        payload = resp.json()
        users = payload.get("users", []) if isinstance(payload, dict) else []
        for item in users if isinstance(users, list) else []:
            user = item.get("user", {}) if isinstance(item, dict) else {}
            u = normalize_username(str(user.get("username", "")))
            if u == normalize_username(username):
                return {
                    "name": user.get("full_name") or "",
                    "followers": normalize_followers_value(user.get("follower_count")),
                    "followers_raw": str(user.get("follower_count") or ""),
                    "bio": user.get("biography") or "",
                    "is_private": user.get("is_private"),
                    "is_verified": user.get("is_verified"),
                    "error": "",
                }
    except Exception:
        pass
    return {}


def run(config_path: Path, *, reset_checkpoint: bool) -> None:
    cfg = load_cfg(config_path)
    vertical = str(cfg.get("vertical", cfg.get("output_prefix", "graph"))).strip() or "graph"
    out_prefix = str(cfg.get("output_prefix", vertical)).strip() or "graph"

    candidate_csv = (ROOT / str(cfg.get("candidate_output_csv", f"data/out/{out_prefix}_graph_candidates_append.csv"))).resolve()
    edges_csv = (ROOT / str(cfg.get("edges_output_csv", f"data/out/{out_prefix}_graph_edges_append.csv"))).resolve()
    checkpoint_path = (ROOT / str(cfg.get("checkpoint_relative_path", f"data/raw/{out_prefix}_graph_checkpoint.json"))).resolve()

    seed_accounts = [
        normalize_username(str(x))
        for x in cfg.get("seed_accounts", [])
        if str(x).strip()
    ]
    seed_accounts = [u for u in seed_accounts if u and u not in RESERVED]

    max_following_per_source = int(cfg.get("max_following_per_source", 400))
    max_graph_depth = int(cfg.get("max_graph_depth", 2))
    max_expand_nodes = int(cfg.get("max_expand_nodes", 500))
    inter_source_sleep = float(cfg.get("inter_source_sleep_seconds", 1.2))
    enrich_each = bool(cfg.get("enrich_via_topsearch", False))

    state_dir_cfg = str(cfg.get("playwright_state_dir", ".ig_browser_state")).strip()
    state_dir = (ROOT / state_dir_cfg).resolve() if not Path(state_dir_cfg).is_absolute() else Path(state_dir_cfg)
    headless = bool(cfg.get("headless", False))

    if reset_checkpoint and checkpoint_path.exists():
        checkpoint_path.unlink()
        print(f"Reset checkpoint removed: {checkpoint_path}", flush=True)

    cp = load_checkpoint(checkpoint_path)
    processed_expansion: set[str] = set(cp.get("processed_expansion", []))
    appended: set[str] = set(cp.get("appended_usernames", []))
    appended |= usernames_from_candidate_csv(candidate_csv)

    queue: deque[tuple[str, int]] = deque()
    raw_q = cp.get("queue", [])
    if raw_q:
        for pair in raw_q:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                u, d = normalize_username(str(pair[0])), int(pair[1])
                if u:
                    queue.append((u, d))
    else:
        for s in seed_accounts:
            queue.append((s, 0))

    if not queue and seed_accounts:
        for s in seed_accounts:
            queue.append((s, 0))

    cand_f, cand_w = _open_append_csv(candidate_csv, CANDIDATE_FIELDS)
    edge_f, edge_w = _open_append_csv(edges_csv, EDGE_FIELDS)

    expanded_count = 0
    print(
        f"Following-graph expander ({vertical}): seeds={len(seed_accounts)}, "
        f"queue={len(queue)}, appended={len(appended)}, max_depth={max_graph_depth}, "
        f"max_expand={max_expand_nodes}, enrich={enrich_each}",
        flush=True,
    )
    print(f"candidates -> {candidate_csv}", flush=True)
    print(f"edges -> {edges_csv}", flush=True)

    try:
        with sync_playwright() as p:
            context: BrowserContext = p.chromium.launch_persistent_context(
                user_data_dir=str(state_dir if state_dir else get_state_dir()),
                headless=headless,
                viewport={"width": 1440, "height": 920},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.new_page()
            wait_for_manual_login(page)
            ensure_not_blocked(page, "following graph init")

            while queue and expanded_count < max_expand_nodes:
                user, depth = queue.popleft()
                if user in processed_expansion:
                    continue
                if user in RESERVED:
                    processed_expansion.add(user)
                    save_checkpoint(checkpoint_path, processed_expansion=processed_expansion, queue=queue, appended=appended)
                    continue

                try:
                    following = extract_following_handles(page, user, max_following_per_source)
                except Exception as exc:
                    print(f"@{user} following fetch failed: {exc}", flush=True)
                    following = []

                for f in following:
                    fu = normalize_username(f)
                    if not fu or fu in RESERVED:
                        continue
                    append_edge_row(edge_f, edge_w, source=user, following=fu, depth=depth, vertical=vertical)
                    if fu not in appended:
                        enrich = enrich_username_topsearch(page, fu) if enrich_each else None
                        append_candidate_row(
                            cand_f,
                            cand_w,
                            username=fu,
                            source_tag=user,
                            via_source=user,
                            depth=depth + 1,
                            enrich=enrich,
                            vertical=vertical,
                        )
                        appended.add(fu)
                    next_depth = depth + 1
                    if next_depth < max_graph_depth and fu not in processed_expansion:
                        queue.append((fu, next_depth))

                processed_expansion.add(user)
                expanded_count += 1
                save_checkpoint(checkpoint_path, processed_expansion=processed_expansion, queue=queue, appended=appended)
                print(
                    f"expanded={expanded_count}/{max_expand_nodes} @{user} d={depth} "
                    f"following={len(following)} queue={len(queue)} total_appended={len(appended)}",
                    flush=True,
                )
                time.sleep(inter_source_sleep)

            context.close()
    finally:
        cand_f.close()
        edge_f.close()

    print("Following-graph expander done.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Following-graph bulk discovery (append + checkpoint)")
    parser.add_argument("--config", required=True, help="Path to JSON config")
    parser.add_argument("--reset-checkpoint", action="store_true")
    args = parser.parse_args()
    pth = Path(args.config).expanduser()
    if not pth.is_absolute():
        pth = (ROOT / pth).resolve()
    run(pth, reset_checkpoint=args.reset_checkpoint)
