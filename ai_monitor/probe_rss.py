"""
Probe candidate AI-company sources for official RSS/Atom feeds and
anti-bot (Cloudflare) exposure. One-time / occasional tool, NOT part of the
polling loop.

For each candidate page:
  1. GET the page once (honest UA) -> record status + CDN signals
  2. Parse <link rel="alternate" type="application/rss+xml|atom+xml">
  3. If none found, try two common feed paths (/feed.xml, /rss.xml)
  4. GET the feed once and validate it parses as XML with entries

Output:
  data/rss_probe_report.json  - per-source facts
Console: compact availability table + suggested ban-risk tier.

Ban-risk tier rules:
  LOW    = page 200, no Cloudflare signals, or an RSS feed exists
  MEDIUM = Cloudflare present but page 200
  HIGH   = page 403/429/challenge, or repeated failures
Polling guidance: LOW every 2h, MEDIUM every 6h (RSS when available),
HIGH only via RSS if one exists, else manual review before adding.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

USER_AGENT = "AI-News-Monitor/1.0 (+https://aisignalfeed.com; one-time source probe)"
REQUEST_TIMEOUT = 15
SLEEP_SECONDS = 0.5

# (display name, landing page to probe)
CANDIDATES = [
    # Tier A - major AI labs
    ("OpenAI Newsroom",        "https://openai.com/newsroom/"),
    ("Anthropic News",         "https://www.anthropic.com/news"),
    ("Google DeepMind Blog",   "https://deepmind.google/discover/blog/"),
    ("Google AI Blog",         "https://blog.google/technology/ai/"),
    ("Meta AI Blog",           "https://ai.meta.com/blog/"),
    ("Microsoft AI Blog",      "https://blogs.microsoft.com/ai/"),
    ("NVIDIA AI Blog",         "https://blogs.nvidia.com/"),
    ("xAI Blog",               "https://x.ai/news"),
    ("Mistral News",           "https://mistral.ai/news/"),
    ("Cohere Blog",            "https://cohere.com/blog"),
    ("Perplexity Blog",        "https://www.perplexity.ai/hub/blog"),
    ("Hugging Face Blog",      "https://huggingface.co/blog"),
    ("Stability AI News",      "https://stability.ai/news"),
    ("Together AI Blog",       "https://www.together.ai/blog"),
    ("Fireworks AI Blog",      "https://fireworks.ai/blog"),
    ("Groq News",              "https://groq.com/news/"),
    ("Cerebras Blog",          "https://www.cerebras.ai/blog"),
    ("AI21 Blog",              "https://www.ai21.com/blog"),
    # Tier B - Chinese labs
    ("DeepSeek Home",          "https://www.deepseek.com/"),
    ("Qwen Blog",              "https://qwenlm.github.io/"),
    ("Moonshot/Kimi News",     "https://moonshotai.github.io/"),
    ("MiniMax Blog",           "https://www.minimax.io/blog"),
    ("Zhipu News",             "https://www.zhipuai.cn/news"),
    ("StepFun News",           "https://www.stepfun.com/news"),
    ("01.AI Home",             "https://www.01.ai/"),
    # Tier D - platform / infra blogs
    ("OpenRouter Changelog",   "https://openrouter.ai/changelog"),
    ("Vercel AI Blog",         "https://vercel.com/blog/tag/ai"),
    ("Cloudflare AI Blog",     "https://blog.cloudflare.com/tag/ai/"),
    ("AWS ML Blog",            "https://aws.amazon.com/blogs/machine-learning/"),
    ("Google Cloud AI Blog",   "https://cloud.google.com/blog/products/ai-machine-learning"),
]

COMMON_FEED_PATHS = ["feed.xml", "rss.xml"]

FEED_LINK_RE = re.compile(
    r"""<link[^>]+rel=["']alternate["'][^>]*>""",
    re.IGNORECASE,
)
FEED_TYPE_RE = re.compile(r"""type=["']application/(rss|atom)\+xml["']""", re.IGNORECASE)
HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)


