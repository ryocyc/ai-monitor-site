"""
GitHub Trending discovery script v0.3.
Finds AI/ML repos worth monitoring from GitHub Trending pages.
Refinements: null total_stars, better desc cleaning, tighter category rules.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import sys
import time
import urllib.request

BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SOURCES_JSON = BASE_DIR / "sources.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36 AI-Monitor-Discovery/0.3"
)

# Category order matters — first match wins
CATEGORY_MAP = [
    ("ai coding",   [
        "codex", "coding agent", "code agent", "swe-agent", "devin",
        "cursor", "continue", "aider", "devin", "code assistant",
        "ai coding", "coding assistant", "llm coder",
    ]),
    ("agent",       [
        "agent", "agentic", "multi-agent", "webagent", "browser agent",
        "task agent", "role agent", "ai agent", "orchestration agent",
    ]),
    ("inference",   [
        "inference", "inference engine", "model serving", "serving",
        "distillation", "tensor parallel",
    ]),
    ("local llm",   [
        "ollama", "llama.cpp", "vllm", "sglang", "local llm",
        "lm studio", "lmstudio", "ollama", "exllama", "llama.cpp",
    ]),
    ("workflow",    [
        "workflow", "pipeline", "orchestration", "automation",
        "no-code", "low-code", "flow",
    ]),
    ("rag",         [
        "rag", "retrieval", "vector search", "embedding model",
        "embeddings", "knowledge base",
    ]),
    ("gateway",     [
        "gateway", "ai gateway", "model gateway", "proxy", "load balancer",
        "router",
    ]),
    ("evals",       [
        "evals", "benchmark", "evaluation", "benchmarking",
        "llm benchmark", "red teaming",
    ]),
]

# Must match at least one to be a candidate
INTERESTING_KEYWORDS = [
    "codex", "coding agent", "code agent", "swe-agent", "devin",
    "cursor", "continue", "aider", "code assistant", "ai coding",
    "agent", "agentic", "multi-agent", "webagent", "browser agent",
    "inference", "inference engine", "model serving", "distillation",
    "ollama", "llama.cpp", "vllm", "sglang", "local llm", "lm studio",
    "workflow", "pipeline", "orchestration", "automation",
    "rag", "retrieval", "vector search", "embedding",
    "gateway", "ai gateway", "model gateway",
    "evals", "benchmark", "evaluation",
    "mcp", "model context protocol", "tool use", "function calling",
    "computer use", "computer agent",
]

# Any match → instant exclusion
EXCLUDE_PATTERNS = [
    "awesome", "curated", "list", "collection",
    "prompt", "prompts", "jailbreak", "dan", "roleplay",
    "tutorial", "course", "learn", "how to", "getting started",
    "example", "examples", "demo", "playground",
    "paper", "survey", "review", "blog",
    "resource", "resources", "toolkit", "tool collection",
    "cheatsheet", "cheat sheet", "cheat-sheet",
    "framework", "sdk",  # too generic
]

# These owners/topics represent generic infra, not AI tools
INFRA_EXCLUDE_KEYWORDS = [
    "database", "db", "sql", "mysql", "postgres", "redis", "mongodb",
    "cloud", "aws", "azure", "gcp", "kubernetes", "k8s", "docker",
    "server", "backend", "api gateway", "microservice", "grpc",
    "blockchain", "crypto", "web3", "nft",
    "iot", "sensor", "device", "embedded",
    "security", "auth", "identity", "iam", "sso", "oauth",
    "saas", "paas", "saas platform", "crm", "erp", "hr", "cms",
    "fintech", "payment", "trading", "stock", "finance",
]

INVALID_PATH_PATTERNS = [
    "sponsors", "orgs", "marketplace", "collections", "teams",
]

TRENDING_URLS = [
    ("https://github.com/trending", "github_trending"),
    ("https://github.com/trending/python", "github_trending_language"),
    ("https://github.com/trending/go", "github_trending_language"),
    ("https://github.com/trending/rust", "github_trending_language"),
    ("https://github.com/trending/typescript", "github_trending_language"),
]

HIGH_STARS_THRESHOLD = 500


def fetch_url(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def load_monitored_repos() -> set[str]:
    """Load GitHub repo identifiers from sources.json that are already monitored."""
    monitored = set()
    try:
        sources = json.loads(SOURCES_JSON.read_text(encoding="utf-8"))
        for src in sources:
            url = src.get("url", "")
            m = re.match(r"https://github\.com/([^/]+)/([^/]+)/releases\.atom", url)
            if m:
                monitored.add(f"{m.group(1)}/{m.group(2)}")
    except Exception:
        pass
    return monitored


def _strip_html(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<!--[\s\S]*?-->", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&#160;", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"&amp;", "&", text, flags=re.IGNORECASE)
    text = re.sub(r"&lt;", "<", text, flags=re.IGNORECASE)
    text = re.sub(r"&gt;", ">", text, flags=re.IGNORECASE)
    text = re.sub(r"&quot;", '"', text, flags=re.IGNORECASE)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    return text


def is_likely_garbage(text: str) -> bool:
    """Return True if the text is mostly garbage after HTML stripping."""
    stripped = _strip_html(text)
    if len(stripped) < 20:
        return True
    # Check for excessive non-CJK special characters in a small window
    if len(stripped) > 0:
        # Remove CJK, letters, numbers, spaces, basic punctuation
        noise = re.sub(r"[\u4e00-\u9fffA-Za-z0-9\s.,\-_!?():]", "", stripped)
        noise_ratio = len(noise) / max(len(stripped), 1)
        if noise_ratio > 0.4:
            return True
    return False


def clean_description(desc: str) -> str:
    """Clean description; return empty string if garbage."""
    if is_likely_garbage(desc):
        return ""
    text = _strip_html(desc)
    # For CJK-dominant text, only keep CJK characters and minimal punctuation
    cjk_ratio = len(re.findall(r"[\u4e00-\u9fff]", text)) / max(len(text), 1)
    if cjk_ratio > 0.6:
        text = re.sub(r"[^\u4e00-\u9fff\s.,\-_!?()]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 400:
        text = text[:400].rsplit(" ", 1)[0] + "..."
    return text


def is_valid_repo_path(owner: str, repo: str) -> bool:
    if not owner or not repo:
        return False
    if owner in INVALID_PATH_PATTERNS:
        return False
    if not re.match(r"^[a-zA-Z0-9_.\-]+$", repo):
        return False
    return True


def is_interesting(name: str, desc: str, lang: str) -> bool:
    name_lower = name.lower()
    desc_lower = desc.lower()
    combined = name_lower + " " + desc_lower

    for pat in EXCLUDE_PATTERNS:
        if pat in combined:
            return False

    for kw in INTERESTING_KEYWORDS:
        if kw in combined or kw in name_lower:
            return True

    return False


def is_infrastructure(desc: str) -> bool:
    """Return True if the description looks like generic infra, not an AI tool."""
    desc_lower = desc.lower()
    for kw in INFRA_EXCLUDE_KEYWORDS:
        if kw in desc_lower:
            return True
    return False


def categorize(name: str, desc: str, lang: str) -> str:
    name_lower = name.lower()
    desc_lower = desc.lower()
    combined = name_lower + " " + desc_lower

    for category, kws in CATEGORY_MAP:
        for kw in kws:
            if kw in combined:
                return category

    return "agent"  # fallback


def determine_discovery_reason(url_tag: str, stars_today: int, has_releases: bool) -> str:
    if stars_today >= HIGH_STARS_THRESHOLD:
        return "high_stars_today"
    if has_releases and url_tag == "github_trending":
        return "github_trending"
    return url_tag


def parse_trending_page(html: str) -> list[dict]:
    repos = []
    blocks = re.split(r"<article[^>]*>", html, flags=re.IGNORECASE)

    for block in blocks[1:]:
        link_match = re.search(r'href="/([^"/]+)/([^"]+)"', block)
        if not link_match:
            continue

        owner, repo_name = link_match.group(1), link_match.group(2)
        if not is_valid_repo_path(owner, repo_name):
            continue

        desc_match = re.search(r"<p[^>]*>([^<]{20,600}?)</p>", block, re.DOTALL)
        description = desc_match.group(1).strip() if desc_match else ""

        lang_match = re.search(
            r'<span[^>]*itemprop="programmingLanguage"[^>]*>([^<]+)</span>', block
        )
        if not lang_match:
            lang_match = re.search(r'toolbar-item[^>]*lang[^>]*>\s*([^<]+)', block, re.IGNORECASE)
        language = (lang_match.group(1).strip() if lang_match else "")

        # Stars today is the primary number on GitHub Trending
        # (the first star count shown is today's stars)
        today_match = re.search(r'aria-label="([\d,]+)\s*star', block)
        if not today_match:
            today_match = re.search(r'([\d,]+)\s*star', block)
        stars_str = (today_match.group(1).replace(",", "") if today_match else "0")
        try:
            stars_today = int(stars_str)
        except ValueError:
            stars_today = 0

        repos.append({
            "owner": owner,
            "repo": repo_name,
            "description": description,
            "language": language,
            "stars_today": stars_today,
            "url": f"https://github.com/{owner}/{repo_name}",
        })

    return repos


def check_has_releases(owner: str, repo: str) -> bool:
    try:
        atom_url = f"https://github.com/{owner}/{repo}/releases.atom"
        req = urllib.request.Request(atom_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def build_candidates() -> list[dict]:
    monitored = load_monitored_repos()
    seen = set()
    candidates = []

    for url, url_tag in TRENDING_URLS:
        try:
            html = fetch_url(url)
            repos = parse_trending_page(html)

            for r in repos:
                key = (r["owner"], r["repo"])
                if key in seen:
                    continue
                seen.add(key)

                full_name = f"{r['owner']}/{r['repo']}"
                if full_name in monitored:
                    continue

                name = r["repo"]
                desc = r["description"]
                lang = r["language"]

                if not is_interesting(name, desc, lang):
                    continue

                # Infrastructure check
                if is_infrastructure(desc):
                    continue

                has_releases = check_has_releases(r["owner"], r["repo"])
                reason = determine_discovery_reason(url_tag, r["stars_today"], has_releases)

                candidates.append({
                    "repo": full_name,
                    "url": r["url"],
                    "description": clean_description(desc),
                    "language": lang,
                    "stars_today": r["stars_today"],
                    "total_stars": None,  # cannot reliably get from Trending page
                    "why_interesting": categorize(name, desc, lang),
                    "candidate_source_url": (
                        f"https://github.com/{r['owner']}/{r['repo']}/releases.atom"
                        if has_releases
                        else r["url"]
                    ),
                    "has_releases": has_releases,
                    "discovery_reason": reason,
                })

        except Exception as exc:
            print(f"Error fetching {url}: {exc}", file=sys.stderr)
            time.sleep(2)

    return candidates


def score_candidate(c: dict) -> tuple[int, int]:
    return (c["stars_today"], 1 if c["has_releases"] else 0)


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output_path = DATA_DIR / "github_candidates.json"

    print("Discovering GitHub Trending repos (v0.3)...")
    candidates = build_candidates()

    unique: dict[str, dict] = {}
    for c in candidates:
        key = c["repo"]
        if key not in unique or score_candidate(c) > score_candidate(unique[key]):
            unique[key] = c

    sorted_candidates = sorted(unique.values(), key=score_candidate, reverse=True)

    result = {
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "total_candidates": len(sorted_candidates),
        "candidates": sorted_candidates,
    }

    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"Wrote {len(sorted_candidates)} candidates to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())