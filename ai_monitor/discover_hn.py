"""
Hacker News early-signal discovery: AI-related stories that are gaining
traction on HN before they show up anywhere else. Uses the free
hn.algolia.com API (no auth), a handful of polite queries per run.

Detection: stories from the last 48h with points above thresholds, matched
against the same AI keyword list as the GitHub discovery, ranked by
points-per-hour velocity.

Output:
  data/hn_items.json        feed-ready items (same contract as trending_items)
  data/hn_candidates.json   full detail for review

Note: dedupe against previously emitted items happens at feed-build time (D2);
each run re-emits the current hot set with fresh point counts.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

SEARCH_SLEEP_SECONDS = 2.0
REQUEST_TIMEOUT = 20
ARTICLE_TIMEOUT = 12
ARTICLE_SLEEP_SECONDS = 0.8
ARTICLE_MAX = 12          # fetch substance only for the top stories
USER_AGENT = "AI-News-Monitor/1.0 (+https://aisignalfeed.com)"
WINDOW_HOURS = 48

# (query, min-points, per-page) — points thresholds keep result sets small
QUERY_PLAN = [
    ("hn_ai",       "AI",               120, 30),
    ("hn_llm",      "LLM",               50, 30),
    ("hn_gpt",      "GPT",               50, 30),
    ("hn_agent",    "AI agent",          40, 30),
    ("hn_oss",      "open source AI",    40, 30),
]

AI_KEYWORDS = [
    "llm", "gpt", "chatgpt", "claude", "gemini", "openai", "anthropic",
    "ai", "a.i.", "agent", "agentic", "rag", "embedding", "mcp",
    "model context protocol", "diffusion", "stable diffusion", "midjourney",
    "tts", "speech", "voice", "whisper", "ocr", "vision", "multimodal",
    "fine-tuning", "finetune", "quantization", "inference", "vllm",
    "ollama", "llama", "deepseek", "qwen", "mistral", "copilot",
    "machine learning", "neural", "transformer", "prompt",
]

EXCLUDE_PATTERNS = [
    "hiring", "who is hiring", "job", "salary",
]

HOT_MIN_POINTS = 150        # within window -> hot
RISING_MIN_POINTS = 50      # within window -> rising
RISING_MIN_PER_HOUR = 1.0   # and fast enough

MAX_ITEMS = 20


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def fetch_hits(url_path: str) -> list[dict] | None:
    """Query hn.algolia.com (free public API, no auth). None = failed."""
    url = f"https://hn.algolia.com/api/v1/{url_path}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace")).get("hits", [])
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError,
            json.JSONDecodeError) as exc:
        print(f"[warn] HN query failed: {exc}", file=sys.stderr)
        return None


def word_match(text: str, patterns: list[str]) -> bool:
    lowered = text.lower()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", lowered)
               for p in patterns)


def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = text.replace("&gt;", ">").replace("&lt;", "<") \
               .replace("&amp;", "&").replace("&quot;", '"').replace("&#x27;", "'")
    return re.sub(r"\s+", " ", text).strip()


def fetch_article_text(url: str) -> str:
    """Fetch the linked article's main text so items carry substance,
    not just a headline. Graceful None on any failure."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(request, timeout=ARTICLE_TIMEOUT) as resp:
            raw = resp.read().decode(resp.headers.get_content_charset() or "utf-8",
                                     errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        return ""
    raw = re.sub(r"(?is)<(script|style|noscript|nav|footer|header).*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<(p|div|section|article|li|h1|h2|h3|br)[^>]*>", "\n", raw)
    text = _clean_html(re.sub(r"<[^>]+>", " ", raw))
    # prefer the longest mid-page chunk (nav/boilerplate live at the edges)
    lines = [l for l in text.splitlines() if len(l) > 80]
    body = max(lines, key=len, default=text)
    return body[:800]


def fetch_discussion_text(object_id: str) -> str:
    """Ask-HN / text posts: pull the story text + top comments from the free
    Algolia item API (no scraping)."""
    url = f"https://hn.algolia.com/api/v1/items/{object_id}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, ConnectionError,
            ValueError, json.JSONDecodeError):
        return ""
    parts = [_clean_html(data.get("text") or "")]
    children = data.get("children") or []
    for comment in children[:3]:
        body = _clean_html(comment.get("text") or "")
        if len(body) > 60:
            parts.append(body[:300])
    return " ".join(p for p in parts if p)[:600]


