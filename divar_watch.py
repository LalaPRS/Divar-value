#!/usr/bin/env python3
"""
divar_watch.py
---------------
Watches Divar (divar.ir) for Tehran apartments for sale in a price range,
scores how good a deal each one is (price per m2 vs. comparable listings),
and sends the new worthwhile ones to a Telegram chat.

Built on top of the open-source `divar-mcp` package (MIT license) by
Sepehr Radmard (github.com/sepehr071/divar-mcp), which talks to the same
public JSON endpoints the divar.ir website itself uses, with a
Chrome-fingerprinted client, rate limiting and retries already handled.
This script drives it directly over the MCP stdio protocol instead of
going through an AI agent, so it can run unattended on a schedule
(see .github/workflows/watch.yml).

State (which ads were already alerted on) is kept in state/seen_tokens.json,
which the GitHub Actions workflow commits back to the repo after each run.

Run manually for testing:
    pip install -r requirements.txt
    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... VERBOSE_SUMMARY=true python divar_watch.py
"""

from __future__ import annotations

import html
import asyncio
import json
import os
import pathlib
import re
import time
from datetime import datetime

import requests
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "state" / "seen_tokens.json"
LISTINGS_STATE_PATH = ROOT / "state" / "listings.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
VERBOSE_SUMMARY = os.environ.get("VERBOSE_SUMMARY", "false").strip().lower() == "true"

DEFAULT_CATEGORY_FALLBACK = "apartment-sell"
STATE_MAX_AGE_DAYS = 45       # یادآوری ارسال تلگرام را بعد از این مدت پاک کن
LISTINGS_MAX_AGE_DAYS = 14    # آگهی که این‌قدر دیده نشده از داشبورد حذف می‌شود (احتمالاً فروخته/حذف شده)

PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_DIGIT_TABLE = {d: str(i) for i, d in enumerate(PERSIAN_DIGITS)}
_DIGIT_TABLE.update({d: str(i) for i, d in enumerate(ARABIC_DIGITS)})


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def normalize_digits(s: str) -> str:
    if not isinstance(s, str):
        return s
    return "".join(_DIGIT_TABLE.get(ch, ch) for ch in s)


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - STATE_MAX_AGE_DAYS * 86400
    pruned = {k: v for k, v in state.items() if v.get("ts", 0) >= cutoff}
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(pruned, f, ensure_ascii=False, indent=2)


