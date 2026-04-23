"""
Quality gates for entry-point sources (docs index, landing pages, org pages,
pricing pages, changelog indexes, status pages, blog indexes).

These functions provide reusable, source-type-agnostic checks so the system
handles noisy entry-point pages automatically without per-source hardcoding.
"""

from __future__ import annotations

import re
from typing import Any


# -------------------------------------------------------------------
# Noise signature patterns — common across entry-point page types
# -------------------------------------------------------------------

NAV_NOISE_TERMS = frozenset([
    "get started", "documentation", "docs", "api reference", "guide",
    "tutorial", "overview", "introduction", "quickstart", "install",
    "setting up", "setup", "configuration", "config", "authentication",
    "authorization", "billing", "pricing", "plans", "enterprise",
    "contact sales", "signup", "sign in", "login", "register",
    "status", "system status", "uptime", "incident", "operational",
    "changelog", "release notes", "versions", "migration", "upgrade",
    "pinned", "repositories", "stars", "forks", "watchers", "followers",
    "pull requests", "issues", "discussions", "packages", "actions",
    "security", "insights", "settings", "profile", "explore",
    "free tier", "pay as you go", "per request", "per token", "per minute",
    "enterprise plan", "team plan", "developer", "starter", "scale",
    "unlimited", "usage limit", "rate limit", "quota",
    "subscribe", "newsletter", "email address", "phone number", "country",
    "otp", "verification", "captcha", "i agree", "terms of service",
    "privacy policy", "cookie", "gdpr", "accessibility",
    "table of contents", "on this page", "previous", "next", "back to top",
    "search documentation", "filter", "sort by", "category",
    "github", "readme", "license", "contributing", "roadmap",
    "community", "support", "faq", "blog", "forum", "discord",
])

ENTRY_POINT_URL_PATTERNS = (
    re.compile(r"/$"),
    re.compile(r"/(index|home|main)\.html?$", re.IGNORECASE),
    re.compile(r"/(docs|documentation|guides?)/?$", re.IGNORECASE),
    re.compile(r"/(api|reference)/?$", re.IGNORECASE),
    re.compile(r"/(pricing|plans|billing)/?$", re.IGNORECASE),
    re.compile(r"/(changelog|releases?|whats.?new)/?$", re.IGNORECASE),
    re.compile(r"/(status|uptime|health)/?$", re.IGNORECASE),
    re.compile(r"/(blog|news|articles?)/?$", re.IGNORECASE),
    re.compile(r"/(about|company|careers|legal|privacy|terms)/?$", re.IGNORECASE),
    re.compile(r"github\.com/[a-zA-Z0-9_-]+/?$"),
    re.compile(r"github\.com/[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+/?$"),
)

GENERIC_HEADLINE_PATTERNS = (
    re.compile(r"\b(update|updated|change|changed|modified|refresh(ed)?)\s+(detected|page|content|site|homepage|index)\b", re.IGNORECASE),
    re.compile(r"\b(docs|documentation|api|reference|landing)\s+(update|page|updated)\b", re.IGNORECASE),
    re.compile(r"\b(page|content|homepage|index|site)\s+(update|changed|modified)\b", re.IGNORECASE),
    re.compile(r"\b(new|latest)\s+(update|post|article|announcement)\b", re.IGNORECASE),
    re.compile(r"^(Overview|Introduction|Summary|Home|Index)$", re.IGNORECASE),
    re.compile(r"\b(announcement|newsletter|subscribe|follow)\b", re.IGNORECASE),
)

# Identity patterns that look like noisy fallbacks (source-source-key chains)
_GENERIC_IDENTITY_PATTERNS = (
    re.compile(r"^(openai|anthropic|huggingface|cohere|mistral|qwen|deepseek|groq|replicate)-(openai|anthropic|huggingface|cohere|mistral|qwen|deepseek|groq|replicate)-"),
    re.compile(r"^(github|ollama|llamacpp|vllm)-(github|ollama|llamacpp|vllm)-"),
    re.compile(r"-changelog-changelog$"),
    re.compile(r"-releases-releases$"),
    re.compile(r"-blog-blog$"),
    re.compile(r"-news-news$"),
    re.compile(r"-status-status$"),
    re.compile(r"-home-home$"),
    re.compile(r"-update-update$"),
)


# -------------------------------------------------------------------
# Source-type classification
# -------------------------------------------------------------------

def classify_source_type(source_name: str, source_url: str) -> str:
    name_lower = source_name.lower()
    url_lower = source_url.lower()

    if any(kw in name_lower for kw in ("status", "uptime", "incident")):
        return "status"
    if any(kw in name_lower for kw in ("pricing", "plan", "billing", "cost")):
        return "pricing"
    if any(kw in name_lower for kw in ("changelog", "release notes", "whats new")):
        return "changelog"
    if any(kw in name_lower for kw in ("docs", "reference", "api", "guide", "tutorial")):
        return "docs_api"
    if any(kw in name_lower for kw in ("blog", "news", "announcement")):
        return "news_blog"
    if "github" in url_lower:
        if "releases.atom" in url_lower or "/releases." in url_lower:
            return "github_org"
        if any(p.match(url_lower) for p in ENTRY_POINT_URL_PATTERNS):
            return "github_org"

    if any(p.search(url_lower) for p in ENTRY_POINT_URL_PATTERNS):
        if any(kw in url_lower for kw in ("status", "uptime", "health")):
            return "status"
        if any(kw in url_lower for kw in ("pricing", "plan", "bill")):
            return "pricing"
        if any(kw in url_lower for kw in ("changelog", "release", "whats-new")):
            return "changelog"
        if any(kw in url_lower for kw in ("/docs", "/documentation", "/reference", "/api")):
            return "docs_api"
        if any(kw in url_lower for kw in ("/blog", "/news", "/articles")):
            return "news_blog"
        if "github.com" in url_lower:
            return "github_org"
        return "homepage"

    return "unknown"


