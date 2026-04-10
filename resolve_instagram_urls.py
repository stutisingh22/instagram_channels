from __future__ import annotations

import csv
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "out"
RAW_DIR = ROOT / "data" / "raw"
CONFIG_PATH = ROOT / "resolve_instagram_urls_config.json"
CHECKPOINT_PATH = RAW_DIR / "resolve_instagram_urls_checkpoint.json"


def ensure_dirs() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Missing config file: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_checkpoint() -> dict[str, Any]:
    if not CHECKPOINT_PATH.exists():
        return {"processed_keys": [], "rows": []}
    try:
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"processed_keys": [], "rows": []}


def save_checkpoint(processed_keys: set[str], rows: list[dict[str, Any]]) -> None:
    payload = {"processed_keys": sorted(processed_keys), "rows": rows}
    CHECKPOINT_PATH.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def write_live_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "input_name",
                "input_handle",
                "matched_username",
                "instagram_url",
                "method",
                "confidence",
                "error",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def normalize_text(v: str) -> str:
    return re.sub(r"\s+", " ", v.strip().lower())


def load_inputs(
    path: Path,
    name_col: str,
    handle_col: str,
) -> list[dict[str, str]]:
    if not path.exists():
        raise RuntimeError(f"Input file not found: {path}")

    if path.suffix.lower() in {".xlsx", ".xls"}:
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)

    out: list[dict[str, str]] = []
    for _, row in df.iterrows():
        name = str(row.get(name_col, "")).strip()
        handle = str(row.get(handle_col, "")).strip()
        if not name and not handle:
            continue
        out.append({"name": name, "handle": handle})
    return out


def search_ddg_for_instagram(input_name: str, input_handle: str, timeout_s: int) -> tuple[str | None, str, str]:
    query_parts = []
    if input_handle:
        query_parts.append(f'"{input_handle}"')
    if input_name:
        query_parts.append(f'"{input_name}"')
    query_parts.append("site:instagram.com")
    query = " ".join(query_parts).strip()

    url = f"https://duckduckgo.com/html/?q={quote(query)}"
    try:
        resp = requests.get(
            url,
            timeout=timeout_s,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
    except Exception as exc:
        return None, "ddg", f"ddg_request_failed: {exc}"

    # Pull first profile-like URL.
    matches = re.findall(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/?", resp.text)
    reserved = {"p", "reel", "explore", "accounts", "direct", "stories"}
    for m in matches:
        u = m.strip().lower()
        if not u or u in reserved:
            continue
        return f"https://www.instagram.com/{u}/", "ddg", ""
    return None, "ddg", "no_match"


def run() -> None:
    ensure_dirs()
    cfg = load_config()

    input_path = Path(str(cfg.get("input_file", "")).strip())
    if not input_path.is_absolute():
        input_path = (ROOT / input_path).resolve()
    name_col = str(cfg.get("name_column", "name"))
    handle_col = str(cfg.get("handle_column", "username"))
    timeout_s = int(cfg.get("ddg_timeout_seconds", 25))
    sleep_s = float(cfg.get("sleep_seconds", 0.8))

    out_csv = ROOT / str(cfg.get("output_file", "data/out/resolved_instagram_urls.csv"))
    if not out_csv.is_absolute():
        out_csv = (ROOT / out_csv).resolve()

    inputs = load_inputs(input_path, name_col=name_col, handle_col=handle_col)
    checkpoint = load_checkpoint()
    processed_keys = set(checkpoint.get("processed_keys", []))
    rows: list[dict[str, Any]] = checkpoint.get("rows", [])

    print(f"Loaded inputs={len(inputs)} from {input_path}", flush=True)
    print(f"Resuming with processed={len(processed_keys)}", flush=True)

    for idx, item in enumerate(inputs, start=1):
        input_name = item["name"]
        input_handle = item["handle"]
        key = normalize_text(f"{input_name}|{input_handle}")
        if key in processed_keys:
            continue

        url, method, err = search_ddg_for_instagram(input_name, input_handle, timeout_s=timeout_s)
        matched_username = ""
        confidence = "low"
        if url:
            m = re.search(r"instagram\.com/([A-Za-z0-9._]+)/", url)
            matched_username = m.group(1).lower() if m else ""
            if input_handle and matched_username == normalize_text(input_handle).lstrip("@"):
                confidence = "high"
            elif input_name and matched_username and any(tok in matched_username for tok in normalize_text(input_name).split()):
                confidence = "medium"

        rows.append(
            {
                "input_name": input_name,
                "input_handle": input_handle,
                "matched_username": matched_username,
                "instagram_url": url or "",
                "method": method,
                "confidence": confidence if url else "",
                "error": err,
            }
        )
        processed_keys.add(key)
        save_checkpoint(processed_keys, rows)
        write_live_csv(out_csv, rows)
        print(f"[{idx}/{len(inputs)}] resolved: {input_name or input_handle} -> {url or 'NO_MATCH'}", flush=True)
        time.sleep(sleep_s)

    print(f"Done. Output: {out_csv}", flush=True)


if __name__ == "__main__":
    run()