def fetch(url: str) -> tuple[int | None, str, dict[str, str]]:
    """GET a URL. Returns (status, body, headers). status=None on network error."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read().decode(resp.headers.get_content_charset() or "utf-8",
                                      errors="replace")
            return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, "", dict(exc.headers or {})
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        return None, "", {}


def is_cloudflared(headers: dict[str, str]) -> bool:
    joined = " ".join(f"{k.lower()}:{str(v).lower()}" for k, v in headers.items())
    return "server:cloudflare" in joined or "cf-ray" in joined


def looks_like_challenge(body: str) -> bool:
    return "Just a moment" in body or "cf-browser-verification" in body or \
        "Enable JavaScript and cookies" in body


def find_feed_link(page_html: str, page_url: str) -> str | None:
    for link in FEED_LINK_RE.findall(page_html):
        if not FEED_TYPE_RE.search(link):
            continue
        href_match = HREF_RE.search(link)
        if not href_match:
            continue
        href = href_match.group(1).strip()
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            from urllib.parse import urlparse
            parsed = urlparse(page_url)
            return f"{parsed.scheme}://{parsed.netloc}{href}"
        if href.startswith("http"):
            return href
    return None


def validate_feed(feed_url: str) -> tuple[bool, int]:
    status, body, _ = fetch(feed_url)
    if status != 200 or not body:
        return False, status or 0
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return False, status
    tags = {elem.tag.rsplit("}", 1)[-1] for elem in root.iter()}
    return ("entry" in tags or "item" in tags), status


def probe_one(name: str, page_url: str) -> dict:
    status, body, headers = fetch(page_url)
    record: dict = {
        "name": name,
        "page_url": page_url,
        "page_status": status,
        "cloudflare": is_cloudflared(headers),
        "challenge": looks_like_challenge(body) if status == 200 else False,
        "feed_url": None,
        "feed_status": None,
        "feed_entries": False,
    }
    if status == 200 and body:
        feed_url = find_feed_link(body, page_url)
        tried: list[str] = []
        if not feed_url:
            from urllib.parse import urlparse
            parsed = urlparse(page_url)
            base = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/"
            for path in COMMON_FEED_PATHS:
                tried.append(base + path)
        candidate_feeds = [feed_url] if feed_url else tried
        for candidate in candidate_feeds:
            ok, feed_status = validate_feed(candidate)
            if ok:
                record["feed_url"] = candidate
                record["feed_status"] = feed_status
                record["feed_entries"] = True
                break

    # ban-risk tier
    if status is None:
        record["risk_tier"] = "HIGH"
        record["note"] = "network error"
    elif status in (403, 429) or record["challenge"]:
        record["risk_tier"] = "HIGH"
        record["note"] = f"blocked/challenge ({status if status else 'challenge'})"
    elif record["feed_url"]:
        record["risk_tier"] = "LOW"
        record["note"] = "RSS available"
    elif record["cloudflare"]:
        record["risk_tier"] = "MEDIUM"
        record["note"] = "cloudflare front, no feed found on page"
    else:
        record["risk_tier"] = "LOW"
        record["note"] = "plain page, no feed found"
    return record


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    results = []
    for name, url in CANDIDATES:
        record = probe_one(name, url)
        results.append(record)
        feed = "RSS" if record["feed_url"] else "---"
        print(f"{record['risk_tier']:6} | page {str(record['page_status']):4} | "
              f"{feed} | {'CF' if record['cloudflare'] else '--'} | {name}")
        time.sleep(SLEEP_SECONDS)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "rss_probe_report.json").write_text(
        json.dumps({"generated_at": dt.datetime.now(dt.timezone.utc)
                    .replace(microsecond=0).isoformat(),
                    "probed": len(results),
                    "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    feeds = sum(1 for r in results if r["feed_url"])
    tiers = {"LOW": 0, "MEDIUM": 0, "HIGH": 0}
    for r in results:
        tiers[r["risk_tier"]] += 1
    print("-" * 70)
    print(f"Probed {len(results)} sources: {feeds} with RSS | "
          f"risk LOW {tiers['LOW']} / MEDIUM {tiers['MEDIUM']} / HIGH {tiers['HIGH']}")
    print("Wrote data/rss_probe_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
