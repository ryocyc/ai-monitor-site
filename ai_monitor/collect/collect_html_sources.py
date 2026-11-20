"""
HTML-source collector: covers the ~22 company sources that have NO RSS
(user-approved expansion). Generic article-link harvest instead of
per-site selectors:

  - fetch the listing page (Scrapling ladder: Fetcher first)
  - harvest same-domain links whose anchor text looks like a headline
  - filter with per-source path hints; diff against last-seen links
  - emit new links only (cap 3/source/run); --seed emits newest 1

Sources needing a real browser (JS SPAs) are run through
StealthyFetcher ONLY when listed in STEALTH_SOURCES - decided by
--probe measurements, keeping patrol time bounded.

Output: data/html_items.json
State:  state/html_seen.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import time
import urllib.parse

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATE_DIR = BASE_DIR / "state"
SEEN_FILE = STATE_DIR / "html_seen.json"

USER_AGENT = "AI-News-Monitor/1.0 (+https://aisignalfeed.com)"
FETCH_TIMEOUT = 20
SLEEP_SECONDS = 0.6
MIN_TEXT = 14          # CJK headlines are short; latin ones need more
MAX_NEW_PER_SOURCE = 3
SEEN_CAP = 200

# (source_id, display name, listing url, path_include substrings, path_exclude substrings)
HTML_SOURCES = [
    ("deepseek",     "DeepSeek",      "https://www.deepseek.com/",              [],        []),
    ("minimax",      "MiniMax Blog",  "https://www.minimax.io/blog",            ["/blog", "/news"], []),
    ("zhipu",        "Zhipu News",    "https://www.zhipuai.cn/news",            [],        []),
    ("stepfun",      "StepFun News",  "https://www.stepfun.com/news",           [],        []),
    ("bytedance_seed","ByteDance Seed","https://team.doubao.com/",              [],        []),
    ("deepmind",     "Google DeepMind","https://deepmind.google/discover/blog/", ["/blog"], ["discover/blog?$"]),
    ("google_ai",    "Google AI Blog","https://blog.google/technology/ai/",     ["/technology/ai/"], []),
    ("meta_ai",      "Meta AI Blog",  "https://ai.meta.com/blog/",              ["/blog"], []),
    ("anthropic",    "Anthropic News","https://www.anthropic.com/news",         ["/news"], ["/engineering"]),
    ("ai21",         "AI21 Blog",     "https://www.ai21.com/blog",              ["/blog"], []),
    ("fireworks",    "Fireworks AI",  "https://fireworks.ai/blog",              ["/blog"], []),
    ("groq",         "Groq News",     "https://groq.com/news/",                 [],        []),
    ("cerebras",     "Cerebras Blog", "https://www.cerebras.ai/blog",           ["/blog"], []),
    ("gcloud_ai",    "Google Cloud AI","https://cloud.google.com/blog/products/ai-machine-learning", ["/blog/"], []),
    ("cognition",    "Cognition Blog","https://cognition.ai/blog",              ["/blog"], []),
    ("worldlabs",    "World Labs",    "https://www.worldlabs.ai/blog",          [],        []),
]

JUNK_PATTERNS = ("privacy", "terms", "careers", "about", "login", "signup",
                 "cookie", "contact", "press-kit", "brand", "legal", "/docs",
                 "/api", "/pricing", "/download", "/help", "/faq")

STEALTH_SOURCES: set[str] = set()   # filled from --probe results


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def fetch_page(url: str, stealth: bool = False):
    if stealth:
        from scrapling import StealthyFetcher
        return StealthyFetcher.fetch(url, headless=True, block_images=True,
                                     timeout=45000)
    from scrapling import Fetcher
    return Fetcher(auto_match=False).get(
        url, timeout=FETCH_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})


def absolute(base: str, href: str) -> str:
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        parsed = urllib.parse.urlparse(base)
        return f"{parsed.scheme}://{parsed.netloc}{href}"
    if href.startswith("http"):
        return href
    return urllib.parse.urljoin(base, href)


def harvest_links(page, source_url: str, include: list[str], exclude: list[str]) -> list[dict]:
    base_domain = urllib.parse.urlparse(source_url).netloc.lower()
    seen: set[str] = set()
    out: list[dict] = []
    for a in page.css("a[href]"):
        text = clean(a.text or "")
        if len(text) < MIN_TEXT:
            # card layouts: the anchor wraps an image while the headline
            # lives in the parent card - fall back to parent text
            try:
                parent_text = clean(a.parent.get_all_text() or "")
            except Exception:  # noqa: BLE001
                parent_text = ""
            if MIN_TEXT <= len(parent_text) <= 200:
                text = parent_text
        href = (a.attrib or {}).get("href", "")
        if not text or len(text) < MIN_TEXT or len(text) > 160:
            continue
        if href.startswith(("#", "mailto:", "javascript:")):
            continue
        link = absolute(source_url, href)
        parsed = urllib.parse.urlparse(link)
        if parsed.netloc.lower() != base_domain:
            continue
        path = parsed.path.lower()
        lowered = (text + " " + path).lower()
        if any(j in lowered for j in JUNK_PATTERNS):
            continue
        if exclude and any(x.lower() in path for x in exclude):
            continue
        if include and not any(x.lower() in path for x in include):
            continue
        if link.rstrip("/") == source_url.rstrip("/"):
            continue
        if link in seen:
            continue
        seen.add(link)
        out.append({"link": link, "title": text})
    return out


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")


def make_item(source_id: str, name: str, link: str, title: str) -> dict:
    return {
        "id": "ht-" + hashlib.sha1(f"{source_id}|{link}".encode()).hexdigest()[:12],
        "timestamp": utc_now(),
        "category": "news",
        "source_name": name,
        "source_url": link,
        "target_url": link,
        "headline_en": title[:200],
        "summary_en": f"New post on {name}: {title[:180]}",
    }


def main() -> int:
    if sys.stdout is not None and sys.stdout.encoding \
            and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true",
                        help="diagnostic: report harvested-link counts per source, emit nothing")
    parser.add_argument("--seed", action="store_true",
                        help="first run: record all links, emit newest 1 per source")
    parser.add_argument("--only", type=str, default="", help="comma-separated source ids")
    args = parser.parse_args()

    sources = HTML_SOURCES
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        sources = [s for s in HTML_SOURCES if s[0] in wanted]

    seen = load_seen()
    items: list[dict] = []
    ok_count = 0
    new_seen: dict[str, list[str]] = {}

    for source_id, name, url, include, exclude in sources:
        stealth = source_id in STEALTH_SOURCES
        page_text_len = 0
        links: list[dict] = []
        try:
            page = fetch_page(url, stealth=stealth)
            if page.status == 200:
                ok_count += 1
                page_text_len = len(page.get_all_text() or "")
                links = harvest_links(page, url, include, exclude)
        except Exception as exc:  # noqa: BLE001
            print(f"  ?      {name}: fetch failed ({type(exc).__name__})")

        # ladder: plain fetch got a shell page -> try stealth once
        if not stealth and (page_text_len < 400 or not links):
            try:
                page = fetch_page(url, stealth=True)
                if page.status == 200:
                    links = harvest_links(page, url, include, exclude)
                    if links:
                        ok_count += 1
            except Exception:  # noqa: BLE001
                pass

        known = set(seen.get(source_id, []))
        fresh = [l for l in links if l["link"] not in known]
        merged = list(dict.fromkeys(
            [l["link"] for l in links] + seen.get(source_id, [])))[:SEEN_CAP]
        new_seen[source_id] = merged

        if args.probe:
            print(f"  {name:18} links={len(links):3} fresh={len(fresh):3} "
                  f"text={page_text_len}")
            continue

        if args.seed:
            emit = fresh[:1]
        else:
            emit = fresh[:MAX_NEW_PER_SOURCE]
        for l in emit:
            items.append(make_item(source_id, name, l["link"], l["title"]))
        tag = "SEED" if args.seed else ("NEW" if fresh else "ok  ")
        print(f"  {tag:5}  {name:18} links={len(links):3} fresh={len(fresh):2} "
              f"emit={len(emit)}")

        time.sleep(SLEEP_SECONDS)

    if not args.probe:
        save_seen({**seen, **new_seen})
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "html_items.json").write_text(
            json.dumps({"generated_at": utc_now(), "item_count": len(items),
                        "items": items}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"Sources ok: {ok_count}/{len(sources)} | emitted: {len(items)} "
              f"-> data/html_items.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
