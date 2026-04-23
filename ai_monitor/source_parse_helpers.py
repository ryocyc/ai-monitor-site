"""
Source-specific parsing helpers for AI news & changelog pages.
Each function takes a diff_excerpt (list[str]) and returns (headline, summary, target_url, article_identity).
The article_identity is a stable slug/title used for dedupe, separate from target_url.
"""

from __future__ import annotations

import re
from typing import Any


# -------------------------------------------------------------------
# Shared utilities
# -------------------------------------------------------------------

def clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clip(text: str, limit: int) -> str:
    text = clean_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def first_match(text: str, patterns: list[str], group: int = 1) -> str | None:
    for p in patterns:
        try:
            m = re.search(p, text, flags=re.IGNORECASE | re.MULTILINE)
            if not m:
                continue
            if m.lastindex is None or m.lastindex < group:
                continue
            val = clean_text(m.group(group))
            if val and len(val) >= 3:
                return val
        except (IndexError, re.error):
            continue
    return None


def extract_all_urls(text: str) -> list[str]:
    """Extract all http/https URLs from text."""
    return re.findall(r"https?://[^\s)>\"]+", text)


def pick_best_url(text: str, source_url: str | None = None) -> str | None:
    candidates = extract_all_urls(text)
    if not candidates:
        return None

    blocked_tokens = (
        "ctfassets.net",
        "framerusercontent.com",
        "images.ctfassets.net",
        "github.com/login",
        "github.com/sponsors",
        "api.openai.com",
        "platform.openai.com/api",
        "x.ai/api",
        "x.ai/grok",
    )
    blocked_suffixes = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico")

    scored: list[tuple[int, str]] = []
    for url in candidates:
        url_clean = url.rstrip(".,")
        lower = url_clean.lower()
        if any(t in lower for t in blocked_tokens):
            continue
        if any(lower.endswith(s) for s in blocked_suffixes):
            continue
        score = 0
        depth = lower.count("/")
        score += depth * 4
        if source_url and lower.rstrip("/") == source_url.lower().rstrip("/"):
            score -= 60
        for good in (
            "/blog/", "/news/", "/newsroom/", "/announce/", "/post/",
            "/changelog", "/releases/tag/", "/docs/", "/research/",
            "/index", "/article", "/update", "/permalink"
        ):
            if good in lower:
                score += 18
        for bad in (
            "/careers", "/help", "/privacy", "/terms", "/login",
            "/signup", "/signin", "/register", "/community"
        ):
            if bad in lower:
                score -= 25
        scored.append((score, url_clean))

    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def title_to_slug(title: str) -> str:
    """Convert a title to a URL slug."""
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9\s\-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug


# -------------------------------------------------------------------
# OpenAI Newsroom
# -------------------------------------------------------------------

def parse_openai_newsroom(text: str) -> dict[str, Any]:
    """
    Parse OpenAI Newsroom diff content.

    The diff lines contain markdown-formatted article links like:
    [Article Title Company Month Day, Year](https://openai.com/index/slug/)

    Returns dict with headline, target_url, and article_identity.
    """
    combined = " ".join(text) if isinstance(text, list) else text

    # Extract all openai.com/index/ URLs with their associated titles
    # Pattern: [Title text](https://openai.com/index/slug/)
    article_blocks: list[dict] = []
    pattern = r'\[([^\]]+)\]\((https://openai\.com/index/[^)\s]+)\)'

    for m in re.finditer(pattern, combined, re.IGNORECASE):
        raw_title = m.group(1).strip()
        article_url = m.group(2).strip().rstrip("/")

        # raw_title looks like: "Scaling Codex to enterprises worldwide Company Apr 21, 2026"
        # Extract just the article title (before the date/company pattern)
        title_match = re.match(r"^([A-Z][A-Za-z0-9\s\-:,']+(?:\s+(?:Company|Product|Engineering|Research))?)", raw_title)
        if title_match:
            headline = title_match.group(1).strip()
        else:
            # Fallback: use raw title up to first date-like pattern
            headline = re.sub(r"\s+[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}\s*$", "", raw_title).strip()
            if not headline or len(headline) < 5:
                headline = raw_title

        slug = article_url.rstrip("/").split("/")[-1]
        article_blocks.append({
            "headline": headline,
            "target_url": article_url,
            "slug": slug,
            "article_identity": slug,  # stable identity = slug
        })

    if not article_blocks:
        # Fallback: look for any openai.com/index/ URL
        urls = re.findall(r"(https://openai\.com/index/[^)\s]+)", combined)
        if urls:
            slug = urls[0].rstrip("/").split("/")[-1]
            article_blocks.append({
                "headline": slug.replace("-", " ").title(),
                "target_url": urls[0].rstrip("/"),
                "slug": slug,
                "article_identity": slug,
            })

    if not article_blocks:
        return {
            "headline": "OpenAI newsroom update detected",
            "target_url": "https://openai.com/newsroom/",
            "article_identity": None,
        }

    # Return the first (topmost/most-prominent) article
    return article_blocks[0]