def load_listings_state() -> dict:
    if LISTINGS_STATE_PATH.exists():
        with open(LISTINGS_STATE_PATH, encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_listings_state(listings_state: dict) -> None:
    LISTINGS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - LISTINGS_MAX_AGE_DAYS * 86400
    pruned = {k: v for k, v in listings_state.items() if v.get("last_seen", 0) >= cutoff}
    with open(LISTINGS_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(pruned, f, ensure_ascii=False, indent=2)


async def call_tool_json(session: ClientSession, name: str, arguments: dict):
    arguments = {k: v for k, v in arguments.items() if v is not None}
    result = await session.call_tool(name, arguments=arguments)
    if getattr(result, "isError", False):
        text = result.content[0].text if result.content else "unknown error"
        raise RuntimeError(f"{name} -> error: {text}")
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
    return {}


def flatten_nodes(obj, acc=None):
    """Walk an arbitrary JSON structure and collect every dict that looks
    like a named/keyed node (category, filter, etc.), regardless of how
    it's nested. Keeps the script working even if divar-mcp changes its
    exact response wrapping."""
    if acc is None:
        acc = []
    if isinstance(obj, dict):
        if obj.keys() & {"slug", "name", "title", "label", "key"}:
            acc.append(obj)
        for v in obj.values():
            flatten_nodes(v, acc)
    elif isinstance(obj, list):
        for item in obj:
            flatten_nodes(item, acc)
    return acc


def extract_ads(obj) -> dict:
    """Find every list-of-ad-card inside an arbitrary tool result and merge
    them into {token: ad_card}, regardless of what the wrapping keys are
    called (deals / best_value / ads / results / ...)."""
    found_lists = []

    def walk(o):
        if isinstance(o, list):
            if o and all(isinstance(i, dict) and "token" in i for i in o):
                found_lists.append(o)
            else:
                for i in o:
                    walk(i)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)

    walk(obj)
    merged = {}
    for lst in found_lists:
        for ad in lst:
            merged[ad["token"]] = ad
    return merged


async def resolve_category(session: ClientSession, cfg: dict) -> str:
    override = cfg.get("category_override")
    if override:
        log(f"دسته‌ی دستی از config.yaml استفاده می‌شود: {override}")
        return override

    try:
        raw = await call_tool_json(session, "dv_categories", {"query": "آپارتمان"})
    except Exception as e:  # noqa: BLE001
        log(f"dv_categories خطا داد ({e!r})؛ استفاده از دسته‌ی پیش‌فرض {DEFAULT_CATEGORY_FALLBACK!r}")
        return DEFAULT_CATEGORY_FALLBACK

    nodes = flatten_nodes(raw)
    best = None
    for n in nodes:
        text = " ".join(str(v) for v in n.values())
        if "آپارتمان" in text and ("فروش" in text or "خرید" in text) and "اجاره" not in text:
            best = n
            break

    if best:
        slug = best.get("slug") or best.get("key") or best.get("id") or best.get("value")
        if slug:
            log(f"دسته‌ی «خرید آپارتمان» شناسایی شد: {best}")
            return slug

    log(
        "تشخیص خودکار دسته ناموفق بود؛ از دسته‌ی پیش‌فرض استفاده می‌شود. "
        "خروجی خام dv_categories را زیر ببین و در صورت نیاز category_override را در config.yaml ست کن:"
    )
    log(json.dumps(raw, ensure_ascii=False)[:3000])
    return DEFAULT_CATEGORY_FALLBACK


def fmt_toman(n) -> str:
    try:
        return f"{int(round(float(n))):,}"
    except (TypeError, ValueError):
        return str(n)


def extract_feature_text(obj) -> str:
    """Flatten every string value in an arbitrary ad/post object into one
    blob, so we can keyword-search it for features (parking, storage,
    elevator, deed type, area) without depending on exact field names."""
    chunks = []

    def walk(o):
        if isinstance(o, str):
            chunks.append(o)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return normalize_digits(" | ".join(chunks))


def has_any(blob: str, *keywords: str) -> bool:
    return any(kw in blob for kw in keywords)


def feature_state(blob: str, *keywords: str):
    """True/False/None for whether a feature is present. Checks for a
    negation marker ("ندارد"، "بدون"، "خیر") within the SAME chunk as the
    keyword (chunks are the " | "-joined original string values from
    extract_feature_text) rather than a fixed character window, so a
    negation on one spec line ("انباری ندارد") can't bleed into the next
    line's feature ("آسانسور دارد"). Iranian listing specs commonly read
    "X: ندارد" for a feature the unit LACKS, so a bare substring match
    would get this backwards. Returns None if the keyword isn't mentioned
    at all."""
    for chunk in blob.split(" | "):
        for kw in keywords:
            if kw in chunk:
                if "ندارد" in chunk or "بدون" in chunk or "خیر" in chunk:
                    return False
                return True
    return None


def extract_area_m2(blob: str):
    m = re.search(r"(\d{2,4})\s*متر", blob)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def extract_floor_info(blob: str):
    floor = None
    total = None
    m = re.search(r"طبقه\s*(\d{1,2})", blob)
    if m:
        try:
            floor = int(m.group(1))
        except ValueError:
            floor = None
    m2 = re.search(r"از\s*(\d{1,2})\s*طبقه", blob)
    if m2:
        try:
            total = int(m2.group(1))
        except ValueError:
            total = None
    return floor, total


FEATURE_CHECKS = {
    "parking": ("پارکینگ",),
    "storage": ("انباری",),
    "elevator": ("آسانسور",),
    "single_sheet_deed": ("تک برگ", "تک‌برگ", "تک برگی"),
}


def build_listing_record(ad: dict, post_detail) -> dict:
    """Normalize one ad (plus optional full dv_post detail) into the flat
    record shape shared by the Telegram message, the feature filter, and
    the dashboard.html listings array. Keeping this in one place means all
    three stay consistent as divar-mcp's exact response shape evolves."""
    blob_ad = extract_feature_text(ad)
    blob_post = extract_feature_text(post_detail) if post_detail else ""
    blob = blob_post or blob_ad

    area = extract_area_m2(blob_post) or extract_area_m2(blob_ad)
    price = ad.get("price")
    price_per_m2 = None
    if isinstance(price, (int, float)) and area:
        price_per_m2 = price / area

    floor, total_floors = extract_floor_info(blob)

    return {
        "token": ad.get("token"),
        "title": ad.get("title") or "بدون عنوان",
        "price": price,
        "district": ad.get("district"),
        "area": area,
        "price_per_m2": price_per_m2,
        "floor": floor,
        "total_floors": total_floors,
        "deal_score": ad.get("deal_score"),
        "discount_pct": ad.get("discount_pct"),
        "reasons": ad.get("reasons") or [],
        "parking": feature_state(blob, *FEATURE_CHECKS["parking"]),
        "storage": feature_state(blob, *FEATURE_CHECKS["storage"]),
        "elevator": feature_state(blob, *FEATURE_CHECKS["elevator"]),
        "single_sheet_deed": feature_state(blob, *FEATURE_CHECKS["single_sheet_deed"]),
        "url": ad.get("url"),
    }


def merge_fresh_fields(cached_record: dict, fresh_ad: dict) -> dict:
    """Refresh the volatile fields (price, title, deal score, ...) of a
    previously-cached record from this run's ad snapshot, without
    re-fetching dv_post detail (area/floor/features rarely change and
    fetching them again for every candidate on every run would multiply
    the number of requests to Divar for no benefit)."""
    record = dict(cached_record)
    fresh = build_listing_record(fresh_ad, None)
    for key in ("title", "price", "district", "deal_score", "discount_pct", "reasons", "url"):
        if fresh.get(key) not in (None, "", []):
            record[key] = fresh[key]
    if record.get("area") and isinstance(record.get("price"), (int, float)):
        record["price_per_m2"] = record["price"] / record["area"]
    return record


def passes_required_features(record: dict, cfg: dict) -> bool:
    req = cfg.get("require") or {}
    for key in FEATURE_CHECKS:
        want = req.get(key)
        if want is None:
            continue
        state = record.get(key)
        if state is None:
            # ویژگی در متن پیدا نشد؛ برای جلوگیری از حذف اشتباه، سخت‌گیری نمی‌کنیم
            continue
        if want is True and state is not True:
            return False
        if want is False and state is not False:
            return False
    return True

def esc(value) -> str:
    """HTML-escape دادن متن‌های داینامیک قبل از قرار گرفتن در پیام HTML تلگرام.
    عنوان/دلیل آگهی‌های دیوار گاهی یک '<' خام دارند که پارسر HTML تلگرام
    آن را شروع یک تگ ناقص می‌خواند و کل پیام را رد می‌کند. هرچه از خودِ
    آگهی می‌آید باید escape شود؛ فقط تگ‌های <b>/</b> که خودمان اضافه
    می‌کنیم دست‌نخورده می‌مانند."""
    return html.escape(str(value), quote=False)
    
def build_message(record: dict) -> str:
    lines = [f"🏠 <b>{esc(record.get('title') or 'بدون عنوان')}</b>"]
    if record.get("district"):
        lines.append(f"📍 محله: {esc(record['district'])}")
    if isinstance(record.get("price"), (int, float)):
        lines.append(f"💰 قیمت: {fmt_toman(record['price'])} تومان")
    if record.get("area"):
        lines.append(f"📐 متراژ: {record['area']} متر")
    if record.get("price_per_m2"):
        lines.append(f"📊 قیمت هر متر: {fmt_toman(record['price_per_m2'])} تومان")

    if record.get("floor") is not None:
        tf = record.get("total_floors")
        lines.append(f"🏢 طبقه: {record['floor']}" + (f" (از {tf} طبقه)" if tf else ""))

    if record.get("deal_score") is not None:
        lines.append(f"⭐ امتیاز ارزشمندی: {record['deal_score']}")
    if record.get("discount_pct") is not None:
        lines.append(f"🔻 ارزان‌تر از میانه: {record['discount_pct']}%")
    if record.get("reasons"):
        lines.append("دلیل: " + "، ".join(esc(r) for r in record["reasons"]))

    feat_labels = {
        "parking": "🚗 پارکینگ",
        "storage": "📦 انباری",
        "elevator": "🛗 آسانسور",
        "single_sheet_deed": "📄 سند تک‌برگ",
    }
    feats = []
    for key, label in feat_labels.items():
        state = record.get(key)
        if state is True:
            feats.append(f"{label}: دارد ✅")
        elif state is False:
            feats.append(f"{label}: ندارد ❌")
    if feats:
        lines.append(" | ".join(feats))

    if record.get("url"):
        lines.append(f"🔗 {record['url']}")

    return "\n".join(lines)


def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("توکن یا chat_id تلگرام تنظیم نشده؛ پیام فقط در لاگ چاپ می‌شود:\n" + text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "false",
            },
            timeout=20,
        )
        if r.status_code != 200:
            log(f"ارسال تلگرام ناموفق بود: {r.status_code} {r.text[:300]}")
    except requests.RequestException as e:  # noqa: BLE001
        log(f"خطای شبکه هنگام ارسال به تلگرام: {e!r}")


