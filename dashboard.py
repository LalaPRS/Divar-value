#!/usr/bin/env python3
"""
dashboard.py
------------
Renders dashboard.html (the local HTML UI) from the current config and the
listings the bot has collected (state/listings.json).

divar_watch.py calls generate() automatically at the end of every run, so
dashboard.html is always up to date and gets committed back to the repo
alongside state/*.json by the GitHub Actions workflow.

Can also be run directly for local testing:
    python dashboard.py            # regenerate from whatever is on disk
    python dashboard.py --sample   # fill with made-up sample listings,
                                    # useful to preview the UI before the
                                    # bot has ever run successfully
"""

from __future__ import annotations

import argparse
import json
import pathlib
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent
TEMPLATE_PATH = ROOT / "template" / "dashboard_template.html"
OUTPUT_PATH = ROOT / "dashboard.html"
LISTINGS_STATE_PATH = ROOT / "state" / "listings.json"

TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))

SAMPLE_LISTINGS = [
    {
        "token": "sample-a1",
        "title": "آپارتمان ۸۰ متری در ونک",
        "price": 5500000000,
        "district": "ونک",
        "area": 80,
        "price_per_m2": 68750000,
        "floor": 3,
        "total_floors": 5,
        "discount_pct": 18,
        "deal_score": 77,
        "reasons": ["قیمت هر متر ۱۸٪ پایین‌تر از میانه‌ی محله"],
        "parking": True,
        "storage": False,
        "elevator": True,
        "single_sheet_deed": True,
        "url": "https://divar.ir/v/x/sample-a1",
        "first_seen": "2026-11-16T08:00:00+03:30",
    },
    {
        "token": "sample-a2",
        "title": "آپارتمان نوساز شهرزیبا",
        "price": 4800000000,
        "district": "شهرزیبا",
        "area": 65,
        "price_per_m2": 73846153,
        "floor": None,
        "total_floors": None,
        "discount_pct": None,
        "deal_score": 55,
        "reasons": [],
        "parking": None,
        "storage": None,
        "elevator": None,
        "single_sheet_deed": None,
        "url": "https://divar.ir/v/x/sample-a2",
        "first_seen": "2026-11-15T12:00:00+03:30",
    },
]


def _json_for_script(data) -> str:
    """json.dumps, but safe to inline inside a <script> tag (escapes any
    literal '</' sequence so a title like '...</script>' can't break out
    of the tag)."""
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def load_listings_state() -> dict:
    if LISTINGS_STATE_PATH.exists():
        with open(LISTINGS_STATE_PATH, encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def generate(cfg: dict, listings_state: dict | None = None, *, sample: bool = False) -> pathlib.Path:
    """Render dashboard.html. Pass sample=True to fill it with made-up
    listings (used before the bot has ever run, or for local preview)."""
    if sample:
        listings = SAMPLE_LISTINGS
        is_sample = True
    else:
        listings_state = listings_state if listings_state is not None else load_listings_state()
        listings = list(listings_state.values())
        is_sample = len(listings) == 0

    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    updated_at = datetime.now(TEHRAN_TZ).strftime("%Y-%m-%d %H:%M") + " به‌وقت تهران"

    html = html.replace("__LISTINGS_JSON__", _json_for_script(listings))
    html = html.replace("__CONFIG_JSON__", _json_for_script(cfg))
    html = html.replace("__IS_SAMPLE__", "true" if is_sample else "false")
    html = html.replace("__UPDATED_AT__", f"آخرین به‌روزرسانی: {updated_at} · {len(listings)} آگهی")

    OUTPUT_PATH.write_text(html, encoding="utf-8")
    return OUTPUT_PATH


if __name__ == "__main__":
    import yaml

    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", action="store_true", help="fill with sample data instead of state/listings.json")
    args = parser.parse_args()

    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    out = generate(config, sample=args.sample)
    print(f"نوشته شد: {out}")