def extract_openai_newsroom(text: str) -> tuple[str, str, str | None, str | None]:
    result = parse_openai_newsroom(text)
    headline = result["headline"]
    target_url = result["target_url"]
    article_identity = result["article_identity"]
    summary = f"OpenAI newsroom changed. Most visible article: \"{clip(headline, 90)}\"."
    return headline, clip(summary, 250), target_url, article_identity


# -------------------------------------------------------------------
# Hugging Face Blog
# -------------------------------------------------------------------

def parse_huggingface_blog(text: str) -> dict[str, Any]:
    """
    Parse Hugging Face blog diff content.

    Articles appear as lines like:
    "Building a Fast Multilingual OCR Model with Synthetic Data 5 days ago • 27"

    Strategy: find all "N days ago •" positions. Before each, extract the
    longest run of words that are:
      - >= 3 chars (filters out "to", "a", "in")
      - and start with uppercase (filters out lowercase menu items)
    Score by run length; pick the longest.
    """
    combined = " ".join(text) if isinstance(text, list) else text

    day_matches = list(re.finditer(r"(\d+)\s+days?\s+ago\s+•", combined))
    if not day_matches:
        day_matches = list(re.finditer(r"[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}\s+•", combined))

    if not day_matches:
        return {
            "headline": "Hugging Face blog update detected",
            "target_url": "https://huggingface.co/blog",
            "article_identity": None,
        }

    best_run: list[str] = []
    skip_words = {"a", "an", "the", "to", "in", "of", "for", "with", "and", "or", "as", "by", "at"}
    for m in day_matches:
        window = combined[max(0, m.start() - 150):m.start()]
        words = window.split()

        # Walk backwards, collecting words that are either:
        # - >= 3 chars AND start with uppercase (real title words)
        # - short skip words (a, an, the, etc.) — skip only these, don't break run
        run: list[str] = []
        for w in reversed(words):
            w_lower = w.lower()
            if w_lower in skip_words:
                continue  # skip but don't break the run
            if len(w) >= 3 and w[0].isupper():
                run.insert(0, w)
            else:
                if len(run) >= 3:
                    break
                run = []

        if len(run) > len(best_run):
            best_run = run

    if len(best_run) < 3:
        return {
            "headline": "Hugging Face blog update detected",
            "target_url": "https://huggingface.co/blog",
            "article_identity": None,
        }

    title = " ".join(best_run)
    slug = title_to_slug(title)
    return {
        "headline": title,
        "target_url": f"https://huggingface.co/blog/{slug}",
        "slug": slug,
        "article_identity": slug,
    }


def extract_huggingface_blog(text: str) -> tuple[str, str, str | None, str | None]:
    result = parse_huggingface_blog(text)
    headline = result["headline"]
    target_url = result["target_url"]
    article_identity = result["article_identity"]
    summary = f"Hugging Face blog updated. Recent post: \"{clip(headline, 90)}\"."
    return headline, clip(summary, 250), target_url, article_identity


# -------------------------------------------------------------------
# AWS Machine Learning Blog
# -------------------------------------------------------------------