async def main() -> None:
    cfg = load_config()
    state = load_state()

    price_min = cfg["price"]["min_toman"]
    price_max = cfg["price"]["max_toman"]
    min_area = cfg.get("min_area_m2", 50)
    districts = cfg.get("districts") or None
    min_discount_pct = cfg.get("min_discount_pct", 12)
    pages = cfg.get("pages_per_run", 6)
    max_age_hours = cfg.get("max_age_hours")

    server_params = StdioServerParameters(command="divar-mcp", args=[])

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            category = await resolve_category(session, cfg)

            filters = {"size": {"min": min_area}}
            filters.update(cfg.get("filters_override") or {})

            base_args = dict(
                city=cfg.get("city", "تهران"),
                category=category,
                districts=districts,
                min_price=price_min,
                max_price=price_max,
                filters=filters,
                pages=pages,
                max_age_hours=max_age_hours,
            )
            log(f"پارامترهای جستجو: {base_args}")

            candidates: dict = {}

            try:
                deals_raw = await call_tool_json(
                    session, "dv_find_deals", {**base_args, "min_discount_pct": min_discount_pct}
                )
                candidates.update(extract_ads(deals_raw))
            except Exception as e:  # noqa: BLE001
                log(f"dv_find_deals خطا داد: {e!r}")

            try:
                budget_raw = await call_tool_json(session, "dv_best_for_budget", {**base_args, "budget": price_max})
                for tok, ad in extract_ads(budget_raw).items():
                    candidates.setdefault(tok, ad)
            except Exception as e:  # noqa: BLE001
                log(f"dv_best_for_budget خطا داد: {e!r}")

            log(f"{len(candidates)} آگهی ارزشمند کاندید (قبل از فیلتر ویژگی‌ها و حذف تکراری‌ها)")

            listings_state = load_listings_state()
            now_ts = time.time()
            new_sent = 0

            for tok, ad in candidates.items():
                cached = listings_state.get(tok)

                # جزئیات کامل آگهی (dv_post) را فقط یک‌بار برای هر آگهی می‌گیریم؛
                # اگر قبلاً گرفته شده (متراژ/طبقه/ویژگی‌ها معمولاً عوض نمی‌شوند)،
                # فقط فیلدهای ناپایدار (قیمت، عنوان، امتیاز) را از این اجرا به‌روزرسانی می‌کنیم.
                if cached and cached.get("area") is not None:
                    record = merge_fresh_fields(cached, ad)
                else:
                    post_detail = None
                    try:
                        post_detail = await call_tool_json(session, "dv_post", {"token": tok})
                    except Exception as e:  # noqa: BLE001
                        log(f"dv_post برای {tok} خطا داد: {e!r}")
                    record = build_listing_record(ad, post_detail)
                    await asyncio.sleep(1.5)  # رعایت نرخ درخواست به دیوار

                record["token"] = tok
                record["first_seen"] = cached.get("first_seen", now_ts) if cached else now_ts
                record["last_seen"] = now_ts

                if record.get("area") and record["area"] < min_area:
                    continue
                if not passes_required_features(record, cfg):
                    continue

                listings_state[tok] = record

                if tok not in state:
                    send_telegram(build_message(record))
                    state[tok] = {"ts": now_ts, "title": record.get("title")}
                    new_sent += 1

            if new_sent == 0 and VERBOSE_SUMMARY:
                send_telegram(
                    f"✅ اجرای تستی انجام شد.\nدسته: {category}\n"
                    f"{len(candidates)} کاندید بررسی شد، آگهی جدید ارزشمندی (که قبلاً دیده نشده) پیدا نشد."
                )

            log(f"{new_sent} پیام جدید به تلگرام ارسال شد. {len(listings_state)} آگهی در داشبورد.")
            save_state(state)
            save_listings_state(listings_state)

            try:
                import dashboard

                dashboard.generate(cfg, listings_state)
                log(f"دشبورد به‌روزرسانی شد: {dashboard.OUTPUT_PATH}")
            except Exception as e:  # noqa: BLE001
                log(f"ساخت dashboard.html ناموفق بود: {e!r}")


if __name__ == "__main__":
    asyncio.run(main())
