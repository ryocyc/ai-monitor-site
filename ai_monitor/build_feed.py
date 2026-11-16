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
import re
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SITE_DATA_DIR = BASE_DIR / "site" / "data"
SITE_DIR = BASE_DIR / "site"
ARTICLES_DIR = SITE_DIR / "articles"
DOMAIN = "https://aisignalfeed.com"
RSS_CAP = 40


def _xml_escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;") \
        .replace(">", "&gt;").replace('"', "&quot;")


def _rfc822(ts: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(ts)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.strftime("%a, %d %b %Y %H:%M:%S +0000")
    except ValueError:
        return ""


def write_rss(feed_items: list[dict]) -> None:
    """RSS 2.0 from the feed; items link to their article page when one
    exists (keeps readers on-site), else to the external source."""
    mapping: dict[str, str] = {}
    mapping_path = ARTICLES_DIR / "article-mapping.json"
    if mapping_path.exists():
        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            mapping = {}
    entries = []
    for item in feed_items[:RSS_CAP]:
        slug = mapping.get(item.get("id", ""))
        link = f"{DOMAIN}/articles/{slug}.html" if slug else \
            (item.get("target_url") or DOMAIN)
        title = item.get("headline_zh") or item.get("headline_en") or "AI update"
        desc = item.get("summary_zh") or item.get("summary_en") or ""
        entries.append(
            "    <item>\n"
            f"      <title>{_xml_escape(title)}</title>\n"
            f"      <link>{_xml_escape(link)}</link>\n"
            f"      <guid isPermaLink=\"false\">{_xml_escape(item.get('id', ''))}</guid>\n"
            f"      <pubDate>{_rfc822(item.get('timestamp', ''))}</pubDate>\n"
            f"      <category>{_xml_escape(item.get('category', 'update'))}</category>\n"
            f"      <description>{_xml_escape(desc)}（來源：{_xml_escape(item.get('source_name', ''))}）</description>\n"
            "    </item>")
    rss = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        '  <channel>\n'
        '    <title>AI Signal Feed — AI 界今日變動，用戶視角雙語新聞卡</title>\n'
        f'    <link>{DOMAIN}</link>\n'
        '    <description>新模型、價格變動、服務故障、爆紅開源工具 — 自動監察、AI 編寫的繁中新聞卡與深度文章。</description>\n'
        f'    <atom:link href="{DOMAIN}/rss.xml" rel="self" type="application/rss+xml"/>\n'
        '    <language>zh-Hant</language>\n'
        f'    <lastBuildDate>{dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")}</lastBuildDate>\n'
        + "\n".join(entries) + "\n  </channel>\n</rss>\n")
    (SITE_DIR / "rss.xml").write_text(rss, encoding="utf-8")


def write_status_board() -> None:
    """Homepage AI status board: current indicator per provider straight
    from the status collector's state (no LLM, zero noise)."""
    state_path = BASE_DIR / "state" / "status.json"
    if not state_path.exists():
        return
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    providers = {}
    for name, info in state.items():
        if not isinstance(info, dict):
            continue
        providers[name] = {
            "indicator": info.get("indicator", "unknown"),
            "description": info.get("description", ""),
            "updated_at": info.get("updated_at"),
        }
    (SITE_DATA_DIR / "status_board.json").write_text(
        json.dumps({"generated_at": utc_now(), "providers": providers},
                   ensure_ascii=False, indent=2), encoding="utf-8")


def write_sitemap() -> None:
    urls = [f"{DOMAIN}/", f"{DOMAIN}/contact.html", f"{DOMAIN}/privacy.html",
            f"{DOMAIN}/terms.html", f"{DOMAIN}/rss.xml"]
    if ARTICLES_DIR.exists():
        for meta in ARTICLES_DIR.glob("*.meta.json"):
            # only new-system articles (meta carries "fingerprint"); the 596
            # pre-restart evt-* pages are stale English data - SEO negative
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if "fingerprint" not in data:
                continue
            urls.append(f"{DOMAIN}/articles/{meta.stem[:-5]}.html")
    body = "\n".join(f"  <url><loc>{u}</loc></url>" for u in urls)
    (SITE_DIR / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + body + "\n</urlset>\n", encoding="utf-8")
    (SITE_DIR / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {DOMAIN}/sitemap.xml\n",
        encoding="utf-8")

INPUT_FILES = [
    "status_items.json",    # outages first in priority if timestamps tie
    "prices_items.json",
    "rankings_items.json",
    "trending_items.json",
    "feeds_items.json",
    # hn_items.json removed with the HN source (user directive: HN is an
    # aggregator; reposting it adds no value)
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
    volatile_tail = re.compile(r" Gained [\d,]+ GitHub stars.*$")
    for item in items:
        cached = cache.get(item.get("id", ""))
        stable_summary = volatile_tail.sub("", item.get('summary_en', '') or '')
        material = f"v5|{item.get('headline_en', '')}|{stable_summary}"
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
    write_rss(feed_items)
    write_sitemap()
    write_status_board()
    print("feed.json categories:", by_category or "{}")
    print(f"feed: {len(feed_items)} items | archive: {len(archive_items)} items "
          f"-> site/data/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