def parse_aws_ml_blog(text: str) -> dict[str, Any]:
    """
    Parse AWS ML Blog diff content.

    The diff contains article content with title and a "Permalink" reference.
    Pattern: "Article Title by Author Name on DATE in Categories Permalink"

    Returns dict with headline, target_url (from Permalink), and article_identity.
    """
    combined = " ".join(text) if isinstance(text, list) else text

    # Look for article title patterns - usually a line starting with a capital letter
    # followed by "by" (author), "on" (date), "in" (categories), "Permalink"
    title_pattern = r"^([A-Z][A-Za-z0-9][A-Za-z0-9\s\-:,']{20,150})\s+by\s+\w+"

    title_match = re.search(title_pattern, combined, re.MULTILINE)
    if title_match:
        raw_headline = title_match.group(1).strip()
    else:
        # Try to find article title by looking for the "Permalink" section
        permalink_pattern = r"<a[^>]*href=\"(https://aws\.amazon\.com/blogs/machine-learning/[^\"]+)\""
        perm_match = re.search(permalink_pattern, combined)
        if perm_match:
            article_url = perm_match.group(1)
            slug = article_url.rstrip("/").split("/")[-1]
            return {
                "headline": slug.replace("-", " ").title(),
                "target_url": article_url,
                "slug": slug,
                "article_identity": slug,
            }

        return {
            "headline": "AWS Machine Learning Blog update detected",
            "target_url": "https://aws.amazon.com/blogs/machine-learning/",
            "article_identity": None,
        }

    # Clean the headline
    headline = raw_headline.strip()
    # Remove any trailing category info
    headline = re.sub(r"\s+in\s+[A-Za-z\s&,]+$", "", headline).strip()

    # Try to extract AWS blog slug from the line
    slug_match = re.search(r"(?:https://aws\.amazon\.com/blogs/machine-learning/([^)\s]+)|/blogs/machine-learning/([^)\s]+))", combined)
    if slug_match:
        slug = slug_match.group(1) or slug_match.group(2)
        slug = slug.rstrip("/")
    else:
        slug = title_to_slug(headline)

    target_url = f"https://aws.amazon.com/blogs/machine-learning/{slug}/"

    return {
        "headline": headline,
        "target_url": target_url,
        "slug": slug,
        "article_identity": slug,
    }


def extract_aws_ml_blog(text: str) -> tuple[str, str, str | None, str | None]:
    result = parse_aws_ml_blog(text)
    headline = result["headline"]
    target_url = result["target_url"]
    article_identity = result["article_identity"]
    summary = f"AWS Machine Learning Blog changed. Recent post: \"{clip(headline, 90)}\"."
    return headline, clip(summary, 250), target_url, article_identity


# -------------------------------------------------------------------
# xAI Blog
# -------------------------------------------------------------------

def extract_xai_blog(text: str) -> tuple[str, str, str | None, str | None]:
    """
    Parse xAI Blog via Jina-rendered content.

    The Jina rendering of x.ai/blog produces structured Markdown with
    article entries in the form:
      Date
      ### [Article Title](http://x.ai/news/slug)

    We extract the first (most recent) such entry for a real sub-page URL.
    """
    combined = " ".join(text) if isinstance(text, list) else text

    # Find all [Title](http://x.ai/news/slug) Markdown links
    top_title: str | None = None
    top_url: str | None = None
    top_slug: str | None = None

    for m in re.finditer(r"\[([^\]]+)\]\((http://x\.ai/news/([^)\s]+))\)", combined):
        if top_title is None:
            top_title = m.group(1).strip()
            top_url = m.group(2).strip()
            top_slug = m.group(3).strip()

    if not top_title:
        # Fallback: look for any x.ai/news/ URL
        fallback_urls = re.findall(r"(http://x\.ai/news/[^\s>\"')]+)", combined)
        if fallback_urls:
            slug = fallback_urls[0].rstrip("/").split("/")[-1]
            headline = slug.replace("-", " ").title()
            summary = "xAI blog updated. Recent post: check the target URL."
            return headline, clip(summary, 250), fallback_urls[0].rstrip(), slug

        headline = first_match(combined, [
            r"<title>\s*([^<]{10,100})\s*</title>",
            r"<h[1-3][^>]*>\s*([^<]{10,100})\s*</h[1-3]>",
        ])
        if not headline:
            headline = "xAI blog update detected"
        summary = f"xAI blog updated. \"{clip(headline, 90)}\"."
        return headline, clip(summary, 250), "https://x.ai/blog", None

    summary = f"xAI blog updated. Recent post: \"{clip(top_title, 90)}\"."
    return top_title, clip(summary, 250), top_url, top_slug


# -------------------------------------------------------------------
# OpenAI API Changelog
# -------------------------------------------------------------------

