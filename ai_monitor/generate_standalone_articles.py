from __future__ import annotations

import argparse
import datetime as dt
import html
import json
from json import JSONDecodeError
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any

from quality_gates import classify_source_type, score_content_specificity
from source_parse_helpers import title_to_slug


BASE_DIR = pathlib.Path(__file__).resolve().parent
SITE_DIR = BASE_DIR / "site"
DATA_DIR = SITE_DIR / "data"
ARTICLES_DIR = SITE_DIR / "articles"

ALLOWED_CATEGORIES = {"blog", "news", "release", "docs", "changelog", "pricing", "update"}
BLOCKED_SOURCE_TYPES = {"status", "homepage"}

GENERIC_HEADLINE_PATTERNS = (
    re.compile(r"\bnew tag detected\b", re.IGNORECASE),
    re.compile(r"\bupdate detected\b", re.IGNORECASE),
    re.compile(r"\bci test tag\b", re.IGNORECASE),
    re.compile(r"\bdevelopment snapshot\b", re.IGNORECASE),
    re.compile(r"\brelease tag bumps?\b", re.IGNORECASE),
    re.compile(r"\btag bumps?\b", re.IGNORECASE),
    re.compile(r"\b(updated|refresh(?:ed)?|changed|modified)\b", re.IGNORECASE),
    re.compile(r"\ball systems operational\b", re.IGNORECASE),
    re.compile(r"\bsubscribe to updates\b", re.IGNORECASE),
    re.compile(r"\bprivacy policy\b", re.IGNORECASE),
    re.compile(r"\bterms of service\b", re.IGNORECASE),
    re.compile(r"\blogin\b", re.IGNORECASE),
    re.compile(r"\bsign in\b", re.IGNORECASE),
    re.compile(r"\bhome page\b", re.IGNORECASE),
    re.compile(r"\bhomepage\b", re.IGNORECASE),
)