def parse_story(hit: dict) -> dict | None:
    title = (hit.get("title") or hit.get("story_title") or "").strip()
    object_id = hit.get("objectID") or ""
    if not title or not object_id:
        return None
    points = hit.get("points") or 0
    comments = hit.get("num_comments") or 0
    created_i = hit.get("created_at_i") or 0
    if not created_i:
        return None
    age_hours = max((dt.datetime.now(dt.timezone.utc).timestamp() - created_i) / 3600.0, 0.5)
    article_url = hit.get("url") or ""
    discussion_url = f"https://news.ycombinator.com/item?id={object_id}"
    return {
        "title": title,
        "points": points,
        "comments": comments,
        "age_hours": round(age_hours, 1),
        "velocity_per_hour": round(points / age_hours, 2),
        "article_url": article_url,
        "discussion_url": discussion_url,
        "author": hit.get("author") or "",
    }


def classify(story: dict) -> str | None:
    if story["points"] >= HOT_MIN_POINTS:
        return "hn_hot"
    if story["points"] >= RISING_MIN_POINTS and \
            story["velocity_per_hour"] >= RISING_MIN_PER_HOUR:
        return "hn_rising"
    return None


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cutoff = int(dt.datetime.now(dt.timezone.utc).timestamp() - WINDOW_HOURS * 3600)
    raw: dict[str, dict] = {}
    queries_run = 0
    for tag, query, min_points, per_page in QUERY_PLAN:
        url_path = (
            "search?"
            + urllib.parse.urlencode({
                "query": query,
                "tags": "story",
                "numericFilters": f"points>{min_points},created_at_i>{cutoff}",
                "hitsPerPage": per_page,
            })
        )
        hits = fetch_hits(url_path)
        queries_run += 1
        for hit in hits or []:
            object_id = hit.get("objectID") or ""
            if object_id and object_id not in raw:
                hit["_query_tag"] = tag
                raw[object_id] = hit
        time.sleep(SEARCH_SLEEP_SECONDS)

    if queries_run == 0:
        print("[error] No Hacker News queries succeeded.")
        return 1

    candidates = []
    filtered = 0
    for hit in raw.values():
        story = parse_story(hit)
        if not story:
            continue
        if word_match(story["title"], EXCLUDE_PATTERNS):
            filtered += 1
            continue
        if not word_match(story["title"], AI_KEYWORDS):
            filtered += 1
            continue
        tier = classify(story)
        if tier is None:
            continue
        candidates.append({**story, "tier": tier, "query_tags": [hit.get("_query_tag", "")]})

    candidates.sort(key=lambda c: (c["velocity_per_hour"], c["points"]), reverse=True)
    top = candidates[:MAX_ITEMS]

    # substance pass: article text or discussion comments for the top stories,
    # so enrichment rewrites from real content instead of title-only
    for story in top:
        excerpt = ""
        if story.get("article_url"):
            excerpt = fetch_article_text(story["article_url"])
        if not excerpt:
            excerpt = fetch_discussion_text(
                story["discussion_url"].rsplit("=", 1)[-1])
        story["content_excerpt"] = excerpt
        time.sleep(ARTICLE_SLEEP_SECONDS)

    items = []
    for c in top:
        excerpt = c.get("content_excerpt", "")
        summary = (
            f"The Hacker News community is actively discussing '{c['title']}' — "
            f"{c['points']} points in {c['age_hours']:.0f}h (~{c['velocity_per_hour']:.1f}/h)."
        )
        if excerpt:
            summary += f" Content excerpt: {excerpt[:400]}"
        items.append({
            "id": "hn-" + hashlib.sha1(c["discussion_url"].encode("utf-8")).hexdigest()[:12],
            "timestamp": utc_now(),
            "category": "trending",
            "source_name": "Hacker News",
            "source_url": c["discussion_url"],
            "target_url": c["article_url"] or c["discussion_url"],
            "headline_en": f"HN: {c['title']} ({c['points']} points, {c['comments']} comments)",
            "summary_en": summary,
            "tier": c["tier"],
            "metrics": {
                "points": c["points"],
                "comments": c["comments"],
                "age_hours": c["age_hours"],
                "velocity_per_hour": c["velocity_per_hour"],
            },
        })

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "hn_items.json").write_text(
        json.dumps({"generated_at": utc_now(), "item_count": len(items), "items": items},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA_DIR / "hn_candidates.json").write_text(
        json.dumps({
            "generated_at": utc_now(),
            "queries_run": queries_run,
            "raw_unique": len(raw),
            "filtered_out": filtered,
            "candidate_count": len(candidates),
            "candidates": candidates,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Queries run: {queries_run} | unique stories: {len(raw)} | "
          f"filtered out: {filtered} | qualified: {len(candidates)}")
    print("-" * 78)
    for c in candidates[:12]:
        tier = {"hn_hot": "HOT    ", "hn_rising": "RISING "}[c["tier"]]
        print(f"{tier} | {c['points']:5,} pts | {c['velocity_per_hour']:6.1f}/h | "
              f"{c['age_hours']:5.1f}h old | {c['title'][:48]}")
    print("-" * 78)
    print(f"Wrote {len(items)} items -> data/hn_items.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