def extract_openai_changelog(text: str) -> tuple[str, str, str | None, str | None]:
    combined = " ".join(text) if isinstance(text, list) else text

    # The Jina-rendered page contains a changelog table near the bottom with
    # entries in the form: "### Month, Year ... Feature gpt-image-2 ..."
    # Find the FIRST such date header (most recent entry).
    month_year = first_match(combined, [
        r"###\s+((?:January|February|March|April|May|June|July|August|September|October|November|December)[,\s]+\d{4})",
    ])

    # Try to get the feature/change mentioned on that line
    if month_year:
        # Find the line containing this month/year and extract a feature name
        # The entry typically looks like: "### April, 2026 Apr 21 Feature gpt-image-2 v1/images/..."
        month_idx = combined.find(month_year)
        if month_idx >= 0:
            line_context = combined[month_idx:month_idx+200]
            feature_match = re.search(r"Feature\s+([a-z0-9_\-.\-]+)", line_context, re.IGNORECASE)
            feature_name = feature_match.group(1) if feature_match else None
        else:
            feature_name = None

        if feature_name:
            headline = f"OpenAI API changelog: {feature_name} ({month_year})"
            slug = title_to_slug(feature_name)
        else:
            headline = f"OpenAI API changelog update ({month_year})"
            slug = title_to_slug(month_year)
    else:
        headline = "OpenAI API changelog updated"
        slug = "openai-changelog-update"

    specific_url = first_match(combined, [
        r"(https://platform\.openai\.com/docs/[^\s>\"']+)",
    ], group=1)
    if specific_url:
        specific_url = specific_url.rstrip(").,")

    target_url = specific_url if specific_url else "https://platform.openai.com/docs/changelog"

    summary = f"OpenAI's API changelog page changed. Latest entry: \"{clip(headline, 100)}\"."
    return clip(headline, 120), clip(summary, 250), target_url, slug


# -------------------------------------------------------------------
# Cohere Changelog
# -------------------------------------------------------------------

def extract_cohere_changelog(text: str) -> tuple[str, str, str | None, str | None]:
    combined = " ".join(text) if isinstance(text, list) else text

    date_match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+([0-9]{1,2}),?\s+([0-9]{4})\b",
        combined, re.IGNORECASE
    )
    date_str = f"{date_match.group(1)} {date_match.group(2)}, {date_match.group(3)}" if date_match else None

    # Look for changelog entry titles: h2/h3 headers or sidebar nav links
    section_name = first_match(combined, [
        r"<h[2-3][^>]*>\s*([^<]{10,100})\s*</h[2-3]>",
        r"<a[^>]+href=\"/changelog/[^>]+>\s*([^<]{10,100})\s*</a>",
        r"aria-current=\"page\"[^>]*>\s*([^<]{10,60})\s*</a>",
    ])

    if date_str and section_name:
        headline = f"Cohere changelog: {section_name} ({date_str})"
    elif date_str:
        headline = f"Cohere changelog update ({date_str})"
    elif section_name:
        headline = f"Cohere changelog: {section_name}"
    else:
        headline = "Cohere changelog update detected"

    target_url = pick_best_url(combined, "https://docs.cohere.com/changelog") or "https://docs.cohere.com/changelog"
    identity = f"cohere-changelog-{title_to_slug(section_name or date_str or 'update')}" if (section_name or date_str) else None

    summary = f"Cohere's changelog page updated. Latest entry: \"{clip(section_name or headline, 80)}\"."
    return clip(headline, 120), clip(summary, 250), target_url, identity


# -------------------------------------------------------------------
# Cohere Pricing
# -------------------------------------------------------------------

def extract_cohere_pricing(text: str) -> tuple[str, str, str | None, str | None]:
    combined = " ".join(text) if isinstance(text, list) else text

    # Try to extract specific pricing plan names or section headers
    plan_name = first_match(combined, [
        r"<h[1-4][^>]*>\s*(?:Plan| Pricing| Tier|Model)[^<]*:\s*([^<]{5,60})\s*</h[1-4]>",
        r"<h[1-4][^>]*>\s*([^<]{5,60})\s*(?:Plan| Pricing| Tier|Model)\s*</h[1-4]>",
        r'"(Performance|Enterprise|Developer|Base|Scale|Advanced|Team|Pro|Command|Rerank)"',
    ])
    if not plan_name:
        plan_match = re.search(r"\b(Performance|Enterprise|Developer|Base|Scale|Advanced|Team|Pro|Command|Rerank)\b", combined)
        plan_name = plan_match.group(1) if plan_match else None

    if plan_name:
        headline = f"Cohere pricing update: {plan_name} plan"
    else:
        headline = "Cohere pricing page update detected"

    target_url = pick_best_url(combined, "https://cohere.com/pricing") or "https://cohere.com/pricing"
    identity = f"cohere-pricing-{plan_name.lower()}" if plan_name else "cohere-pricing"

    summary = f"Cohere's pricing page changed. Review the page for current {plan_name or 'plan'} details."
    return clip(headline, 100), clip(summary, 250), target_url, identity


