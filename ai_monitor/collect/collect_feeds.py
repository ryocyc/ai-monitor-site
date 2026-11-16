"""
Pillars 1+5 collector: parse RSS/Atom feeds from feeds.json
  releases = GitHub /releases.atom feeds (version releases + tool updates)
  news     = company blog/changelog feeds (new model / product posts)

Emits feed items for entries not seen before. First run with --seed records
everything and emits only the newest entry per feed (so the site starts with
content, without flooding).

Output: data/feeds_items.json
State:  state/feeds_seen.json
Exit 1 only if EVERY feed failed (patrol alerting).
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
FEEDS_CONFIG = BASE_DIR / "feeds.json"
STATE_DIR = BASE_DIR / "state"
SEEN_FILE = STATE_DIR / "feeds_seen.json"

USER_AGENT = "AI-News-Monitor/1.0 (+https://aisignalfeed.com)"
REQUEST_TIMEOUT = 20
SLEEP_SECONDS = 0.5
MAX_NEW_PER_FEED = 5
SEEN_CAP = 300


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def fetch_feed(url: str) -> str | None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            return resp.read().decode(resp.headers.get_content_charset() or "utf-8",
                                      errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
        print(f"[warn] {url}: {exc}", file=sys.stderr)
        return None


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_date(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        return email.utils.parsedate_to_datetime(value).astimezone(
            dt.timezone.utc).replace(microsecond=0).isoformat()
    except (ValueError, TypeError):
        pass
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
    except ValueError:
        return ""


def parse_entries(xml_text: str, limit: int = 40) -> list[dict]:
    """Return entries as {id(link/guid), title, link, published}. RSS + Atom."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    entries: list[dict] = []
    is_atom = local(root.tag) == "feed"
    if is_atom:
        nodes = [e for e in root.iter() if local(e.tag) == "entry"][:limit]
        for node in nodes:
            title = link = updated = ""
            for child in node:
                tag = local(child.tag)
                if tag == "title":
                    title = _clean(child.text or "")
                elif tag == "updated" or tag == "published":
                    updated = updated or (child.text or "")
                elif tag == "link":
                    if (child.attrib or {}).get("href") and not link:
                        link = child.attrib["href"]
            entries.append({"id": link or title, "title": title, "link": link,
                            "published": _parse_date(updated)})
    else:
        nodes = [e for e in root.iter() if local(e.tag) == "item"][:limit]
        for node in nodes:
            title = link = pub = guid = ""
            for child in node:
                tag = local(child.tag)
                if tag == "title":
                    title = _clean(child.text or "")
                elif tag == "link":
                    link = (child.text or "").strip()
                elif tag == "guid":
                    guid = (child.text or "").strip()
                elif tag in ("pubDate", "published", "updated"):
                    pub = pub or (child.text or "")
            entries.append({"id": link or guid or title, "title": title,
                            "link": link, "published": _parse_date(pub)})
    return [e for e in entries if e["title"]]


def load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_seen(seen: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")


def make_item(feed: dict, entry: dict, category: str) -> dict:
    return {
        "id": "fd-" + hashlib.sha1(f"{feed['id']}|{entry['id']}".encode()).hexdigest()[:12],
        "timestamp": entry["published"] or utc_now(),
        "category": category,
        "source_name": feed["name"],
        "source_url": feed["url"],
        "target_url": entry["link"] or feed["url"],
        "headline_en": entry["title"][:200],
        "summary_en": f"New entry from {feed['name']}: {entry['title'][:180]}",
    }


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", action="store_true",
                        help="first run: record all entries, emit newest 1 per feed")
    args = parser.parse_args()

    config = json.loads(FEEDS_CONFIG.read_text(encoding="utf-8"))
    feeds = [(f, "release") for f in config.get("releases", [])] + \
            [(f, f.get("category", "news")) for f in config.get("news", [])]

    seen = load_seen()
    items: list[dict] = []
    ok_count = 0
    new_seen: dict[str, list[str]] = {}

    for feed, category in feeds:
        xml_text = fetch_feed(feed["url"])
        if xml_text is None:
            new_seen[feed["id"]] = seen.get(feed["id"], [])
            time.sleep(SLEEP_SECONDS)
            continue
        ok_count += 1
        entries = parse_entries(xml_text)
        known = set(seen.get(feed["id"], []))
        fresh = [e for e in entries if e["id"] not in known]

        # record everything we saw now (capped)
        merged = list(dict.fromkeys(
            [e["id"] for e in entries] + seen.get(feed["id"], [])))[:SEEN_CAP]
        new_seen[feed["id"]] = merged

        if args.seed:
            emit = fresh[:1]  # newest entry only (feed order is newest-first)
            print(f"  SEED   {feed['name']}: {len(entries)} entries, emitted {len(emit)}")
        else:
            emit = fresh[:MAX_NEW_PER_FEED]
            if fresh:
                print(f"  NEW    {feed['name']}: {len(fresh)} new (emitted {len(emit)})")
        for entry in emit:
            items.append(make_item(feed, entry, category))
        time.sleep(SLEEP_SECONDS)

    save_seen({**seen, **new_seen})
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "feeds_items.json").write_text(
        json.dumps({"generated_at": utc_now(), "item_count": len(items), "items": items},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Feeds ok: {ok_count}/{len(feeds)} | emitted: {len(items)} "
          f"-> data/feeds_items.json")
    return 0 if ok_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
