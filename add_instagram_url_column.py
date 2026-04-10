from __future__ import annotations

from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent
INPUT_CSV = ROOT / "instagram handles - Sheet1.csv"
OUTPUT_CSV = ROOT / "instagram handles - Sheet1.with_instagram_url.csv"


def build_profile_url(username: str) -> str:
    u = str(username).strip().lstrip("@")
    if not u:
        return ""
    return f"https://www.instagram.com/{u}/"


def run() -> None:
    if not INPUT_CSV.exists():
        raise RuntimeError(f"Input file not found: {INPUT_CSV}")

    df = pd.read_csv(INPUT_CSV)
    if df.empty:
        df.to_csv(OUTPUT_CSV, index=False)
        print(f"Input empty. Wrote: {OUTPUT_CSV}")
        return

    first_col = df.columns[0]
    urls = df[first_col].map(build_profile_url)

    out = df.copy()
    out.insert(1, "Instagram Profile URL", urls)
    out.to_csv(OUTPUT_CSV, index=False)

    print(f"Wrote file: {OUTPUT_CSV}")
    print(f"Rows: {len(out)}")


if __name__ == "__main__":
    run()