# -------------------------------------------------------------------
# Groq Docs
# -------------------------------------------------------------------

def extract_groq_docs(text: str) -> tuple[str, str, str | None, str | None]:
    combined = " ".join(text) if isinstance(text, list) else text

    # Pattern: "Page Title - GroqDocs" or similar
    page_title_match = first_match(combined, [
        r"^([A-Z][A-Za-z0-9][A-Za-z0-9\s\-]{3,60})\s*[-–]\s*GroqDocs",
        r"<title>\s*([^<]{10,80})\s*[-–]\s*GroqDocs",
    ])
    if not page_title_match:
        page_title_match = re.search(r"^([A-Z][A-Za-z0-9][A-Za-z0-9\s\-]{3,60})\s*[-–]\s*GroqDocs", combined, re.MULTILINE)
        page_title_match = page_title_match.group(1).strip() if page_title_match else None

    # Try to find the specific page path from the URL in content
    page_slug = first_match(combined, [r"https://docs\.groq\.com/([^/\s\">']+)"])

    # If we have a page slug, use it for a more specific identity
    if page_slug:
        section = page_slug.replace("-", " ").replace("/", " ").title()
        headline = f"Groq docs: {section}"
        identity = f"groq-docs-{page_slug.rstrip('/')}"
        target_url = f"https://docs.groq.com/{page_slug.rstrip('/')}"
    elif page_title_match:
        headline = f"Groq docs updated: {page_title_match}"
        identity = f"groq-docs-{title_to_slug(page_title_match)}"
        target_url = "https://docs.groq.com/"
    else:
        headline = "Groq documentation update detected"
        identity = "groq-docs-update"
        target_url = "https://docs.groq.com/"

    summary = "Groq docs changed. Check the target URL for new or updated documentation content."
    return clip(headline, 120), clip(summary, 250), target_url, identity


# -------------------------------------------------------------------
# Replicate Changelog
# -------------------------------------------------------------------

def extract_replicate_changelog(text: str) -> tuple[str, str, str | None, str | None]:
    combined = " ".join(text) if isinstance(text, list) else text

    version = first_match(combined, [r"#\s*([0-9]+\.[0-9.]+)", r"version\s+([0-9]+\.[0-9.]+)"])
    headline_raw = first_match(combined, [
        r'href="/changelog/([^"]+)"[^>]*>\s*([^<]{10,150})\s*</a>',
        r"<h[2-4][^>]*>\s*([^<]{10,150})\s*</h[2-4]>",
    ])
    if version and headline_raw:
        headline = f"Replicate changelog v{version}: {headline_raw}"
    elif headline_raw:
        headline = f"Replicate changelog: {headline_raw}"
    else:
        headline = "Replicate changelog update detected"

    article_url = first_match(combined, [
        r"href=\"(https://replicate\.com/changelog/[^)\"\s]+)\"",
        r"href=\"(/changelog/[^)\"\s]+)\"",
    ], group=1)
    if article_url and not article_url.startswith("http"):
        article_url = "https://replicate.com" + article_url

    slug = article_url.rstrip("/").split("/")[-1] if article_url else None
    target_url = article_url or "https://replicate.com/changelog"

    identity = f"replicate-{slug or 'update'}"
    summary = f"Replicate changelog updated. Latest entry: \"{clip(headline_raw or headline, 100)}\"."
    return clip(headline, 120), clip(summary, 250), target_url, identity


# -------------------------------------------------------------------
# DeepSeek Home
# -------------------------------------------------------------------