GENERIC_IDENTITY_PATTERNS = (
    re.compile(r"^(update|news|blog|home|index|article|page|feed)$", re.IGNORECASE),
    re.compile(r"-(update|news|blog|home|index|article|page|feed)$", re.IGNORECASE),
    re.compile(r"^(.*-)?(status|pricing|changelog|releases?)$", re.IGNORECASE),
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_time(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except Exception:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def format_display_time(value: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except Exception:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("\uFFFD", " ")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[^\S\r\n]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clip(text: str, limit: int) -> str:
    text = clean_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def read_json(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {"generated_at": None, "item_count": 0, "items": []}
    return json.loads(path.read_text(encoding="utf-8"))


def load_items() -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for path in (DATA_DIR / "archive.json", DATA_DIR / "latest.json"):
        payload = read_json(path)
        for item in payload.get("items", []):
            key = item.get("id") or item.get("merge_key") or item.get("article_identity") or item.get("target_url")
            if not key:
                continue
            if key in merged:
                merged[key] = {**merged[key], **item}
            else:
                merged[key] = dict(item)
    items = list(merged.values())
    items.sort(key=lambda item: parse_time(item.get("timestamp", "")), reverse=True)
    return items


def is_generic_identity(identity: str) -> bool:
    identity = clean_text(identity)
    if not identity:
        return True
    if len(identity) < 8 and not re.search(r"\d|[a-z]-[a-z]", identity):
        return True
    return any(pattern.match(identity) for pattern in GENERIC_IDENTITY_PATTERNS)


def is_generic_headline(headline: str, source_name: str = "") -> bool:
    headline = clean_text(headline)
    source_name = clean_text(source_name)
    if not headline:
        return True
    if len(headline) < 12:
        return True
    if source_name and headline.lower() == source_name.lower():
        return True
    if re.fullmatch(r".+\s+(blog|news|changelog|status|pricing|docs)", headline, flags=re.IGNORECASE):
        if not re.search(r"\b(v?\d+(?:\.\d+)*(?:-[a-z0-9.]+)?)\b", headline):
            return True
    if re.fullmatch(r"(blog|news|changelog|status|pricing|docs)\s+.*", headline, flags=re.IGNORECASE):
        return True
    return any(pattern.search(headline) for pattern in GENERIC_HEADLINE_PATTERNS)


def is_entry_url(url: str) -> bool:
    url = (url or "").lower().strip()
    if not url:
        return True
    if url.endswith(("/", "/index.html", "/home.html", "/main.html")):
        return True
    entry_terms = (
        "/docs", "/documentation", "/pricing", "/plans", "/billing", "/status",
        "/uptime", "/health", "/blog", "/news", "/articles", "/changelog",
        "/about", "/company", "/careers", "/privacy", "/terms",
        "status.", "blog.", "news.", "docs.",
    )
    if any(term in url for term in entry_terms):
        return True
    if "github.com/" in url and "/releases/tag/" not in url and "/blob/" not in url and "/pull/" not in url:
        return True
    return False


def url_slug(url: str) -> str:
    parsed = urllib.parse.urlparse(url or "")
    path = parsed.path.strip("/")
    if not path:
        return ""
    slug = title_to_slug(path.replace("/", " "))
    return slug


def source_slug(source_name: str) -> str:
    slug = title_to_slug(clean_text(source_name))
    return slug or "source"


def article_key(item: dict[str, Any]) -> str | None:
    source = source_slug(item.get("source_name", ""))
    article_identity = clean_text(item.get("article_identity", "") or "")
    target_url = clean_text(item.get("target_url", "") or "")
    merge_key = clean_text(item.get("merge_key", "") or "")
    headline = clean_text(item.get("headline_en", "") or "")

    if article_identity and not is_generic_identity(article_identity):
        return f"{source}::identity::{article_identity}"

    if target_url and not is_entry_url(target_url):
        slug = url_slug(target_url)
        if slug:
            return f"{source}::url::{slug}"

    if merge_key and not is_generic_identity(merge_key):
        return f"{source}::merge::{title_to_slug(merge_key)}"

    if headline and not is_generic_headline(headline):
        return f"{source}::headline::{title_to_slug(headline)}"

    return None


def article_worthiness(item: dict[str, Any]) -> tuple[bool, int, list[str]]:
    score, reason = score_content_specificity(item)
    reasons = [reason]

    category = clean_text(item.get("category", "") or "").lower()
    source_name = clean_text(item.get("source_name", "") or "")
    source_url = clean_text(item.get("source_url", "") or "")
    headline = clean_text(item.get("headline_en", "") or "")
    summary = clean_text(item.get("summary_en", "") or "")
    target_url = clean_text(item.get("target_url", "") or "")
    article_identity = clean_text(item.get("article_identity", "") or "")
    source_type = classify_source_type(source_name, source_url)

    if category in {"release", "news", "blog", "docs", "changelog", "pricing"}:
        score += 16
        reasons.append(f"category:{category}")
    elif category == "update":
        score += 4
        reasons.append("category:update")
    else:
        score -= 20
        reasons.append(f"category:{category or 'missing'}")

    if source_type in {"news_blog", "docs_api", "pricing", "changelog", "github_org"}:
        score += 10
        reasons.append(f"source_type:{source_type}")
    elif source_type in BLOCKED_SOURCE_TYPES:
        score -= 45
        reasons.append(f"blocked_source_type:{source_type}")
    elif source_type == "unknown":
        score -= 6
        reasons.append("source_type:unknown")

    concrete_signal = any(
        term in f"{headline} {summary}".lower()
        for term in ("launch", "releases", "released", "adds", "announces", "introduces", "general availability", "ga", "pricing", "docs", "changelog", "deprecat", "retires", "ships")
    )

    if is_generic_headline(headline, source_name=source_name):
        score -= 35
        reasons.append("generic_headline")
        if not concrete_signal:
            reasons.append("blocked_generic_shell")
            return False, max(0, min(100, score)), reasons
    else:
        reasons.append("specific_headline")

    if article_identity and not is_generic_identity(article_identity):
        score += 18
        reasons.append("specific_identity")
    else:
        score -= 18
        reasons.append("generic_identity")

    if target_url and not is_entry_url(target_url):
        score += 10
        reasons.append("specific_target_url")
    elif target_url:
        score -= 24
        reasons.append("entry_target_url")

    if concrete_signal:
        score += 10
        reasons.append("concrete_change_signal")

    evidence = clean_text(item.get("evidence_excerpt", "") or "")
    noisy_terms = (
        "subscribe", "privacy policy", "terms of service", "login", "sign in",
        "captcha", "otp", "cookie", "all systems operational", "uptime over the past 90 days",
        "feed", "home", "front page",
    )
    noise_hits = sum(1 for term in noisy_terms if term in evidence.lower())
    if noise_hits >= 5:
        score -= 28
        reasons.append("very_noisy_evidence")
    elif noise_hits >= 3:
        score -= 12
        reasons.append("noisy_evidence")

    specific_payload = bool(article_identity and not is_generic_identity(article_identity)) or (target_url and not is_entry_url(target_url))
    if not specific_payload:
        score -= 25
        reasons.append("no_specific_payload")

    allow = score >= 72 and category in ALLOWED_CATEGORIES and source_type not in BLOCKED_SOURCE_TYPES
    return allow, max(0, min(100, score)), reasons


def fingerprint_for_item(item: dict[str, Any]) -> str:
    """Compute a content fingerprint for change detection."""
    candidates = [
        item.get("target_title", ""),
        item.get("target_summary", ""),
        item.get("target_excerpt", ""),
        item.get("evidence_excerpt", ""),
        item.get("headline_en", ""),
        item.get("summary_en", ""),
    ]
    import re
    def normalize_key_text(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    fingerprint = " ".join(normalize_key_text(value) for value in candidates if value)
    fingerprint = re.sub(r"\s+", " ", fingerprint).strip()
    return fingerprint[:600]


def read_existing_mapping() -> dict[str, str]:
    path = ARTICLES_DIR / "article-mapping.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_existing_article_fingerprints() -> dict[str, str]:
    """
    Read per-article-slug fingerprints stored in .meta.json sidecar files.
    Fingerprints are keyed by slug (the filename stem of the article HTML),
    matching what's stored in article-mapping.json.
    """
    meta: dict[str, str] = {}
    for path in ARTICLES_DIR.glob("*.meta.json"):
        slug = path.stem  # e.g. "gpt-image-2-7eac15" from "gpt-image-2-7eac15.meta.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            fingerprint = data.get("content_fingerprint", "")
            if slug and fingerprint:
                meta[slug] = fingerprint
        except Exception:
            continue
    return meta


def article_worth_score(item: dict[str, Any]) -> int:
    allow, score, _ = article_worthiness(item)
    return score if allow else 0


def normalize_key_text_for_fingerprint(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def content_fingerprint(item: dict[str, Any]) -> str:
    candidates = [
        item.get("target_title", ""),
        item.get("target_summary", ""),
        item.get("target_excerpt", ""),
        item.get("evidence_excerpt", ""),
        item.get("headline_en", ""),
        item.get("summary_en", ""),
    ]
    fingerprint = " ".join(normalize_key_text_for_fingerprint(value) for value in candidates if value)
    import re
    fingerprint = re.sub(r"\s+", " ", fingerprint).strip()
    return fingerprint[:600]


def token_signature(text: str) -> set[str]:
    STOPWORDS = {
        "a", "an", "and", "announces", "article", "articles", "blog", "blogs",
        "data", "detected", "face", "fast", "for", "guide", "hugging", "in",
        "launches", "model", "new", "news", "ocr", "on", "openai", "post",
        "synthetic", "the", "update", "updated", "with",
    }
    tokens = [token for token in normalize_key_text_for_fingerprint(text).split() if len(token) > 2 and token not in STOPWORDS]
    return set(tokens)


def fingerprint_similarity(left: str, right: str) -> float:
    left_set = token_signature(left)
    right_set = token_signature(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def select_articles_incremental(
    all_items: list[dict[str, Any]],
    existing_mapping: dict[str, str],
    existing_fingerprints: dict[str, str],
    limit: int,
) -> list[dict[str, Any]]:
    """
    Incrementally select article candidates.

    Priority order:
      1. Items with no existing article page (new coverage)
         - sorted by article_worth_score descending, then timestamp descending
      2. Items with an existing article page but content has actually changed
         - sorted by article_worth_score descending, then timestamp descending
         - only included if fingerprint similarity < 0.72 (meaningful content change)

    Items that already have a page AND have not meaningfully changed are skipped.

    Key format: uses article_identity (the stored mapping key) as primary identifier.
    For lookups we try article_identity, then merge_key, then id, since these
    are the key_fields used when building article-mapping.json.
    """
    def lookup_slug(item: dict[str, Any]) -> str:
        """Find the existing slug for this item by checking all key fields."""
        for field in ("article_identity", "merge_key", "id"):
            val = item.get(field, "") or ""
            if val and val in existing_mapping:
                return existing_mapping[val]
        return ""

    scored: list[tuple[str, int, dict[str, Any]]] = []

    for item in all_items:
        key = article_key(item)
        if not key:
            continue
        allow, score, reasons = article_worthiness(item)
        if not allow:
            continue

        item["_article_key"] = key
        item["_article_score"] = score
        item["_article_reasons"] = reasons

        slug = lookup_slug(item)

        if not slug:
            # Priority 1: brand new article coverage
            scored.append(("new", 0, item))
        else:
            # Check if content meaningfully changed
            current_fp = content_fingerprint(item)
            previous_fp = existing_fingerprints.get(slug, "")
            if previous_fp:
                similarity = fingerprint_similarity(previous_fp, current_fp)
            else:
                similarity = 0.0

            if similarity < 0.72:
                # Priority 2: existing article with meaningful content change
                scored.append(("changed", 1, item))
            # else: skip — no new page needed and content hasn't meaningfully changed

    # Sort: new items first (priority 0), then changed items (priority 1),
    # within each group by score desc, then timestamp desc
    def sort_key(entry: tuple[str, int, dict[str, Any]]) -> tuple[int, int, float]:
        _, priority, item = entry
        return (priority, -item["_article_score"], -parse_time(item.get("timestamp", "")).timestamp())

    scored.sort(key=sort_key)

    result: list[dict[str, Any]] = []
    for priority, _, item in scored[:limit]:
        result.append(item)
    return result


def select_articles(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Legacy score-based selector used only by --force-refresh mode."""
    best_by_key: dict[str, dict[str, Any]] = {}

    for item in items:
        allow, score, reasons = article_worthiness(item)
        if not allow:
            continue

        key = article_key(item)
        if not key:
            continue

        candidate = dict(item)
        candidate["_article_key"] = key
        candidate["_article_score"] = score
        candidate["_article_reasons"] = reasons

        existing = best_by_key.get(key)
        if existing is None:
            best_by_key[key] = candidate
            continue

        current_score = existing["_article_score"]
        current_time = parse_time(existing.get("timestamp", ""))
        candidate_time = parse_time(candidate.get("timestamp", ""))

        if score > current_score or (score == current_score and candidate_time > current_time):
            best_by_key[key] = candidate

    selected = list(best_by_key.values())
    selected.sort(key=lambda item: parse_time(item.get("timestamp", "")), reverse=True)

    used_slugs: set[str] = set()
    for item in selected:
        slug = unique_slug(pick_slug_base(item), used_slugs, item)
        item["_article_slug"] = slug
    return selected


def pick_slug_base(item: dict[str, Any]) -> str:
    candidates = [
        item.get("article_identity", "") or "",
        item.get("headline_en", "") or "",
        item.get("target_title", "") or "",
        item.get("source_name", "") or "",
        item.get("id", "") or "",
    ]
    for candidate in candidates:
        slug = title_to_slug(clean_text(candidate))
        if not slug:
            continue
        if slug in {"update", "news", "blog", "home", "index", "article", "page"}:
            continue
        if len(slug) < 8 and candidate != item.get("id", ""):
            continue
        return slug[:96].strip("-")
    return "article"


def unique_slug(base_slug: str, used: set[str], item: dict[str, Any]) -> str:
    slug = base_slug or "article"
    if slug not in used:
        used.add(slug)
        return slug

    suffix = clean_text(item.get("id", "") or "")[-6:] or clean_text(item.get("timestamp", "") or "")[:10].replace("-", "")
    candidate = f"{slug}-{suffix}" if suffix else f"{slug}-copy"
    counter = 2
    while candidate in used:
        candidate = f"{slug}-{suffix}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def render_paragraphs(paragraphs: list[str]) -> str:
    return "\n".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in paragraphs if paragraph)


def render_bullets(items: list[str]) -> str:
    if not items:
        return "<p>No bullet points were generated for this page.</p>"
    lis = "".join(f"<li>{html.escape(item)}</li>" for item in items if item)
    return f'<ul class="bullets">{lis}</ul>'


def body_section(title: str, inner_html: str) -> str:
    return f"""
      <section class="section">
        <h2>{html.escape(title)}</h2>
        {inner_html}
      </section>
    """


def derive_h1(item: dict[str, Any]) -> str:
    headline = clean_text(item.get("headline_en", "") or "")
    target_title = clean_text(item.get("target_title", "") or "")
    if headline and len(headline) <= 100:
        return headline
    if target_title and len(target_title) <= 100:
        return target_title
    return clip(headline or target_title or "Article update", 100)


def build_local_article(item: dict[str, Any]) -> dict[str, Any]:
    category = clean_text(item.get("category", "") or "update").lower()
    source_name = clean_text(item.get("source_name", "") or "Unknown source")
    headline = clean_text(item.get("headline_en", "") or derive_h1(item))
    summary = clean_text(item.get("summary_en", "") or "")
    target_title = clean_text(item.get("target_title", "") or "")
    target_summary = clean_text(item.get("target_summary", "") or "")
    target_excerpt = clean_text(item.get("target_excerpt", "") or "")
    evidence = clean_text(item.get("evidence_excerpt", "") or "")
    target_url = clean_text(item.get("target_url", "") or "")
    source_url = clean_text(item.get("source_url", "") or "")
    timestamp = clean_text(item.get("timestamp", "") or "")
    score = item.get("_article_score", 0)

    topic = summary or target_summary or target_excerpt or target_title or headline
    standfirst = clip(
        f"{topic} This standalone article is intentionally narrow and stays within what the monitored page actually shows.",
        240,
    )

    article_body = [
        f"{headline} is the clearest visible signal in the monitored page for {source_name}.",
        f"The page text points to {clip(topic or 'a concrete update', 180)} and we keep the wording close to that evidence instead of inflating it.",
        "Because this flow is built to stay honest, it does not fill gaps with invented context when the page is thin or noisy.",
    ]
    why_matters = [
        "Specific product, release, and docs signals are more useful than generic homepage churn.",
        "Filtering out status pages and repeated noise keeps the standalone article set readable.",
        "A narrow article still preserves the signal even when the source page changes again later.",
    ]
    source_note = [
        f"Source: {source_name}",
        f"Category: {category}",
        f"Source URL: {source_url or 'n/a'}",
        f"Target URL: {target_url or 'n/a'}",
        f"Timestamp: {format_display_time(timestamp) if timestamp else 'Unknown'}",
        f"Score: {score}",
    ]

    if target_title:
        source_note.append(f"Target title: {clip(target_title, 180)}")
    if target_summary:
        source_note.append(f"Target summary: {clip(target_summary, 180)}")
    if target_excerpt:
        source_note.append(f"Target excerpt: {clip(target_excerpt, 180)}")
    if evidence:
        source_note.append(f"Evidence excerpt: {clip(evidence, 220)}")

    return {
        "seo_title": clip(headline, 82),
        "h1": derive_h1(item),
        "standfirst": standfirst,
        "article_body": article_body,
        "why_matters": why_matters,
        "source_note": source_note,
        "confidence": "rule-based",
        "generation_mode": "local",
    }


def extract_visible_text(html_text: str) -> str:
    text = html_text
    text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\\1>", " ", text)
    text = re.sub(r"(?is)<svg.*?>.*?</svg>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[^\S\r\n]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_url_text(url: str, timeout: int = 20) -> tuple[str, str]:
    if not url:
        return "", ""

    candidates = [url]
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        stripped = url.split("://", 1)[1]
        candidates.append(f"https://r.jina.ai/http://{stripped}")

    for candidate in candidates:
        try:
            request = urllib.request.Request(
                candidate,
                headers={
                    "User-Agent": "Mozilla/5.0 (Codex standalone article generator)",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                encoding = response.headers.get_content_charset() or "utf-8"
                text = raw.decode(encoding, errors="replace")
                visible = extract_visible_text(text)
                if len(visible) >= 200:
                    return candidate, visible
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, UnicodeDecodeError):
            continue

    return "", ""


def build_claude_prompt(item: dict[str, Any], source_text: str, fetched_url: str) -> str:
    source_packet = {
        "id": item.get("id", ""),
        "source_name": item.get("source_name", ""),
        "source_url": item.get("source_url", ""),
        "target_url": item.get("target_url", ""),
        "headline_en": item.get("headline_en", ""),
        "summary_en": item.get("summary_en", ""),
        "target_title": item.get("target_title", ""),
        "target_summary": item.get("target_summary", ""),
        "target_excerpt": item.get("target_excerpt", ""),
        "evidence_excerpt": item.get("evidence_excerpt", ""),
        "article_identity": item.get("article_identity", ""),
        "category": item.get("category", ""),
        "fetched_from": fetched_url,
        "source_text": clip(source_text, 12000),
    }

    return (
        "Write one standalone article from the live source page capture below.\n"
        "Use the source text itself as the primary evidence, not the prewritten summary.\n"
        "Stay honest: if the page is thin, ambiguous, or noisy, say that plainly instead of inventing detail.\n"
        "Return strict JSON only with these keys:\n"
        "- seo_title\n"
        "- h1\n"
        "- standfirst\n"
        "- article_body (exactly 3 short paragraphs)\n"
        "- why_this_matters (exactly 3 short bullets)\n"
        "- source_note (1 short paragraph)\n"
        "- confidence (one of: strong, moderate, weak)\n"
        "Guidance:\n"
        "- Make the SEO title concise and readable.\n"
        "- Keep the H1 specific and factual.\n"
        "- The standfirst should be 1 sentence.\n"
        "- The article body should read like a tight news/article page, not a generic template.\n"
        "- Do not mention unseen facts, numbers, or dates.\n"
        "- If the page looks like status chrome, a feed card, or a generic root page, explain that in a cautious way.\n"
        "- Prefer concrete wording for releases, blog posts, docs changes, pricing shifts, and product announcements.\n\n"
        f"{json.dumps(source_packet, ensure_ascii=False)}"
    )


def call_claude(prompt: str, max_budget_usd: float) -> dict[str, Any]:
    command = [
        "claude",
        "-p",
        "--bare",
        "--output-format",
        "json",
        "--max-budget-usd",
        str(max_budget_usd),
        "--permission-mode",
        "bypassPermissions",
        prompt,
    ]
    result = subprocess.run(command, capture_output=True)
    stdout_text = result.stdout.decode("utf-8", errors="replace")
    stderr_text = result.stderr.decode("utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(
            "Claude CLI failed.\n"
            f"STDOUT:\n{stdout_text}\n"
            f"STDERR:\n{stderr_text}"
        )
    payload = json.loads(stdout_text)
    content = payload.get("result", "").strip()
    parsed = parse_claude_json_content(content)
    if parsed is not None:
        return parsed
    repaired = repair_claude_json(content, max_budget_usd=min(max_budget_usd, 0.2))
    if repaired is not None:
        return repaired
    raise RuntimeError(f"Claude returned non-JSON content:\n{content}")


def parse_claude_json_content(content: str) -> dict[str, Any] | None:
    content = (content or "").strip()
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None

    candidate = content[start : end + 1]
    try:
        return json.loads(candidate)
    except JSONDecodeError:
        compact = re.sub(r"[\x00-\x08\x0b-\x1f]", " ", candidate)
        decoder = json.JSONDecoder()
        for index, char in enumerate(compact):
            if char != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(compact[index:])
                if isinstance(parsed, dict):
                    return parsed
            except JSONDecodeError:
                continue
        return None


def repair_claude_json(content: str, max_budget_usd: float) -> dict[str, Any] | None:
    repair_prompt = (
        "Convert the response below into strict JSON only.\n"
        "Return exactly these keys:\n"
        "- seo_title\n"
        "- h1\n"
        "- standfirst\n"
        "- article_body (exactly 3 short paragraphs)\n"
        "- why_this_matters (exactly 3 short bullets)\n"
        "- source_note (1 short paragraph)\n"
        "- confidence (one of: strong, moderate, weak)\n"
        "Do not add markdown fences or commentary. If the source text below is incomplete, preserve only what is supported.\n\n"
        f"{content}"
    )
    command = [
        "claude",
        "-p",
        "--bare",
        "--output-format",
        "json",
        "--max-budget-usd",
        str(max_budget_usd),
        "--permission-mode",
        "bypassPermissions",
        repair_prompt,
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0:
        return None
    stdout_text = result.stdout.decode("utf-8", errors="replace")
    try:
        payload = json.loads(stdout_text)
    except JSONDecodeError:
        return None
    return parse_claude_json_content(payload.get("result", ""))


def coerce_paragraphs(value: Any, max_items: int) -> list[str]:
    if isinstance(value, list):
        paragraphs = [clean_text(str(item)) for item in value]
    elif isinstance(value, str):
        raw = value.replace("\r\n", "\n")
        split = [clean_text(part) for part in re.split(r"\n{2,}|\n[-*]\s+", raw)]
        paragraphs = [part for part in split if part]
        if not paragraphs and clean_text(raw):
            paragraphs = [clean_text(raw)]
    else:
        paragraphs = [clean_text(str(value))] if value else []
    return [paragraph for paragraph in paragraphs if paragraph][:max_items]


def coerce_bullets(value: Any, max_items: int) -> list[str]:
    if isinstance(value, list):
        bullets = [clean_text(str(item)) for item in value]
    elif isinstance(value, str):
        raw = value.replace("\r\n", "\n")
        split = [clean_text(part) for part in re.split(r"\n{2,}|\n[-*]\s+", raw)]
        bullets = [part for part in split if part]
        if not bullets and clean_text(raw):
            bullets = [clean_text(raw)]
    else:
        bullets = [clean_text(str(value))] if value else []
    return [bullet for bullet in bullets if bullet][:max_items]


def build_claude_article(item: dict[str, Any], timeout: int, max_budget_usd: float) -> dict[str, Any] | None:
    fetch_targets = [item.get("target_url", ""), item.get("source_url", "")]
    fetched_url = ""
    fetched_text = ""
    for url in fetch_targets:
        fetched_url, fetched_text = fetch_url_text(clean_text(url), timeout=timeout)
        if fetched_text:
            break
    if not fetched_text:
        return None

    prompt = build_claude_prompt(item, fetched_text, fetched_url)
    raw = call_claude(prompt, max_budget_usd=max_budget_usd)
    article_body = coerce_paragraphs(raw.get("article_body", []), 3)
    why_matters = coerce_bullets(raw.get("why_this_matters", []), 3)
    source_note_text = clean_text(raw.get("source_note", ""))
    return {
        "seo_title": clip(clean_text(raw.get("seo_title", "")) or clean_text(item.get("headline_en", "")), 82),
        "h1": clean_text(raw.get("h1", "")) or derive_h1(item),
        "standfirst": clip(clean_text(raw.get("standfirst", "")) or clean_text(item.get("summary_en", "")), 240),
        "article_body": article_body,
        "why_matters": why_matters,
        "source_note": [source_note_text] if source_note_text else [],
        "confidence": clean_text(raw.get("confidence", "moderate")) or "moderate",
        "generation_mode": "claude",
        "fetched_from": fetched_url,
    }


def build_article_payload(item: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if args.with_claude:
        try:
            drafted = build_claude_article(item, timeout=args.fetch_timeout, max_budget_usd=args.claude_budget_usd)
        except Exception as exc:
            print(f"[warn] Claude generation failed for {item.get('id', '')}: {exc}", file=sys.stderr)
            drafted = None
        if drafted:
            return drafted
    return build_local_article(item)


def article_page(item: dict[str, Any], payload: dict[str, Any], generated_at: str) -> str:
    category = clean_text(item.get("category", "") or "update").lower()
    source_name = clean_text(item.get("source_name", "") or "Unknown source")
    timestamp = clean_text(item.get("timestamp", "") or "")
    source_url = clean_text(item.get("source_url", "") or "")
    target_url = clean_text(item.get("target_url", "") or "")
    page_title = f"{payload['seo_title']} | AI Signal Articles"
    display_time = format_display_time(timestamp) if timestamp else "Unknown time"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(page_title)}</title>
  <meta name="description" content="{html.escape(clip(payload['standfirst'], 155))}">
  <style>
    :root {{
      --bg: #f6efe4;
      --surface: rgba(255,255,255,0.88);
      --ink: #182018;
      --muted: #556055;
      --accent: #0f766e;
      --accent-soft: rgba(15,118,110,0.12);
      --border: rgba(15,118,110,0.16);
      --shadow: 0 18px 42px rgba(27, 39, 33, 0.09);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      font-family: "Segoe UI", system-ui, sans-serif;
      background: linear-gradient(180deg, #fcf8f2 0%, var(--bg) 100%);
    }}
    main {{
      max-width: 940px;
      margin: 0 auto;
      padding: 32px 18px 56px;
    }}
    .topline, .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px 14px;
      align-items: center;
    }}
    .topline {{ justify-content: space-between; margin-bottom: 22px; color: var(--muted); font-size: 0.92rem; }}
    .chip {{
      display: inline-flex;
      align-items: center;
      padding: 6px 10px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 0.74rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}
    .hero {{
      padding: 28px 28px 24px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 24px;
      box-shadow: var(--shadow);
      position: relative;
      overflow: hidden;
    }}
    .hero::after {{
      content: "";
      position: absolute;
      inset: auto -10% -45% auto;
      width: 280px;
      height: 280px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(15,118,110,0.16), transparent 70%);
      pointer-events: none;
    }}
    h1 {{
      margin: 14px 0 12px;
      font-family: Georgia, "Times New Roman", serif;
      font-size: clamp(2rem, 5vw, 4.1rem);
      line-height: 0.95;
      letter-spacing: -0.04em;
      max-width: 14ch;
    }}
    .standfirst {{
      margin: 0;
      max-width: 70ch;
      color: var(--muted);
      line-height: 1.7;
      font-size: 1.02rem;
    }}
    .meta {{
      margin-top: 18px;
      color: var(--muted);
      font-size: 0.92rem;
    }}
    .content {{
      margin-top: 20px;
      display: grid;
      gap: 16px;
    }}
    .section {{
      padding: 20px 22px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: var(--shadow);
    }}
    .section h2 {{
      margin: 0 0 8px;
      font-size: 1.08rem;
      letter-spacing: -0.01em;
    }}
    .section p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.72;
    }}
    .section ul {{
      margin: 0;
      padding-left: 1.1rem;
      color: var(--muted);
      line-height: 1.72;
    }}
    .section li + li {{
      margin-top: 0.45rem;
    }}
    a {{ color: var(--accent); text-decoration: none; font-weight: 700; }}
    .footer {{
      margin-top: 18px;
      color: var(--muted);
      font-size: 0.92rem;
    }}
  </style>
</head>
<body>
  <main>
    <div class="topline">
      <div><a href="../index.html">Homepage</a> | <a href="../history.html">History</a></div>
      <div>Generated {html.escape(format_display_time(generated_at))}</div>
    </div>
    <article class="hero">
      <span class="chip">{html.escape(category)}</span>
      <h1>{html.escape(payload['h1'])}</h1>
      <p class="standfirst">{html.escape(payload['standfirst'])}</p>
      <div class="meta">
        <span>{html.escape(source_name)}</span>
        <span>{html.escape(display_time)}</span>
        <span>Key: {html.escape(item.get('_article_key', ''))}</span>
        <span>Confidence: {html.escape(payload.get('confidence', 'moderate'))}</span>
        <span>Mode: {html.escape(payload.get('generation_mode', 'local'))}</span>
      </div>
    </article>
    <section class="content">
      {body_section("Article body", render_paragraphs(payload.get("article_body", [])))}
      {body_section("Why this matters", render_bullets(payload.get("why_matters", [])))}
      {body_section("Source note", render_bullets(payload.get("source_note", [])))}
      <section class="section">
        <h2>Original link</h2>
        <p><a href="{html.escape(target_url or source_url or '#')}" rel="nofollow noopener">Open the monitored source</a></p>
      </section>
    </section>
    <div class="footer">
      This article was generated from filtered monitored source changes.
    </div>
  </main>
</body>
</html>
"""


def render_index_page(items: list[dict[str, Any]], generated_at: str) -> str:
    rows: list[str] = []
    for item in items:
        rows.append(
            f"""
          <article class="card">
            <div class="row">
              <span class="pill">{html.escape(clean_text(item.get('category', '') or 'update'))}</span>
              <time>{html.escape(format_display_time(item.get('timestamp', '')))}</time>
            </div>
            <h2><a href="./{html.escape(item['_article_slug'])}.html">{html.escape(clean_text(item.get('headline_en', '') or item.get('target_title', '') or 'Untitled article'))}</a></h2>
            <p>{html.escape(clip(item.get('_standfirst', ''), 220))}</p>
            <div class="row">
              <span>{html.escape(clean_text(item.get('source_name', '') or 'Unknown source'))}</span>
              <a href="{html.escape(clean_text(item.get('target_url', '') or item.get('source_url', '') or '#'))}">Source</a>
            </div>
          </article>
        """
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Signal Articles</title>
  <meta name="description" content="Filtered standalone article pages generated from high-value monitored items.">
  <style>
    :root {{
      --bg: #f6efe4;
      --surface: rgba(255,255,255,0.88);
      --ink: #182018;
      --muted: #566056;
      --accent: #0f766e;
      --accent-soft: rgba(15,118,110,0.12);
      --border: rgba(15,118,110,0.16);
      --shadow: 0 18px 42px rgba(27, 39, 33, 0.09);
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: "Segoe UI", system-ui, sans-serif; color: var(--ink); background: linear-gradient(180deg, #fcf8f2 0%, var(--bg) 100%); }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 34px 18px 60px; }}
    .hero {{ display: grid; gap: 12px; margin-bottom: 20px; }}
    .eyebrow {{ display: inline-flex; width: fit-content; padding: 7px 12px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; font-size: 0.76rem; }}
    h1 {{ margin: 0; font-family: Georgia, "Times New Roman", serif; font-size: clamp(2.2rem, 5vw, 4.8rem); line-height: 0.96; letter-spacing: -0.04em; max-width: 12ch; }}
    .sub {{ margin: 0; max-width: 72ch; color: var(--muted); line-height: 1.7; }}
    .toolbar {{ display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; align-items: center; margin-bottom: 18px; color: var(--muted); }}
    .grid {{ display: grid; gap: 16px; }}
    .card {{ display: grid; gap: 12px; padding: 20px 22px; background: var(--surface); border: 1px solid var(--border); border-radius: 18px; box-shadow: var(--shadow); }}
    .row {{ display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; color: var(--muted); font-size: 0.9rem; }}
    .pill {{ display: inline-flex; align-items: center; padding: 6px 10px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); text-transform: uppercase; font-weight: 700; font-size: 0.74rem; letter-spacing: 0.04em; }}
    h2 {{ margin: 0; font-size: 1.4rem; line-height: 1.15; letter-spacing: -0.02em; }}
    p {{ margin: 0; color: var(--muted); line-height: 1.68; }}
    a {{ color: var(--accent); text-decoration: none; font-weight: 700; }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <span class="eyebrow">Standalone article flow</span>
      <h1>{len(items)} filtered pages.</h1>
      <p class="sub">These pages are generated from the existing monitor data, but only for specific high-value items. Repeated updates, status-page chrome, and generic entry pages are filtered out before any article file is written.</p>
    </section>
    <div class="toolbar">
      <div>Generated {html.escape(format_display_time(generated_at))}</div>
      <div><a href="../index.html">Homepage</a> | <a href="../history.html">History</a></div>
    </div>
    <section class="grid">
      {''.join(rows)}
    </section>
  </main>
</body>
</html>
"""


def purge_articles_dir() -> None:
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    for path in ARTICLES_DIR.glob("*.html"):
        path.unlink()


def write_article_meta(item: dict[str, Any]) -> None:
    """Write a JSON sidecar with fingerprint and key for change detection."""
    slug = item.get("_article_slug", "")
    key = item.get("_article_key", "")
    fp = content_fingerprint(item)
    # Key by slug (like article-mapping.json), not article_key, so
    # existing_fingerprints lookups by slug work consistently
    meta = {"article_key": key, "content_fingerprint": fp, "slug": slug}
    if slug:
        (ARTICLES_DIR / f"{slug}.meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate standalone article pages from monitored site data.")
    parser.add_argument("--limit", type=int, default=3, help="Maximum new/updated article pages to process per run (default: 3)")
    parser.add_argument("--with-claude", action="store_true", help="Use Claude CLI to draft each article from a live source-page fetch.")
    parser.add_argument("--claude-budget-usd", type=float, default=0.6, help="Per-article Claude budget when --with-claude is enabled.")
    parser.add_argument("--fetch-timeout", type=int, default=20, help="Timeout in seconds for live page fetches.")
    parser.add_argument("--dry-run", action="store_true", help="Print selected candidates and exit.")
    parser.add_argument("--force-refresh", action="store_true", help="Force refresh of all eligible articles (skip change detection)")
    args = parser.parse_args()

    # Load existing state for incremental logic
    existing_mapping = read_existing_mapping()
    existing_fingerprints = read_existing_article_fingerprints()

    all_items = load_items()
    if args.force_refresh:
        # Force mode: use old select_articles (score-ordered, no incremental logic)
        selected = select_articles(all_items)[: args.limit]
    else:
        selected = select_articles_incremental(all_items, existing_mapping, existing_fingerprints, args.limit)

    if not selected:
        print("No article updates needed this round.")
        return 0

    if args.dry_run:
        print(f"=== Incremental selection (limit={args.limit}) ===")
        print(f"Existing articles in mapping: {len(existing_mapping)}")
        for item in selected:
            key = item.get("_article_key", "")
            # Reconstruct slug lookup the same way select_articles_incremental does
            slug = ""
            for field in ("article_identity", "merge_key", "id"):
                val = item.get(field, "") or ""
                if val and val in existing_mapping:
                    slug = existing_mapping[val]
                    break
            in_mapping = bool(slug)
            prev_fp = existing_fingerprints.get(slug, "")
            curr_fp = content_fingerprint(item)
            sim = fingerprint_similarity(prev_fp, curr_fp) if prev_fp else 0.0
            status = "NEW" if not in_mapping else f"changed (sim={sim:.2f})"
            print(
                f"  [{status}] {item.get('source_name', '')} | "
                f"{item.get('headline_en', '')[:60]} | key={key[:60]}"
            )
        return 0

    generated_at = utc_now()

    # Load existing articles HTML files so we can preserve ones not re-written
    existing_slugs: set[str] = set()
    for path in ARTICLES_DIR.glob("*.html"):
        existing_slugs.add(path.stem)

    # Build new mapping from scratch but only write articles for selected items
    # Preserve already-existing articles that aren't re-generated this round
    new_mapping: dict[str, str] = {}

    for item in selected:
        payload = build_article_payload(item, args)
        item["_standfirst"] = payload.get("standfirst", "")
        slug = unique_slug(pick_slug_base(item), existing_slugs, item)
        item["_article_slug"] = slug
        existing_slugs.add(slug)

        article_path = ARTICLES_DIR / f"{slug}.html"
        article_path.write_text(article_page(item, payload, generated_at), encoding="utf-8")
        write_article_meta(item)

        # Record all stable keys -> slug mapping so homepage/history linking
        # can match whichever identifier survives the publish pipeline.
        for key_field in ("article_identity", "merge_key", "id"):
            key_val = item.get(key_field, "") or ""
            if key_val:
                new_mapping[key_val] = slug

    # Merge: keep entries from existing_mapping that correspond to slugs still on disk
    # (i.e. articles from previous rounds that were NOT re-generated this round)
    for key_val, slug in existing_mapping.items():
        if slug not in new_mapping:
            # Check slug still exists on disk
            if (ARTICLES_DIR / f"{slug}.html").exists():
                new_mapping[key_val] = slug

    # Build the full items list for the index page: items with existing files on disk
    all_slugs_on_disk = {p.stem for p in ARTICLES_DIR.glob("*.html")} - {"index"}
    index_items: list[dict[str, Any]] = []
    slug_to_item: dict[str, dict[str, Any]] = {item["_article_slug"]: item for item in selected if "_article_slug" in item}

    # Re-load all articles (those preserved from previous runs + newly generated)
    for slug in sorted(all_slugs_on_disk):
        # Try to find the item in selected (newly written ones)
        if slug in slug_to_item:
            index_items.append(slug_to_item[slug])
        else:
            # For previously-written articles not re-generated, reconstruct minimal info
            # from the mapping and existing HTML for the index — keep the index complete
            for key_val, s in list(new_mapping.items()):
                if s == slug:
                    index_items.append({"_article_slug": slug, "headline_en": slug, "source_name": "", "category": "update", "timestamp": ""})
                    break

    (ARTICLES_DIR / "article-mapping.json").write_text(json.dumps(new_mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    index_path = ARTICLES_DIR / "index.html"
    if index_path.exists():
        index_path.unlink()
    print(f"Generated {len(selected)} article page(s) in {ARTICLES_DIR} (total on disk: {len(all_slugs_on_disk)}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
