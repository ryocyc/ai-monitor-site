from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import pathlib
import re
import urllib.parse
from typing import Any

from source_parse_helpers import (
    extract_openai_newsroom,
    extract_openai_changelog,
    extract_huggingface_blog,
    extract_aws_ml_blog,
    extract_xai_api,
    extract_xai_blog,
    extract_cohere_changelog,
    extract_cohere_pricing,
    extract_groq_docs,
    extract_replicate_changelog,
    extract_deepseek_home,
    extract_qwen_blog,
    extract_mistral_changelog,
    extract_generic,
    make_safe_identity,
)
from quality_gates import QualityGate, score_content_specificity, classify_source_type


BASE_DIR = pathlib.Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
SITE_DIR = BASE_DIR / "site"
DATA_DIR = SITE_DIR / "data"
EVENT_LOG = LOG_DIR / "events.jsonl"
ARTICLES_DIR = SITE_DIR / "articles"
ARTICLE_MAPPING_FILE = ARTICLES_DIR / "article-mapping.json"

PRIORITY_SOURCES = [
    "OpenAI Newsroom",
    "Anthropic News",
    "MiniMax Docs Models",
    "Google DeepMind Blog",
    "Google AI Blog",
    "Mistral News",
    "Cohere Blog",
    "Hugging Face Blog",
    "OpenRouter Changelog",
    "Groq News",
    "Microsoft AI Blog",
    "AWS Machine Learning Blog",
    "Perplexity Blog",
    "xAI API",
    "NVIDIA AI Blog",
]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def format_display_time(value: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except Exception:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    utc_value = parsed.astimezone(dt.timezone.utc)
    return utc_value.strftime("%Y-%m-%d %H:%M UTC")


def parse_time(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except Exception:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def read_events() -> list[dict[str, Any]]:
    if not EVENT_LOG.exists():
        return []
    return [json.loads(line) for line in EVENT_LOG.read_text(encoding="utf-8").splitlines() if line.strip()]


def stable_id_for_merge_key(merge_key: str) -> str:
    return f"evt-{hashlib.sha1(merge_key.encode('utf-8')).hexdigest()[:20]}"


def read_existing_content() -> dict[str, dict[str, dict[str, str]]]:
    result: dict[str, dict[str, dict[str, str]]] = {"by_id": {}, "by_merge_key": {}}
    for filename in ("archive.json", "latest.json"):
        path = DATA_DIR / filename
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload.get("items", []):
            cached = {
                "headline_en": item.get("headline_en", ""),
                "summary_en": item.get("summary_en", ""),
                "headline_zh": item.get("headline_zh", ""),
                "summary_zh": item.get("summary_zh", ""),
                "target_title": item.get("target_title", ""),
                "target_summary": item.get("target_summary", ""),
                "target_excerpt": item.get("target_excerpt", ""),
                "content_fingerprint": item.get("content_fingerprint", ""),
            }
            result["by_id"][item["id"]] = cached
            result["by_merge_key"][item.get("merge_key") or merge_key_for_item(item)] = cached
    return result


def read_previous_latest_generated_at() -> dt.datetime | None:
    path = DATA_DIR / "latest.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        generated_at = payload.get("generated_at")
        if not generated_at:
            return None
        return dt.datetime.fromisoformat(generated_at)
    except Exception:
        return None


def read_previous_latest_items() -> list[dict[str, Any]]:
    path = DATA_DIR / "latest.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload.get("items", [])
        return items if isinstance(items, list) else []
    except Exception:
        return []


def clean_text(text: str) -> str:
    text = html.unescape(text)
    text = text.replace("\uFFFD", " ")
    text = re.sub(r"[^\S\r\n]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_body_text(diff_excerpt: list[str]) -> str:
    parts: list[str] = []
    for line in diff_excerpt:
        if line.startswith(("---", "+++", "@@")):
            continue
        if line.startswith("+"):
            parts.append(line[1:])
    return clean_text(" ".join(parts))


def first_match(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = clean_text(match.group(1))
            if value:
                return value
    return None


def clip(text: str, limit: int) -> str:
    text = clean_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def sanitize_item_identity(
    article_identity: str | None,
    source_name: str,
    headline: str,
) -> str:
    """
    Clean up article_identity to prevent entry-point page noise from
    polluting the dedupe key space.

    Falls back to a short source-derived key if the proposed identity
    looks like a full page dump, generic landing-page text, or a nav chunk.
    """
    return make_safe_identity(source_name, headline, article_identity, max_length=80)


# Known noisy source names that should NEVER appear on the homepage.
# These are hard blocks applied after build_item so even legacy events
# (e.g. old Qwen GitHub org-page events still in events.jsonl) get filtered.
# Hard-block: source names that must NEVER appear on homepage regardless of content.
# NOTE: Qwen GitHub = old HTML org-page source (now replaced by Qwen GitHub Releases RSS).
_HOMEPAGE_HARD_BLOCK_SOURCES = frozenset([
    "Qwen GitHub",              # old org-page source
    "Moonshot System Status",   # status homepage noise
    "MiniMax System Status",    # status homepage noise
    "Hugging Face Changelog",   # changelog index with no specific entry
    "GitHub llama.cpp Releases", # generic RSS feed with no specific release tag
])

# Extra patterns for GitHub RSS feeds that produce generic headlines without a tag
_GITHUB_RSS_GENERIC_PATTERNS = (
    re.compile(r"^.+\s+(release feed updated|updated|changed)$"),
    re.compile(r"^[^a-zA-Z]+", re.IGNORECASE),  # starts with non-letter (e.g. numbers)
)

# Global quality gate for entry-point sources.
_GATE = QualityGate(homepage_score_min=55, allow_generic_fallback=False)


def _is_homepage_dirty(item: dict[str, Any]) -> bool:
    """
    Return True if the item has dirty/noisy content that should NOT appear
    on the homepage.

    Three layers of defense:
    1. Hard block by known noisy source name
    2. Hard block by generic fallback identity chains
    3. QualityGate generic check
    """
    # Layer 1: hard block by known noisy source name
    if item.get("source_name", "") in _HOMEPAGE_HARD_BLOCK_SOURCES:
        return True

    # Layer 2: reject generic fallback identity chains.
    article_identity = item.get("article_identity", "") or ""
    source_name = item.get("source_name", "")
    if article_identity and source_name:
        from source_parse_helpers import title_to_slug
        source_slug = title_to_slug(source_name).lower()
        id_lower = article_identity.lower()
        # If identity starts with the source slug and contains it again, it's a noisy fallback.
        if id_lower.startswith(source_slug):
            second_pos = id_lower.find(source_slug, len(source_slug))
            if second_pos != -1:
                return True
        # Also catch the case where identity is just "-{source_slug}"
        if id_lower == f'-{source_slug}':
            return True
        # Also catch the case where identity is just the source slug with a leading dash
        if id_lower.startswith('-') and id_lower.lstrip('-') == source_slug:
            return True

    # Layer 3: generic quality gate
    return _GATE.should_demote_to_archive(item)[0]


def _headline_is_generic(headline: str, source_name: str) -> bool:
    headline = (headline or "").strip()
    if not headline:
        return True
    if headline.lower() == (source_name or "").lower():
        return True
    return any(pattern.search(headline) for pattern in GENERIC_HOMEPAGE_HEADLINE_PATTERNS)


def _summary_is_generic(summary: str) -> bool:
    summary = (summary or "").strip()
    if not summary:
        return True
    return any(pattern.search(summary) for pattern in GENERIC_HOMEPAGE_SUMMARY_PATTERNS)


def _has_specific_signal(text: str) -> bool:
    text = text or ""
    return any(pattern.search(text) for pattern in SPECIFIC_SIGNAL_PATTERNS)


def _entry_point_only(item: dict[str, Any]) -> bool:
    target_url = (item.get("target_url") or "").strip().rstrip("/")
    source_url = (item.get("source_url") or "").strip().rstrip("/")
    if not target_url:
        return True
    if target_url == source_url:
        return True
    source_type = classify_source_type(item.get("source_name", ""), item.get("source_url", "") or "")
    return source_type in {"homepage", "status", "github_org"}


def _homepage_qualifies(item: dict[str, Any]) -> bool:
    if _is_homepage_dirty(item):
        return False

    source_name = item.get("source_name", "")
    headline = item.get("headline_en", "")
    summary = item.get("summary_en", "")
    article_identity = (item.get("article_identity") or "").strip()
    combined = " ".join([headline, summary, article_identity])
    source_type = classify_source_type(source_name, item.get("source_url", "") or "")
    entry_point = _entry_point_only(item)

    if source_name.endswith("System Status"):
        return False

    if source_name.endswith(" Home") and entry_point:
        return False

    if source_name == "OpenAI Newsroom" and _headline_is_generic(headline, source_name):
        return False

    if source_name.endswith("Releases"):
        if _headline_is_generic(headline, source_name):
            return False
        if item.get("target_url", "").endswith("releases.atom") and not _has_specific_signal(combined):
            return False

    if source_type == "docs_api" and entry_point:
        return False

    if source_type in {"pricing", "changelog", "docs_api", "news_blog", "homepage"}:
        if entry_point and _headline_is_generic(headline, source_name):
            return False
        if entry_point and _summary_is_generic(summary) and not _has_specific_signal(combined):
            return False

    if not article_identity or len(article_identity) > 80 or article_identity.endswith("-update-detected"):
        if _headline_is_generic(headline, source_name):
            return False

    if source_type == "homepage" and entry_point:
        return False

    return True


def _english_only(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in items:
        item["headline_zh"] = ""
        item["summary_zh"] = ""
    return items


def _is_github_release_item(item: dict[str, Any]) -> bool:
    source_name = item.get("source_name", "") or ""
    return source_name.startswith("GitHub ") and source_name.endswith("Releases")


def _github_release_family(item: dict[str, Any]) -> str:
    if not _is_github_release_item(item):
        return ""
    for url in (item.get("target_url", "") or "", item.get("source_url", "") or ""):
        parsed = urllib.parse.urlparse(url)
        if "github.com" not in parsed.netloc.lower():
            continue
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) >= 2:
            return f"github::{parts[0].lower()}/{parts[1].lower()}"
    return f"github::{release_product_name(item.get('source_name', '')).lower()}"


def _homepage_family_key(item: dict[str, Any]) -> str:
    github_family = _github_release_family(item)
    if github_family:
        return github_family
    source_name = (item.get("source_name", "") or "").strip().lower()
    category = (item.get("category", "") or "").strip().lower()
    article_identity = (item.get("article_identity", "") or "").strip().lower()
    if category == "release" and article_identity:
        return f"{source_name}::{article_identity}"
    return source_name


def _previous_homepage_release_times(items: list[dict[str, Any]]) -> dict[str, dt.datetime]:
    families: dict[str, dt.datetime] = {}
    for item in items:
        family = _github_release_family(item)
        if not family:
            continue
        item_dt = parse_time(item.get("timestamp", ""))
        if family not in families or item_dt > families[family]:
            families[family] = item_dt
    return families


def _github_release_under_cooldown(item: dict[str, Any], previous_release_times: dict[str, dt.datetime]) -> bool:
    family = _github_release_family(item)
    if not family:
        return False
    previous_dt = previous_release_times.get(family)
    if previous_dt is None:
        return False
    item_dt = parse_time(item.get("timestamp", ""))
    if item_dt <= previous_dt:
        return False
    return (item_dt - previous_dt) < dt.timedelta(hours=HOMEPAGE_GITHUB_RELEASE_COOLDOWN_HOURS)


def _select_homepage_candidates(
    items: list[dict[str, Any]],
    limit: int | None,
    previous_release_times: dict[str, dt.datetime],
    seed_items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = list(seed_items or [])
    seen_ids = {item["id"] for item in selected}
    seen_sources = {item.get("source_name", "") for item in selected}
    seen_families = {_homepage_family_key(item) for item in selected if _homepage_family_key(item)}
    github_release_count = sum(1 for item in selected if _is_github_release_item(item))

    for item in items:
        if item["id"] in seen_ids:
            continue
        source_name = item.get("source_name", "")
        family = _homepage_family_key(item)
        if source_name in seen_sources:
            continue
        if family and family in seen_families:
            continue
        if _is_github_release_item(item) and github_release_count >= HOMEPAGE_GITHUB_RELEASE_MAX:
            continue
        if _github_release_under_cooldown(item, previous_release_times):
            continue
        selected.append(item)
        seen_ids.add(item["id"])
        seen_sources.add(source_name)
        if family:
            seen_families.add(family)
        if _is_github_release_item(item):
            github_release_count += 1
        if limit is not None and len(selected) >= limit:
            break

    return selected


def is_garbled(text: str) -> bool:
    """
    Return True if text appears to be garbled/broken and should be
    replaced with English fallback. Checks for:
    - High ratio of replacement characters (U+FFFD)
    - High ratio of private-use / control characters
    - Very short strings that are mostly non-Latin
    """
    if not text or len(text.strip()) < 3:
        return True
    # Count replacement characters and private-use characters
    bad_chars = sum(1 for c in text if c in ("\ufffd", "\uf000", "\uf001", "\uf002") or (0xE000 <= ord(c) <= 0xF8FF))
    if bad_chars / max(len(text), 1) > 0.03:
        return True
    # Check for broken Chinese: mostly CJK but very short and no space (typical garble)
    cjk_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    if cjk_count > 5 and cjk_count / max(len(text), 1) > 0.7 and len(text) < 30:
        return True
    return False


def safe_text(text_en: str, text_zh: str) -> str:
    """
    Return text_zh if it is readable, otherwise fall back to text_en.
    """
    if text_zh and not is_garbled(text_zh):
        return text_zh
    return text_en


GENERIC_HOMEPAGE_HEADLINE_PATTERNS = (
    re.compile(r"\bupdate detected\b", re.IGNORECASE),
    re.compile(r"\bpage updated\b", re.IGNORECASE),
    re.compile(r"\bhomepage updated\b", re.IGNORECASE),
    re.compile(r"\brelease feed updated\b", re.IGNORECASE),
    re.compile(r"\bdocs updated\b", re.IGNORECASE),
)

GENERIC_HOMEPAGE_SUMMARY_PATTERNS = (
    re.compile(r"no specific .* currently available", re.IGNORECASE),
    re.compile(r"open the target url to see details", re.IGNORECASE),
    re.compile(r"page content changed", re.IGNORECASE),
    re.compile(r"visible update appears to be", re.IGNORECASE),
)

SPECIFIC_SIGNAL_PATTERNS = (
    re.compile(r"\bv?\d+\.\d+", re.IGNORECASE),
    re.compile(r"\bgpt[- ]image\b", re.IGNORECASE),
    re.compile(r"\bgrok[- ]?\d*", re.IGNORECASE),
    re.compile(r"\bcodex\b", re.IGNORECASE),
    re.compile(r"\badvanced\b", re.IGNORECASE),
    re.compile(r"\benterprise\b", re.IGNORECASE),
    re.compile(r"\bpro\b", re.IGNORECASE),
    re.compile(r"\blaunch(es|ed)?\b", re.IGNORECASE),
)


def parse_feed_title(text: str) -> str | None:
    return first_match(text, [r"title=([^\n]+?) updated="])


def parse_feed_link(text: str) -> str | None:
    return first_match(text, [r"link=(https?://[^\s]+)"])


def parse_first_content_url(
    text: str,
    blocked: list[str] | None = None,
    source_url: str | None = None,
) -> str | None:
    blocked = blocked or []
    candidates = re.findall(r"https?://[^\s)>\"]+", text)
    ranked: list[tuple[int, str]] = []
    for candidate in candidates:
        url = candidate.rstrip(".,")
        lower = url.lower()
        if any(token in lower for token in blocked):
            continue
        if any(lower.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")):
            continue
        score = lower.count("/") * 5
        if source_url and lower.rstrip("/") == source_url.lower().rstrip("/"):
            score -= 50
        if any(token in lower for token in ("/blog/", "/news/", "/index/", "/releases/tag/", "/developers/", "/docs/")):
            score += 20
        if any(token in lower for token in ("/hub/blog/", "/index/", "/releases/tag/")):
            score += 30
        if any(token in lower for token in ("/careers", "/help", "/privacy", "/terms")):
            score -= 20
        ranked.append((score, url))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def normalize_target_url(target_url: str | None, source_url: str | None) -> str:
    if not target_url:
        return source_url or ""
    if source_url and source_url.startswith("https://") and target_url.startswith("http://"):
        target_url = "https://" + target_url[len("http://") :]
    return target_url


def script_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


def release_product_name(source_name: str) -> str:
    return source_name.replace("GitHub ", "").replace(" Releases", "").strip()


HOMEPAGE_GITHUB_RELEASE_COOLDOWN_HOURS = 12
HOMEPAGE_GITHUB_RELEASE_MAX = 2


def extract_release_item(source_name: str, text: str) -> tuple[str, str, str | None]:
    product = release_product_name(source_name)
    version = parse_feed_title(text)
    updated = first_match(text, [r"updated=([^\n]+?) link="])
    target_url = parse_feed_link(text)
    if version:
        headline = f"{product} release feed now shows {version}"
        summary = (
            f"The monitored release feed for {product} changed and the newest visible tag is {version}. "
            f"{'The feed timestamp is ' + updated + '. ' if updated else ''}"
            "This looks like a release-level update rather than a cosmetic page edit."
        )
        return headline, clip(summary, 250), target_url
    return (
        f"{product} release activity detected",
        f"The monitored GitHub release feed for {product} changed. Open the source to confirm the newest tag and inspect release notes.",
        target_url,
    )


def extract_openai_item(text: str) -> tuple[str, str, str | None]:
    headline = first_match(
        text,
        [
            r"\[([^\]]+?)\s+(?:Product|Company|Research|Security|Safety)\s+Apr",
            r"Scaling Codex to enterprises worldwide",
        ],
    ) or "OpenAI newsroom update detected"
    target_url = parse_first_content_url(text, blocked=["images.ctfassets.net"], source_url="https://openai.com/newsroom/")
    summary = (
        "OpenAI's newsroom feed changed and the top card list now highlights "
        f"\"{headline}\" among the latest visible announcements."
    )
    return headline, summary, target_url


def extract_anthropic_item(text: str) -> tuple[str, str, str | None]:
    headline = first_match(text, [r"(Introducing Claude [^.]+)", r"(Project Glasswing)"]) or "Anthropic newsroom update detected"
    target_url = parse_first_content_url(text, source_url="https://www.anthropic.com/news")
    summary = f"Anthropic's newsroom content changed, with \"{headline}\" appearing prominently in the visible entries."
    return headline, summary, target_url


def extract_minimax_item(text: str) -> tuple[str, str, str | None]:
    focus = first_match(
        text,
        [
            r"MiniMax Speech 2\.8 ([^.]+?) MiniMax M2-her",
            r"MiniMax M2\.7 ([^.]+?) Music2\.6",
            r"speech-2\.8-hd ([^.]+?) speech-2\.8-turbo",
        ],
    )
    target_url = parse_first_content_url(text, source_url="https://platform.minimax.io/docs")
    if focus:
        return (
            "MiniMax docs highlight an updated model lineup",
            clip(
                f"MiniMax's docs snapshot now emphasizes {focus}. The change looks like a documentation refresh pointing to model or speech updates worth reviewing.",
                250,
            ),
            target_url,
        )
    return (
        "MiniMax docs update detected",
        "MiniMax's model documentation changed and may include refreshed model, speech, or capability entries.",
        target_url,
    )


def extract_google_deepmind_item(text: str) -> tuple[str, str, str | None]:
    headline = first_match(text, [r"News ([^.]+?) April 2026 Models"]) or "Google DeepMind blog update detected"
    target_url = parse_first_content_url(text, source_url="https://deepmind.google/discover/blog/")
    summary = f"Google DeepMind's latest visible blog entry appears to be \"{headline}\", suggesting a refreshed top-of-feed story."
    return headline, summary, target_url


def extract_google_ai_item(text: str) -> tuple[str, str, str | None]:
    headline = first_match(text, [r"AI (.+?) An overview of Google"]) or "Google AI blog update detected"
    target_url = parse_first_content_url(text, source_url="https://blog.google/technology/ai/")
    summary = f"Google's AI blog changed, and the most prominent visible item now points to \"{headline}\"."
    return headline, summary, target_url


def extract_generic_blog_item(source_name: str, text: str, source_url: str | None) -> tuple[str, str, str | None]:
    headline = first_match(
        text,
        [
            r"Title:\s*([^.:\n]+)",
            r"#\s*([^\n]+)",
            r"([A-Z][A-Za-z0-9][^.]{18,90})",
        ],
    ) or f"{source_name} update detected"
    target_url = parse_first_content_url(
        text,
        blocked=["framerusercontent.com", "images.ctfassets.net"],
        source_url=source_url,
    )
    summary = clip(
        f"{source_name} changed and the leading visible topic appears to be \"{headline}\". Open the source to verify details and capture the exact release note.",
        250,
    )
    return headline, summary, target_url


def build_item(event: dict[str, Any]) -> dict[str, Any]:
    source_name = event.get("source_name", "Unknown source")
    body = extract_body_text(event.get("diff_excerpt", []))
    target_url: str | None = None
    article_identity: str | None = None

    if source_name.startswith("GitHub "):
        headline, summary, target_url = extract_release_item(source_name, body)
        category = "release"
    elif source_name == "OpenAI Newsroom":
        headline, summary, target_url, article_identity = extract_openai_newsroom(body)
        category = "news"
    elif source_name == "OpenAI API Changelog":
        headline, summary, target_url, article_identity = extract_openai_changelog(body)
        category = "docs"
    elif source_name == "Hugging Face Blog":
        headline, summary, target_url, article_identity = extract_huggingface_blog(body)
        category = "blog"
    elif source_name == "AWS Machine Learning Blog":
        headline, summary, target_url, article_identity = extract_aws_ml_blog(body)
        category = "blog"
    elif source_name == "xAI API":
        headline, summary, target_url, article_identity = extract_xai_api(body)
        category = "docs"
    elif source_name == "xAI Blog":
        headline, summary, target_url, article_identity = extract_xai_blog(body)
        category = "blog"
    elif source_name == "Cohere Changelog":
        headline, summary, target_url, article_identity = extract_cohere_changelog(body)
        category = "docs"
    elif source_name == "Cohere Pricing":
        headline, summary, target_url, article_identity = extract_cohere_pricing(body)
        category = "docs"
    elif source_name == "Groq Docs":
        headline, summary, target_url, article_identity = extract_groq_docs(body)
        category = "docs"
    elif source_name == "Replicate Changelog":
        headline, summary, target_url, article_identity = extract_replicate_changelog(body)
        category = "changelog"
    elif source_name == "DeepSeek Home":
        headline, summary, target_url, article_identity = extract_deepseek_home(body)
        category = "news"
    elif source_name == "Qwen Blog":
        headline, summary, target_url, article_identity = extract_qwen_blog(body)
        category = "blog"
    elif source_name == "Mistral Changelog":
        headline, summary, target_url, article_identity = extract_mistral_changelog(body)
        category = "docs"
    elif source_name == "Anthropic News":
        headline, summary, target_url = extract_anthropic_item(body)
        category = "news"
    elif source_name == "MiniMax Docs Models":
        headline, summary, target_url = extract_minimax_item(body)
        category = "docs"
    elif source_name == "Google DeepMind Blog":
        headline, summary, target_url = extract_google_deepmind_item(body)
        category = "blog"
    elif source_name == "Google AI Blog":
        headline, summary, target_url = extract_google_ai_item(body)
        category = "blog"
    else:
        headline, summary, target_url, article_identity = extract_generic(source_name, body, event.get("url", ""))
        category = "update"

    timestamp = event.get("timestamp", utc_now())
    article_identity = sanitize_item_identity(article_identity, source_name, headline)
    return {
        "timestamp": timestamp,
        "source_name": source_name,
        "source_url": event.get("url", ""),
        "effective_url": event.get("effective_url"),
        "target_url": normalize_target_url(target_url or event.get("effective_url") or event.get("url", ""), event.get("url", "")),
        "category": category,
        "headline_en": clean_text(headline),
        "summary_en": clean_text(summary),
        "headline_zh": "",
        "summary_zh": "",
        "evidence_excerpt": clip(body, 800),
        "article_identity": article_identity or "",
        "target_title": "",
        "target_summary": "",
        "target_excerpt": "",
        "status": "auto-published-demo",
        "merge_key": "",
        "id": "",
    }


def apply_existing_content(items: list[dict[str, Any]], existing_content: dict[str, dict[str, dict[str, str]]]) -> list[dict[str, Any]]:
    for item in items:
        existing = (
            existing_content["by_merge_key"].get(item.get("merge_key", ""))
            or existing_content["by_id"].get(item["id"])
        )
        if not existing:
            item["needs_cc_refresh"] = True
            continue
        previous_fingerprint = existing.get("content_fingerprint", "")
        current_fingerprint = item.get("content_fingerprint", "")
        # Before target enrichment runs, freshly built items may only have a thin evidence fingerprint.
        # Reuse the previous richer fingerprint in that case so we do not trigger CC on unchanged events.
        if previous_fingerprint and len(token_signature(current_fingerprint)) < 12:
            current_fingerprint = previous_fingerprint
            item["content_fingerprint"] = previous_fingerprint
        has_complete_cached_copy = all(
            [
                (existing.get("headline_en") or "").strip(),
                (existing.get("summary_en") or "").strip(),
            ]
        )
        similarity = fingerprint_similarity(previous_fingerprint, current_fingerprint)
        needs_refresh = not has_complete_cached_copy or similarity < 0.72
        item["headline_en"] = existing.get("headline_en", item["headline_en"]) or item["headline_en"]
        item["summary_en"] = existing.get("summary_en", item["summary_en"]) or item["summary_en"]
        item["headline_zh"] = ""
        item["summary_zh"] = ""
        item["target_title"] = existing.get("target_title", item.get("target_title", ""))
        item["target_summary"] = existing.get("target_summary", item.get("target_summary", ""))
        item["target_excerpt"] = existing.get("target_excerpt", item.get("target_excerpt", ""))
        item["needs_cc_refresh"] = needs_refresh
    return items


def build_archive_items(events: list[dict[str, Any]], existing_content: dict[str, dict[str, dict[str, str]]]) -> list[dict[str, Any]]:
    items = [build_item(event) for event in events]
    items.sort(key=lambda item: item["timestamp"], reverse=True)
    items = dedupe_archive_items(items)
    items = apply_existing_content(items, existing_content)
    return items


def normalize_key_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


STOPWORDS = {
    "a", "an", "and", "announces", "article", "articles", "blog", "blogs", "building", "community",
    "data", "detected", "face", "fast", "for", "guide", "highlights", "hugging", "in", "introduces",
    "launches", "model", "new", "news", "ocr", "on", "openai", "post", "synthetic", "the", "update",
    "updated", "with",
}


def token_signature(text: str) -> set[str]:
    tokens = [token for token in normalize_key_text(text).split() if len(token) > 2 and token not in STOPWORDS]
    return set(tokens)


def token_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def fingerprint_similarity(left: str, right: str) -> float:
    return token_similarity(token_signature(left), token_signature(right))


def content_signature(item: dict[str, Any]) -> str:
    candidates = [
        item.get("target_title", ""),
        item.get("target_summary", ""),
        item.get("target_excerpt", ""),
        item.get("headline_en", ""),
        item.get("summary_en", ""),
        item.get("evidence_excerpt", ""),
    ]
    merged = " ".join(normalize_key_text(value) for value in candidates if value)
    source_name = normalize_key_text(item.get("source_name", ""))
    if source_name:
        merged = re.sub(rf"\b{re.escape(source_name)}\b", " ", merged)
    merged = re.sub(r"\b(blog|news|update|detected|highlights|launches|announces|article|new)\b", " ", merged)
    merged = re.sub(r"\s+", " ", merged).strip()
    return merged[:180]


def archive_dedupe_key(item: dict[str, Any]) -> tuple[str, str, str]:
    source_name = item.get("source_name", "")
    category = item.get("category", "")
    target_url = (item.get("target_url") or "").strip().rstrip("/")
    source_url = (item.get("source_url") or "").strip().rstrip("/")
    headline = normalize_key_text(item.get("headline_en", ""))
    signature = content_signature(item)
    article_identity = item.get("article_identity", "") or ""

    if category == "release" and target_url:
        return ("release", source_name.lower(), target_url.lower())

    # For the 3 homepage-list sources, use article_identity as the primary
    # dedupe signal so the same article gets the same key regardless of
    # minor homepage reshuffles.
    NEWSROOM_FAMILY = {
        "OpenAI Newsroom",
        "Hugging Face Blog",
        "AWS Machine Learning Blog",
    }
    if source_name in NEWSROOM_FAMILY and article_identity:
        return (category, source_name.lower(), article_identity.lower())

    # If the target URL is just a generic homepage/source page, dedupe by source + headline.
    if not target_url or target_url.lower() == source_url.lower():
        return (category, source_name.lower(), signature or headline)

    generic_suffixes = (
        "/blog",
        "/blog/",
        "/news",
        "/news/",
        "/newsroom",
        "/newsroom/",
        "/research",
        "/research/",
        "/research/index",
        "/research/index/",
        "/hub/blog",
        "/hub/blog/",
        "/machine-learning",
        "/machine-learning/",
    )
    lower_target = target_url.lower()
    if any(lower_target.endswith(suffix) for suffix in generic_suffixes):
        return (category, source_name.lower(), signature or headline)

    return (category, source_name.lower(), lower_target)


def merge_key_for_item(item: dict[str, Any]) -> str:
    return "||".join(archive_dedupe_key(item))


def fingerprint_for_item(item: dict[str, Any]) -> str:
    candidates = [
        item.get("target_title", ""),
        item.get("target_summary", ""),
        item.get("target_excerpt", ""),
        item.get("evidence_excerpt", ""),
        item.get("headline_en", ""),
        item.get("summary_en", ""),
    ]
    fingerprint = " ".join(normalize_key_text(value) for value in candidates if value)
    fingerprint = re.sub(r"\s+", " ", fingerprint).strip()
    return fingerprint[:600]


def are_similar_generic_items(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_headline = token_signature(left.get("headline_en", ""))
    right_headline = token_signature(right.get("headline_en", ""))
    left_content = token_signature(content_signature(left))
    right_content = token_signature(content_signature(right))

    headline_score = token_similarity(left_headline, right_headline)
    content_score = token_similarity(left_content, right_content)

    if headline_score >= 0.72:
        return True
    if content_score >= 0.82:
        return True
    if headline_score >= 0.5 and content_score >= 0.55:
        return True
    return False


def dedupe_archive_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        key = archive_dedupe_key(item)
        if key in seen:
            continue
        target_url = (item.get("target_url") or "").strip().rstrip("/").lower()
        source_url = (item.get("source_url") or "").strip().rstrip("/").lower()
        if target_url == source_url or any(
            target_url.endswith(suffix)
            for suffix in (
                "/blog",
                "/blog/",
                "/news",
                "/news/",
                "/newsroom",
                "/newsroom/",
                "/research",
                "/research/",
                "/research/index",
                "/research/index/",
                "/hub/blog",
                "/hub/blog/",
                "/machine-learning",
                "/machine-learning/",
            )
        ):
            if any(
                existing.get("source_name") == item.get("source_name")
                and existing.get("category") == item.get("category")
                and are_similar_generic_items(existing, item)
                for existing in deduped
            ):
                continue
        seen.add(key)
        item["merge_key"] = merge_key_for_item(item)
        item["id"] = stable_id_for_merge_key(item["merge_key"])
        item["content_fingerprint"] = fingerprint_for_item(item)
        item["needs_cc_refresh"] = True
        deduped.append(item)
    return deduped


def pick_top_items(events: list[dict[str, Any]], limit: int, existing_content: dict[str, dict[str, dict[str, str]]]) -> list[dict[str, Any]]:
    latest_by_source: dict[str, dict[str, Any]] = {}
    for event in reversed(events):
        source_name = event.get("source_name", "Unknown source")
        if source_name not in latest_by_source:
            latest_by_source[source_name] = event

    items = [build_item(latest_by_source[source]) for source in PRIORITY_SOURCES if source in latest_by_source]
    for source_name, event in latest_by_source.items():
        if source_name not in PRIORITY_SOURCES:
            items.append(build_item(event))

    items.sort(key=lambda item: item["timestamp"], reverse=True)
    items = items[:limit]
    for item in items:
        item["merge_key"] = merge_key_for_item(item)
        item["id"] = stable_id_for_merge_key(item["merge_key"])
    return apply_existing_content(items, existing_content)


def build_homepage_items(
    archive_items: list[dict[str, Any]],
    limit: int,
    previous_generated_at: dt.datetime | None = None,
    previous_latest_items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    qualified = [
        item
        for item in sorted(archive_items, key=lambda item: item["timestamp"], reverse=True)
        if _homepage_qualifies(item)
    ]
    previous_release_times = _previous_homepage_release_times(previous_latest_items or [])

    recent_batch: list[dict[str, Any]] = []
    if previous_generated_at is not None:
        for item in qualified:
            try:
                item_dt = dt.datetime.fromisoformat(item["timestamp"])
            except Exception:
                continue
            if item_dt > previous_generated_at:
                recent_batch.append(item)

    recent_batch = _select_homepage_candidates(
        recent_batch,
        None,
        previous_release_times,
    )

    if len(recent_batch) > limit:
        return _english_only(recent_batch)

    selected = _select_homepage_candidates(
        qualified,
        limit,
        previous_release_times,
        seed_items=recent_batch,
    )

    return _english_only(selected)


def render_index(items: list[dict[str, Any]], article_mapping: dict[str, str]) -> str:
    cards: list[str] = []
    for item in items:
        # Look up standalone article page via stable keys
        article_slug: str | None = None
        for key_field in ("article_identity", "merge_key", "id"):
            key_val = item.get(key_field, "") or ""
            if key_val and key_val in article_mapping:
                article_slug = article_mapping[key_val]
                break
        article_url = f"./articles/{article_slug}.html" if article_slug else None
        target_url = item.get("target_url") or item.get("source_url") or "#"
        headline_html = (
            f'<a href="{html.escape(article_url)}">{html.escape(item["headline_en"])}</a>'
            if article_url
            else html.escape(item["headline_en"])
        )
        cards.append(
            f"""
            <article class="card" data-category="{html.escape(item["category"])}">
              <div class="card-top">
                <span class="pill">{html.escape(item["category"])}</span>
                <time>{html.escape(format_display_time(item["timestamp"]))}</time>
              </div>
              <h2 class="headline">{headline_html}</h2>
              <p class="summary">{html.escape(item["summary_en"])}</p>
              <div class="card-bottom">
                <span>{html.escape(item["source_name"])}</span>
                <a href="{html.escape(target_url)}">Source</a>
              </div>
            </article>
            """
        )

    updated_at = format_display_time(utc_now())
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Signal Feed</title>
  <style>
    :root {{
      --bg: #f6efe4; --surface: rgba(255,255,255,0.84); --ink: #1a1f18; --muted: #586255;
      --accent: #0f766e; --accent-soft: rgba(15,118,110,0.12); --border: rgba(15,118,110,0.14);
      --shadow: 0 18px 40px rgba(27, 39, 33, 0.09);
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: "Segoe UI", system-ui, sans-serif; color: var(--ink); background: linear-gradient(180deg, #fbf8f2 0%, var(--bg) 100%); }}
    main {{ max-width: 1340px; margin: 0 auto; padding: 40px 20px 64px; }}
    .hero {{ display: grid; gap: 14px; margin-bottom: 24px; }}
    .eyebrow {{ display: inline-flex; width: fit-content; padding: 7px 12px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; font-size: 0.78rem; }}
    h1 {{ margin: 0; font-family: Georgia, "Times New Roman", serif; font-size: clamp(2.1rem, 5vw, 4.6rem); line-height: 0.94; letter-spacing: -0.04em; max-width: 860px; }}
    .sub {{ margin: 0; max-width: 760px; color: var(--muted); font-size: 1.02rem; line-height: 1.55; }}
    .toolbar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 26px; color: var(--muted); }}
    .filters {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 22px; color: var(--muted); }}
    .filters select {{ padding: 10px 12px; border: 1px solid var(--border); border-radius: 12px; background: white; color: var(--ink); }}
    .list {{ display: grid; gap: 18px; }}
    .card {{ display: grid; gap: 14px; padding: 22px 24px; background: var(--surface); border: 1px solid var(--border); border-radius: 18px; box-shadow: var(--shadow); }}
    .card-top, .card-bottom {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; color: var(--muted); font-size: 0.86rem; }}
    .pill {{ display: inline-flex; align-items: center; padding: 6px 10px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); text-transform: uppercase; font-weight: 700; letter-spacing: 0.04em; font-size: 0.74rem; }}
    .headline {{ margin: 0; font-size: 1.7rem; line-height: 1.14; letter-spacing: -0.02em; }}
    .summary {{ margin: 0; color: var(--muted); line-height: 1.7; font-size: 1rem; }}
    a {{ color: var(--accent); text-decoration: none; font-weight: 700; }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <span class="eyebrow">AI Signal Feed</span>
      <h1>10 readable AI updates.</h1>
      <p class="sub">This homepage is built from monitored source changes and shows only English while the homepage quality filter is being stabilized.</p>
    </section>
    <section class="toolbar">
      <div>Last built: {html.escape(updated_at)} - Showing {len(items)} items</div>
    </section>
    <section class="filters">
      <label for="homepage-category">Category</label>
      <select id="homepage-category">
        <option value="">All categories</option>
      </select>
      <span id="homepage-count">{len(items)} items</span>
    </section>
    <p class="sub"><a href="./history.html">Open full history</a></p>
    <section class="list">{''.join(cards)}</section>
  </main>
  <script>
    const homepageCategorySelect = document.getElementById('homepage-category');
    const homepageCountNode = document.getElementById('homepage-count');
    const homepageCards = Array.from(document.querySelectorAll('.card'));
    const homepageCategories = [...new Set(homepageCards.map((card) => card.dataset.category).filter(Boolean))].sort();

    homepageCategories.forEach((category) => {{
      const option = document.createElement('option');
      option.value = category;
      option.textContent = category;
      homepageCategorySelect.appendChild(option);
    }});

    function renderHomepageFilter() {{
      const selected = homepageCategorySelect.value;
      let visible = 0;
      homepageCards.forEach((card) => {{
        const show = !selected || card.dataset.category === selected;
        card.style.display = show ? '' : 'none';
        if (show) visible += 1;
      }});
      homepageCountNode.textContent = `${{visible}} item${{visible === 1 ? '' : 's'}}`;
    }}

    homepageCategorySelect.addEventListener('change', renderHomepageFilter);
    renderHomepageFilter();
  </script>
</body>
</html>
"""


def render_history(items: list[dict[str, Any]], article_mapping: dict[str, str]) -> str:
    updated_at = format_display_time(utc_now())
    payload_json = script_json(items)
    mapping_json = script_json(article_mapping)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Signal Feed History</title>
  <style>
    :root {{
      --bg: #f6efe4; --surface: rgba(255,255,255,0.84); --ink: #1a1f18; --muted: #586255;
      --accent: #0f766e; --accent-soft: rgba(15,118,110,0.12); --border: rgba(15,118,110,0.14);
      --shadow: 0 18px 40px rgba(27, 39, 33, 0.09);
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: "Segoe UI", system-ui, sans-serif; color: var(--ink); background: linear-gradient(180deg, #fbf8f2 0%, var(--bg) 100%); }}
    main {{ max-width: 1340px; margin: 0 auto; padding: 36px 20px 64px; }}
    .toolbar, .filters, .pagination {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-bottom: 18px; }}
    .filters select, .pagination button {{ padding: 10px 12px; border: 1px solid var(--border); border-radius: 12px; background: white; }}
    .pagination button {{ cursor: pointer; }}
    .pagination button[disabled] {{ opacity: 0.45; cursor: default; }}
    .list {{ display: grid; gap: 14px; }}
    .card {{ display: grid; gap: 12px; padding: 20px 22px; background: var(--surface); border: 1px solid var(--border); border-radius: 16px; box-shadow: var(--shadow); }}
    .row {{ display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; color: var(--muted); font-size: 0.9rem; }}
    .pill {{ display: inline-flex; align-items: center; padding: 6px 10px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); text-transform: uppercase; font-weight: 700; font-size: 0.74rem; letter-spacing: 0.04em; }}
    h1, h2, p {{ margin: 0; }}
    p {{ color: var(--muted); line-height: 1.65; }}
    a {{ color: var(--accent); text-decoration: none; font-weight: 700; }}
  </style>
</head>
<body>
  <main>
    <div class="toolbar">
      <h1>AI Signal Feed History</h1>
      <div>Updated: {html.escape(updated_at)}</div>
      <div><a href="./index.html">Back to homepage</a></div>
    </div>
    <div class="filters">
      <label>Category <select id="category-filter"><option value="">All categories</option></select></label>
      <label>Source <select id="source-filter"><option value="">All sources</option></select></label>
    </div>
    <div class="pagination">
      <button id="prev-page" type="button">Previous</button>
      <span id="page-state"></span>
      <button id="next-page" type="button">Next</button>
    </div>
    <section id="history-list" class="list"></section>
  </main>
  <script id="archive-data" type="application/json">{payload_json}</script>
  <script id="article-mapping" type="application/json">{mapping_json}</script>
  <script>
    const archive = JSON.parse(document.getElementById('archive-data').textContent);
    const articleMapping = JSON.parse(document.getElementById('article-mapping').textContent);
    const perPage = 50;
    let page = 1;
    let categoryFilter = '';
    let sourceFilter = '';
    const listNode = document.getElementById('history-list');
    const pageState = document.getElementById('page-state');
    const prevButton = document.getElementById('prev-page');
    const nextButton = document.getElementById('next-page');
    const categorySelect = document.getElementById('category-filter');
    const sourceSelect = document.getElementById('source-filter');

    function formatTime(value) {{
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return new Intl.DateTimeFormat('en-US', {{
        timeZone: 'Asia/Taipei',
        year: 'numeric',
        month: 'short',
        day: '2-digit',
        hour: 'numeric',
        minute: '2-digit',
        hour12: true
      }}).format(date) + ' Taipei';
    }}

    function uniqueValues(key) {{
      return [...new Set(archive.map((item) => item[key]).filter(Boolean))].sort();
    }}
    function fillSelect(select, values) {{
      values.forEach((value) => {{
        const option = document.createElement('option');
        option.value = value;
        option.textContent = value;
        select.appendChild(option);
      }});
    }}
    fillSelect(categorySelect, uniqueValues('category'));
    fillSelect(sourceSelect, uniqueValues('source_name'));

    function filteredItems() {{
      return archive.filter((item) => {{
        if (categoryFilter && item.category !== categoryFilter) return false;
        if (sourceFilter && item.source_name !== sourceFilter) return false;
        return true;
      }});
    }}

    function render() {{
      const items = filteredItems();
      const totalPages = Math.max(1, Math.ceil(items.length / perPage));
      page = Math.min(page, totalPages);
      const slice = items.slice((page - 1) * perPage, page * perPage);
      listNode.innerHTML = '';
      slice.forEach((item) => {{
        // Look up standalone article page via stable keys
        let articleSlug = null;
        for (const key of ['article_identity', 'merge_key', 'id']) {{
          const k = item[key];
          if (k && articleMapping[k]) {{
            articleSlug = articleMapping[k];
            break;
          }}
        }}
        const articleUrl = articleSlug ? './articles/' + articleSlug + '.html' : null;
        const headline = articleUrl
          ? '<a href="' + articleUrl + '">' + item.headline_en + '</a>'
          : item.headline_en;
        const article = document.createElement('article');
        article.className = 'card';
        article.innerHTML = `
          <div class="row"><span class="pill">${{item.category}}</span><time>${{formatTime(item.timestamp)}}</time></div>
          <h2>${{headline}}</h2>
          <p>${{item.summary_en}}</p>
          <div class="row"><span>${{item.source_name}}</span><a href="${{item.target_url || item.source_url || '#'}}">Source</a></div>
        `;
        listNode.appendChild(article);
      }});
      pageState.textContent = `Page ${{page}} / ${{totalPages}} - ${{items.length}} items`;
      prevButton.disabled = page <= 1;
      nextButton.disabled = page >= totalPages;
    }}

    categorySelect.addEventListener('change', (event) => {{ categoryFilter = event.target.value; page = 1; render(); }});
    sourceSelect.addEventListener('change', (event) => {{ sourceFilter = event.target.value; page = 1; render(); }});
    prevButton.addEventListener('click', () => {{ page = Math.max(1, page - 1); render(); }});
    nextButton.addEventListener('click', () => {{ page = page + 1; render(); }});
    render();
  </script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate homepage and history pages from monitored AI events.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum number of homepage cards.")
    args = parser.parse_args()

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    previous_latest_generated_at = read_previous_latest_generated_at()
    previous_latest_items = read_previous_latest_items()
    existing_content = read_existing_content()
    events = read_events()
    archive_items = build_archive_items(events, existing_content)
    archive_items = _english_only(archive_items)
    latest_items = build_homepage_items(archive_items, args.limit, previous_latest_generated_at, previous_latest_items)

    ARTICLE_MAPPING_FILE = ARTICLES_DIR / "article-mapping.json"
    article_mapping: dict[str, str] = {}
    if ARTICLE_MAPPING_FILE.exists():
        try:
            article_mapping = json.loads(ARTICLE_MAPPING_FILE.read_text(encoding="utf-8"))
        except Exception:
            article_mapping = {}

    (DATA_DIR / "latest.json").write_text(json.dumps({"generated_at": utc_now(), "item_count": len(latest_items), "items": latest_items}, ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA_DIR / "archive.json").write_text(json.dumps({"generated_at": utc_now(), "item_count": len(archive_items), "items": archive_items}, ensure_ascii=False, indent=2), encoding="utf-8")
    (SITE_DIR / "index.html").write_text(render_index(latest_items, article_mapping), encoding="utf-8")
    (SITE_DIR / "history.html").write_text(render_history(archive_items, article_mapping), encoding="utf-8")
    print(f"Published {len(latest_items)} homepage item(s) and {len(archive_items)} archive item(s) to {SITE_DIR}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