# -------------------------------------------------------------------
# Content quality scoring
# -------------------------------------------------------------------

def score_content_specificity(item: dict[str, Any]) -> tuple[int, str]:
    score = 50
    reasons: list[str] = []

    article_identity = item.get("article_identity", "") or ""
    headline = item.get("headline_en", "")
    target_url = item.get("target_url", "") or ""
    source_url = item.get("source_url", "") or ""
    evidence = item.get("evidence_excerpt", "") or ""

    # Identity quality
    if article_identity:
        is_noisy = (
            len(article_identity) > 80
            or any(p.match(article_identity) for p in _GENERIC_IDENTITY_PATTERNS)
        )
        if is_noisy:
            score -= 30
            reasons.append("noisy_identity")
        else:
            score += 20
            reasons.append("specific_identity")
    else:
        score -= 25
        reasons.append("no_identity")

    # Headline quality
    if headline and not any(p.match(headline) for p in GENERIC_HEADLINE_PATTERNS):
        if 10 <= len(headline) <= 100:
            score += 15
            reasons.append("good_headline")
    else:
        score -= 20
        reasons.append("generic_headline")

    # URL specificity
    if target_url and target_url != source_url:
        is_entry = any(p.search(target_url.lower()) for p in ENTRY_POINT_URL_PATTERNS)
        if not is_entry and len(target_url) > 30:
            score += 10
            reasons.append("specific_url")
        elif is_entry:
            score -= 10
            reasons.append("entry_url")

    # Evidence noise
    if evidence:
        noise_count = sum(1 for term in NAV_NOISE_TERMS if term in evidence.lower())
        if noise_count >= 5:
            score -= 25
            reasons.append("very_noisy_excerpt")
        elif noise_count >= 3:
            score -= 10
            reasons.append("noisy_excerpt")

    return max(0, min(100, score)), ",".join(reasons) if reasons else "neutral"


# -------------------------------------------------------------------
# Quality gate
# -------------------------------------------------------------------

class QualityGate:
    def __init__(
        self,
        homepage_score_min: int = 55,
        allow_generic_fallback: bool = False,
    ) -> None:
        self.homepage_score_min = homepage_score_min
        self.allow_generic_fallback = allow_generic_fallback

    def is_entry_point_type(self, source_name: str, source_url: str) -> bool:
        t = classify_source_type(source_name, source_url)
        return t in (
            "docs_api", "pricing", "changelog", "status",
            "github_org", "homepage", "unknown",
        )

    def should_demote_to_archive(self, item: dict[str, Any]) -> tuple[bool, str]:
        source_name = item.get("source_name", "")
        source_url = item.get("source_url", "") or ""

        score, reason = score_content_specificity(item)
        source_type = classify_source_type(source_name, source_url)

        # Always demote if score is below threshold
        if score < self.homepage_score_min:
            return True, f"low_score({score}<{self.homepage_score_min})[{reason}]"

        # Hard block: status and github_org types need truly specific content
        if source_type in ("status", "github_org"):
            article_identity = item.get("article_identity", "") or ""
            headline = item.get("headline_en", "")
            target_url = item.get("target_url", "") or ""

            has_noisy_identity = (
                not article_identity
                or any(p.match(article_identity) for p in _GENERIC_IDENTITY_PATTERNS)
                or len(article_identity) > 80
            )
            has_generic_headline = any(p.match(headline) for p in GENERIC_HEADLINE_PATTERNS)
            has_entry_url = any(p.search(target_url.lower()) for p in ENTRY_POINT_URL_PATTERNS)

            if has_noisy_identity or has_generic_headline or has_entry_url:
                return True, f"hard_block[{source_type}][{reason}]"

        # Demote entry-point sources with generic content
        if not self.allow_generic_fallback:
            headline = item.get("headline_en", "")
            if self.is_entry_point_type(source_name, source_url):
                if any(p.match(headline) for p in GENERIC_HEADLINE_PATTERNS):
                    return True, f"generic_headline_for_entry_point[{reason}]"
                article_identity = item.get("article_identity", "") or ""
                if not article_identity or any(p.match(article_identity) for p in _GENERIC_IDENTITY_PATTERNS):
                    return True, f"generic_or_missing_identity[{reason}]"
                if len(article_identity) > 120:
                    return True, f"huge_identity_for_entry_point[{reason}]"

        return False, reason

    def homepage_qualifies(self, item: dict[str, Any]) -> bool:
        demote, _ = self.should_demote_to_archive(item)
        return not demote


DEFAULT_GATE = QualityGate(homepage_score_min=55, allow_generic_fallback=False)
