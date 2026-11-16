"""
Merge all pillar outputs into the single frontend contract:
  site/data/feed.json    (live feed, newest first, capped)
  site/data/archive.json (growing history, id-keyed)

Inputs (all optional - missing pillar files are skipped):
  data/trending_items.json  (GitHub exploding repos)
  data/hn_items.json        (Hacker News early signal)
  data/status_items.json    (provider outages / recoveries)
  data/prices_items.json    (API price changes)
  data/feeds_items.json     (releases + company news)

Item contract: {id, timestamp, category, source_name, source_url,
target_url, headline_en, summary_en, ...extras}
Categories: model|ranking|pricing|outage|tool|release|news|blog|changelog|update
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SITE_DATA_DIR = BASE_DIR / "site" / "data"

INPUT_FILES = [
    "status_items.json",    # outages first in priority if timestamps tie
    "prices_items.json",
    "rankings_items.json",
    "trending_items.json",
    "hn_items.json",
    "feeds_items.json",
]

ENRICHMENT_CACHE = BASE_DIR / "state" / "enriched_cache.json"


def apply_enrichment_cache(items: list[dict]) -> tuple[int, int]:
    """Re-attach LLM bilingual enrichment from the persistent cache so a
    rebuild never loses it (the old pipeline burned money then wiped zh
    fields - this is the fix). Only applies when the SOURCE material still
    matches what was enriched (src_hash); stale entries stay un-applied and
    go back to the enrichment queue in the patrol's enrich step."""
    import hashlib
    if not ENRICHMENT_CACHE.exists():
        return 0, 0
    try:
        cache = json.loads(ENRICHMENT_CACHE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0, 0
    applied = pending = 0
    for item in items:
        cached = cache.get(item.get("id", ""))
        material = f"{item.get('headline_en', '')}|{item.get('summary_en', '')}"
        current_src = hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]
        if cached and cached.get("src_hash") == current_src:
            item.update({k: v for k, v in cached.items() if k != "src_hash"})
            item["enriched"] = True
            applied += 1
        else:
            pending += 1
    return applied, pending

FEED_CAP = 60
ARCHIVE_CAP = 2000


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_items() -> list[dict]:
    items: dict[str, dict] = {}
    for name in INPUT_FILES:
        path = DATA_DIR / name
        if not path.exists():
            print(f"  skip   {name} (not present this run)")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"  skip   {name} (corrupt)")
            continue
        count = 0
        for item in payload.get("items", []):
            if item.get("id") and item.get("headline_en"):
                items[item["id"]] = item
                count += 1
        print(f"  in     {name}: {count}")
    return list(items.values())


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Archive is the memory: past items stay in the feed window until they
    # age out of the cap. Without this, a new patrol would overwrite
    # pillar files and forget items emitted in previous runs.
    archive_path = SITE_DATA_DIR / "archive.json"
    previous: dict[str, dict] = {}
    if archive_path.exists():
        try:
            for item in json.loads(archive_path.read_text(encoding="utf-8")).get("items", []):
                previous[item["id"]] = item
        except (json.JSONDecodeError, KeyError):
            previous = {}

    items = load_items()
    applied, pending = apply_enrichment_cache(items)
    if applied or pending:
        print(f"enrichment cache: applied {applied} | pending API: {pending}")
    for item in items:
        previous[item["id"]] = item   # current run overrides (fresh metrics)
    items = sorted(previous.values(),
                   key=lambda i: i.get("timestamp", ""), reverse=True)
    print(f"merged unique (incl. archive memory): {len(items)}")

    feed_items = items[:FEED_CAP]
    (SITE_DATA_DIR / "feed.json").write_text(
        json.dumps({"generated_at": utc_now(),
                    "item_count": len(feed_items), "items": feed_items},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    archive_items = items[:ARCHIVE_CAP]
    (archive_path).write_text(
        json.dumps({"generated_at": utc_now(),
                    "item_count": len(archive_items), "items": archive_items},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    by_category: dict[str, int] = {}
    for item in feed_items:
        by_category[item.get("category", "?")] = by_category.get(item.get("category", "?"), 0) + 1
    print("feed.json categories:", by_category or "{}")
    print(f"feed: {len(feed_items)} items | archive: {len(archive_items)} items "
          f"-> site/data/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