def extract_deepseek_home(text: str) -> tuple[str, str, str | None, str | None]:
    combined = " ".join(text) if isinstance(text, list) else text

    model = first_match(combined, [r"(DeepSeek-[A-Z][0-9]+)"])
    headline = first_match(combined, [
        r"<h[1-3][^>]*>\s*([^<]{5,150})\s*</h[1-3]>",
        r"#\s*([^\n]{10,120})",
    ])

    if model:
        headline_str = f"DeepSeek update: {model}"
    elif headline:
        headline_str = f"DeepSeek homepage update"
    else:
        headline_str = "DeepSeek homepage update detected"

    target_url = pick_best_url(combined, "https://www.deepseek.com/") or "https://www.deepseek.com/"
    slug = title_to_slug(model or headline or "update")
    identity = f"deepseek-{slug}"

    summary = "DeepSeek homepage content changed. Check the page for latest model or API announcements."
    return clip(headline_str, 100), clip(summary, 250), target_url, identity


# -------------------------------------------------------------------
# Qwen Blog
# -------------------------------------------------------------------

def extract_qwen_blog(text: str) -> tuple[str, str, str | None, str | None]:
    combined = " ".join(text) if isinstance(text, list) else text

    headline = first_match(combined, [
        r"<h[1-3][^>]*>\s*([^<]{10,150})\s*</h[1-3]>",
        r"<title>\s*([^<]{10,120})\s*</title>",
        r"#\s*([^\n]{10,120})",
    ])

    if not headline:
        headline = first_match(combined, [r"Qwen\s+([0-9]+\.[0-9.]+)"])

    if not headline:
        headline = "Qwen blog update detected"

    target_url = pick_best_url(combined, "https://qwenlm.github.io/") or "https://qwenlm.github.io/"
    slug = title_to_slug(headline)
    identity = f"qwen-{slug}"

    summary = f"Qwen blog updated. Recent post: \"{clip(headline, 90)}\"."
    return headline, clip(summary, 250), target_url, identity


# -------------------------------------------------------------------
# Mistral Changelog
# -------------------------------------------------------------------

def extract_mistral_changelog(text: str) -> tuple[str, str, str | None, str | None]:
    combined = " ".join(text) if isinstance(text, list) else text

    version = first_match(combined, [
        r"(?:version|v)\s*([0-9]+\.[0-9.]+)",
        r'"version"\s*:\s*"([^"]+)"',
    ])
    item_headline = first_match(combined, [
        r"(?:##|###)\s+([A-Z][A-Za-z][^.^\n]{10,100})",
        r"<h[2-4][^>]*>\s*([^<]{10,100})\s*</h[2-4]>",
    ])

    if version and item_headline:
        headline = f"Mistral changelog v{version}: {item_headline}"
    elif version:
        headline = f"Mistral changelog v{version}"
    elif item_headline:
        headline = f"Mistral changelog: {item_headline}"
    else:
        headline = "Mistral changelog update detected"

    target_url = pick_best_url(combined, "https://docs.mistral.ai/getting-started/changelog/") or "https://docs.mistral.ai/getting-started/changelog/"
    slug = f"mistral-{version or title_to_slug(item_headline or 'update')}"
    identity = slug

    summary = f"Mistral's changelog updated. Latest: \"{clip(item_headline or headline, 100)}\"."
    return clip(headline, 120), clip(summary, 250), target_url, identity


# -------------------------------------------------------------------
# Generic fallback
# -------------------------------------------------------------------

def extract_generic(text: str, source_name: str, source_url: str | None = None) -> tuple[str, str, str | None, str | None]:
    combined = " ".join(text) if isinstance(text, list) else text

    headline = first_match(combined, [
        r"Title:\s*([^.:\n]+)",
        r"#\s*([^\n]+)",
        r"<title>\s*([^<]{10,120})\s*</title>",
        r'"headline"\s*:\s*"([^"]{10,150})"',
        r"([A-Z][A-Za-z0-9][^.]{18,90})",
    ]) or f"{source_name} update detected"

    target_url = pick_best_url(combined, source_url) or source_url or ""
    slug = title_to_slug(headline)
    identity = f"{source_name.lower().replace(' ', '-')}-{slug}"

    summary = (
        f"{source_name} changed. The visible update appears to be \"{clip(headline, 80)}\". "
        "Open the target URL to see details."
    )
    return headline, clip(summary, 250), target_url, identity