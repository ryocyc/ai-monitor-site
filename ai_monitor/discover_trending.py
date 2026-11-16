"""
Discover trending / exploding AI open-source projects on GitHub.

Replaces the old HTML-scraping approach (discover_github_sources.py) with the
official GitHub Search API (via authenticated `gh` CLI), which gives exact star
counts, creation dates, and topic filters without fragile HTML parsing.

Detection strategy:
  1. "Hot new" repos: created within 7-30 days, AI topics, star thresholds.
  2. Velocity: stars-per-day, either measured against the previous run's
     snapshot (state/trending/stars.json) or estimated as stars / age_days
     on first run.

Output (all paths relative to ai_monitor/):
  data/trending_items.json      feed-ready items (the future feed.json contract seed)
  data/trending_candidates.json full candidate detail (debug / review)
  state/trending/stars.json     star snapshot for velocity measurement

Hygiene: `gh` subprocesses use CREATE_NO_WINDOW on Windows; each search call
has a timeout; total runtime stays well under the 30-searches/min API limit
by sleeping between queries.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time

BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATE_DIR = BASE_DIR / "state" / "trending"
STARS_STATE = STATE_DIR / "stars.json"

SEARCH_SLEEP_SECONDS = 2.5
GH_TIMEOUT_SECONDS = 45

# ---------------------------------------------------------------------------
# Query plan: ~12 searches per run, comfortably below the 30/min search limit.
# (tag, query-template, days-back, min-stars, per-page)
# ---------------------------------------------------------------------------
QUERY_PLAN = [
    ("new_ai_7d",     "topic:ai created:>{date} stars:>{stars}",        7,  50,  25),
    ("new_llm_7d",    "topic:llm created:>{date} stars:>{stars}",       7,  50,  25),
    ("new_agent_7d",  "topic:ai-agents created:>{date} stars:>{stars}", 7,  30,  25),
    ("new_mcp_7d",    "topic:mcp created:>{date} stars:>{stars}",       7,  20,  25),
    ("new_rag_14d",   "topic:rag created:>{date} stars:>{stars}",       14, 30,  25),
    ("new_diff_14d",  "topic:diffusion created:>{date} stars:>{stars}", 14, 30,  25),
    ("new_tts_14d",   "topic:tts created:>{date} stars:>{stars}",       14, 30,  25),
    ("new_ft_14d",    "topic:fine-tuning created:>{date} stars:>{stars}", 14, 30, 25),
    ("hot_ai_30d",    "topic:ai created:>{date} stars:>{stars}",        30, 400, 25),
    ("hot_llm_30d",   "topic:llm created:>{date} stars:>{stars}",       30, 400, 25),
    ("kw_agent_7d",   '"AI agent" in:name,description created:>{date} stars:>{stars}', 7, 100, 25),
    ("kw_localllm_14d", '"local llm" OR llama.cpp in:name,description created:>{date} stars:>{stars}', 14, 60, 25),
]

# ---------------------------------------------------------------------------
# Filters (reused from the old discover_github_sources.py, with word
# boundaries so e.g. "list" no longer matches "distillation").
# ---------------------------------------------------------------------------
EXCLUDE_PATTERNS = [
    "awesome", "curated", "list", "collection", "papers", "paper-",
    "tutorial", "course", "how to", "getting started",
    "examples", "example-", "demos", "demo-", "playground",
    "survey", "cheatsheet", "cheat-sheet",
    "resources", "resource-", "prompts", "prompt-", "jailbreak",
    "interview", "roadmap", "books", "feed", "rss", "news-",
]

# Generic infra, not AI tools (same spirit as the old INFRA_EXCLUDE_KEYWORDS)
INFRA_EXCLUDE_PATTERNS = [
    "blockchain", "web3", "nft", "crypto trading", "forex",
    "e-commerce", "shopify", "wordpress plugin", "crm", "erp",
]

INTERESTING_KEYWORDS = [
    "llm", "gpt", "agent", "agentic", "rag", "retrieval", "embedding",
    "mcp", "model context protocol", "diffusion", "stable diffusion",
    "tts", "speech", "voice", "whisper", "ocr", "vision", "multimodal",
    "fine-tuning", "finetuning", "quantization", "inference", "vllm",
    "ollama", "llama", "claude", "openai", "gemini", "chatbot",
    "coding agent", "code assistant", "computer use", "browser agent",
    "ai", "machine learning", "deep learning", "neural",
]

# ---------------------------------------------------------------------------
# 爆紅 thresholds
# ---------------------------------------------------------------------------
HOT_NEW_MAX_AGE_DAYS = 7      # repo age for "hot new" tier
HOT_NEW_MIN_STARS = 100       # stars needed for "hot new" tier
RISING_MIN_PER_DAY = 40.0     # stars/day for "rising" tier

MAX_ITEMS = 30


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def run_gh_api(query: str, per_page: int) -> list[dict] | None:
    """Call GitHub Search API through the authenticated gh CLI. None = failed."""
    args = ["gh", "api", "-X", "GET", "search/repositories",
            "-f", f"q={query}", "-f", "sort=stars", "-f", "order=desc",
            "-f", f"per_page={per_page}"]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(args, capture_output=True, timeout=GH_TIMEOUT_SECONDS,
                                creationflags=creationflags)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"[warn] gh api failed: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")[:200]
        print(f"[warn] gh api error: {stderr}", file=sys.stderr)
        return None
    try:
        payload = json.loads(result.stdout.decode("utf-8", errors="replace"))
        return payload.get("items", [])
    except json.JSONDecodeError:
        return None


def load_stars_state() -> dict:
    if STARS_STATE.exists():
        try:
            return json.loads(STARS_STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"updated_at": None, "repos": {}}


def save_stars_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = utc_now()
    STARS_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def word_match(text: str, patterns: list[str]) -> bool:
    lowered = text.lower()
    return any(re.search(rf"(?<![a-z0-9-]){re.escape(p)}(?![a-z0-9-])", lowered) for p in patterns)


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _name_tokens(name: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", name.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


YEAR_SUFFIX_RE = re.compile(r"(?:19|20)\d{2}")


def _owner_year_suspect(r: dict) -> bool:
    """Spam owners observed live ('racdeufindurch198812', 'zovulrira1974',
    'tiobeharxi1987') carry a 4-digit year suffix in the login."""
    return bool(YEAR_SUFFIX_RE.search(r.get("owner", {}).get("login", "")))


def _created_dt(r: dict) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(r.get("created_at", "").replace("Z", "+00:00"))
    except ValueError:
        return dt.datetime.now(dt.timezone.utc)


def drop_spam_rings(repos: list[dict]) -> tuple[list[dict], int]:
    """Multi-signal star-farm detection (live-observed patterns):
      1. exact (description, stars) duplicates
      2. same normalized repo NAME created within 48h of each other
         (e.g. six 'ai--manus2.0' forks-farms, stars ±2% to dodge exact match)
      3. fuzzy: name token similarity >= 0.75 AND created within 48h AND
         star counts within ±15% (e.g. blender-ai-*-studio variants)
      4. one owner with 3+ repos in the result set -> keep only the top one
    Returns (kept_repos, dropped_count)."""
    kept: list[dict] = []

    # signal 1: exact description+stars groups
    groups: dict[tuple, list[dict]] = {}
    for r in repos:
        key = ((r.get("description") or "").strip().lower(), r.get("stargazers_count", 0))
        groups.setdefault(key, []).append(r)
    exact_dropped = sum(len(g) for g in groups.values() if len(g) >= 3)
    pool = [g[0] for g in groups.values() if len(g) < 3]

    # signals 2+3: name clustering across remaining repos
    entries = sorted(pool, key=_created_dt)  # earliest first = ring anchors
    ring_members: set[int] = set()
    for i, a in enumerate(entries):
        if i in ring_members:
            continue
        a_norm = _norm_name(a.get("name", ""))
        a_tok = _name_tokens(a.get("name", ""))
        a_t = _created_dt(a)
        a_stars = max(a.get("stargazers_count", 1), 1)
        matched = False
        for j in range(i + 1, len(entries)):
            if j in ring_members:
                continue
            b = entries[j]
            if abs((_created_dt(b) - a_t).total_seconds()) > 48 * 3600:
                continue
            same_norm = _norm_name(b.get("name", "")) == a_norm
            b_tok = _name_tokens(b.get("name", ""))
            name_sim = _jaccard(b_tok, a_tok)
            stars_close = abs(b.get("stargazers_count", 0) - a_stars) / a_stars <= 0.15
            fuzzy = name_sim >= 0.75 and stars_close
            # Spam farms vary the last name token ('...-studio' vs '...-flow')
            # but keep 2+ leading tokens and reuse year-suffixed owner logins.
            prefix_sim = len(a_tok & b_tok) >= 2 and \
                min(len(a_tok), len(b_tok)) >= 3 and stars_close
            year_fuzzy = name_sim >= 0.5 and stars_close and \
                _owner_year_suspect(a) and _owner_year_suspect(b)
            if same_norm or fuzzy or prefix_sim or year_fuzzy:
                ring_members.add(j)
                matched = True
        if matched:
            ring_members.add(i)  # anchor belongs to the ring too
    name_dropped = len(ring_members)
    kept = [r for i, r in enumerate(entries) if i not in ring_members]

    # signal 4: one owner flooding the result set
    by_owner: dict[str, list[dict]] = {}
    for r in kept:
        by_owner.setdefault(r.get("owner", {}).get("login", ""), []).append(r)
    owner_dropped = 0
    final: list[dict] = []
    for owner, rs in by_owner.items():
        if owner and len(rs) >= 3:
            owner_dropped += len(rs) - 1
            final.append(max(rs, key=lambda r: r.get("stargazers_count", 0)))
        else:
            final.extend(rs)

    return final, exact_dropped + name_dropped + owner_dropped


def age_days(created_at: str) -> float:
    try:
        created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return max((dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 86400.0, 0.5)
    except ValueError:
        return 999.0


def classify(repo: dict, velocity_per_day: float) -> str | None:
    stars = repo["stargazers_count"]
    days = age_days(repo["created_at"])
    if days <= HOT_NEW_MAX_AGE_DAYS and stars >= HOT_NEW_MIN_STARS:
        return "hot_new"
    if velocity_per_day >= RISING_MIN_PER_DAY:
        return "rising"
    return None


def build_items(candidates: list[dict]) -> list[dict]:
    items = []
    for c in candidates[:MAX_ITEMS]:
        repo = c["repo"]
        metrics = c["metrics"]
        stars = metrics["stars"]
        per_day = metrics["velocity_per_day"]
        days = metrics["age_days"]
        # Human-editor headline: lead with WHAT THE TOOL DOES (its
        # description), not the star metadata. Stars/velocity are trailing
        # evidence in the summary, never the headline. (User directive:
        # 'threerocks/article2media: 351 stars in 8 days' says nothing;
        # '一鍵將文章/視頻做成小紅書圖文、AI 配音短片嘅開源工具' does.)
        description = (c["description"] or "").strip()
        first_sentence = re.split(r"(?<=[.!?。!?])\s", description, maxsplit=1)[0]
        headline = first_sentence[:110] if first_sentence else f"{repo}: trending AI tool"
        items.append({
            "id": "gh-" + hashlib.sha1(repo.encode("utf-8")).hexdigest()[:12],
            "timestamp": utc_now(),
            "category": "trending",
            "source_name": "GitHub",
            "source_url": f"https://github.com/{repo}",
            "target_url": f"https://github.com/{repo}",
            "headline_en": headline,
            "summary_en": (
                f"{repo} - {description or 'AI-related project'} "
                f"Gained {stars:,} GitHub stars in {days:.0f} days (~{per_day:.0f}/day)."
            ),
            "tier": c["tier"],
            "metrics": metrics,
        })
    return items


YEAR_SUFFIX_RE = re.compile(r"(?:19|20)\d{2}")


def compute_review_flags(candidate: dict) -> list[str]:
    """Human-review signals for star-farm / low-quality repos that survived
    the automated spam filters (single-account farms are hard to catch)."""
    flags = []
    metrics = candidate["metrics"]
    if YEAR_SUFFIX_RE.search(candidate["repo"].split("/")[0]):
        flags.append("owner_year_suffix")
    if not candidate["description"]:
        flags.append("no_description")
    if not candidate["language"]:
        flags.append("no_language")
    if metrics["stars"] >= 100 and metrics["forks"] <= 2:
        flags.append("hot_but_no_forks")
    return flags


def fetch_owner_account_age(owner: str) -> float | None:
    """GitHub account age in days, via gh (1 core-API call). None = failed."""
    args = ["gh", "api", f"users/{owner}", "--jq", ".created_at"]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(args, capture_output=True, timeout=GH_TIMEOUT_SECONDS,
                                creationflags=creationflags)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    try:
        created = dt.datetime.fromisoformat(
            result.stdout.decode("utf-8").strip().replace("Z", "+00:00"))
        return (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 86400.0
    except ValueError:
        return None


def build_review_list(candidates: list[dict], max_owner_checks: int = 10) -> dict:
    """Flag suspects, probe owner account age for flagged repos, and
    auto-exclude confident spam (brand-new account + brand-new repo + no
    engagement). Returns review structure; excludes from candidates in place."""
    suspects = []
    for c in candidates:
        flags = compute_review_flags(c)
        if not flags:
            continue
        c["review_flags"] = flags
        suspects.append(c)

    owner_cache: dict[str, float | None] = {}
    auto_excluded = []
    for c in suspects[:max_owner_checks]:
        owner = c["repo"].split("/")[0]
        if owner not in owner_cache:
            owner_cache[owner] = fetch_owner_account_age(owner)
        age = owner_cache[owner]
        c["owner_account_age_days"] = round(age, 1) if age is not None else None
        if age is not None and age < 30 and c["metrics"]["age_days"] < 7 \
                and c["metrics"]["forks"] <= 3:
            c["spam_confident"] = True
            auto_excluded.append(c)

    if auto_excluded:
        excluded_repos = {c["repo"] for c in auto_excluded}
        candidates[:] = [c for c in candidates if c["repo"] not in excluded_repos]

    return {
        "flagged_count": len(suspects),
        "owner_checks": {k: (round(v, 1) if v is not None else None)
                         for k, v in owner_cache.items()},
        "auto_excluded": [
            {"repo": c["repo"], "stars": c["metrics"]["stars"],
             "owner_account_age_days": c.get("owner_account_age_days"),
             "flags": c["review_flags"]}
            for c in auto_excluded
        ],
        "suspects": [
            {"repo": c["repo"], "stars": c["metrics"]["stars"],
             "flags": c["review_flags"]}
            for c in suspects if not c.get("spam_confident")
        ],
    }


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    prev_state = load_stars_state()
    prev_repos: dict[str, dict] = prev_state.get("repos", {})

    raw: dict[str, dict] = {}
    queries_run = 0
    for tag, template, days, stars, per_page in QUERY_PLAN:
        date = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%d")
        query = template.format(date=date, stars=stars)
        repos = run_gh_api(query, per_page)
        queries_run += 1
        if repos:
            for r in repos:
                full = r.get("full_name", "")
                if full and full not in raw:
                    r["_query_tag"] = tag
                    raw[full] = r
        time.sleep(SEARCH_SLEEP_SECONDS)

    if queries_run == 0:
        print("[error] No GitHub search queries succeeded.")
        return 1

    # ---- spam-ring filter: multi-signal star-farm detection ----
    deduped, spam_dropped = drop_spam_rings(list(raw.values()))

    # ---- content filters ----
    candidates = []
    filtered_out = 0
    for r in deduped:
        full = r.get("full_name", "")
        desc = (r.get("description") or "").strip()
        if r.get("fork"):
            filtered_out += 1
            continue
        combined = f"{full} {desc}"
        if word_match(combined, EXCLUDE_PATTERNS) or word_match(desc, INFRA_EXCLUDE_PATTERNS):
            filtered_out += 1
            continue
        # Brand-squatter convention ('ai--manus2.0', 'manus--ai'): legit repos
        # almost never use double dashes in their name.
        if "--" in r.get("name", ""):
            filtered_out += 1
            continue
        # Topic-based queries are AI-pre-filtered; keyword queries need the
        # interesting-keyword check applied to everything as a safety net.
        if not word_match(combined, INTERESTING_KEYWORDS):
            filtered_out += 1
            continue

        stars = r.get("stargazers_count", 0)
        days = age_days(r.get("created_at", ""))
        # Primary signal: average velocity since creation. Secondary: measured
        # delta against the previous run, only meaningful when the baseline is
        # >= 2h (a 3-minute-old baseline would always read as ~0/day).
        velocity_avg = stars / days
        velocity_recent: float | None = None
        prev = prev_repos.get(full)
        if prev and prev.get("stars") is not None:
            try:
                baseline_hours = (dt.datetime.now(dt.timezone.utc)
                                  - dt.datetime.fromisoformat(prev["updated_at"])).total_seconds() / 3600.0
            except ValueError:
                baseline_hours = 0.0
            if baseline_hours >= 2.0:
                velocity_recent = max(
                    (stars - prev["stars"]) / (baseline_hours / 24.0), 0.0)
        velocity = max(velocity_avg, velocity_recent or 0.0)

        tier = classify(r, velocity)
        if tier is None:
            continue

        candidates.append({
            "repo": full,
            "description": desc,
            "language": r.get("language") or "",
            "url": f"https://github.com/{full}",
            "created_at": r.get("created_at", ""),
            "tier": tier,
            "query_tags": [t for t in {r.get("_query_tag", "")} if t],
            "metrics": {
                "stars": stars,
                "age_days": round(days, 1),
                "velocity_per_day": round(velocity, 1),
                "velocity_avg_per_day": round(velocity_avg, 1),
                "velocity_recent_per_day": (round(velocity_recent, 1)
                                            if velocity_recent is not None else None),
                "forks": r.get("forks_count", 0),
            },
        })

    candidates.sort(key=lambda c: (c["metrics"]["velocity_per_day"], c["metrics"]["stars"]), reverse=True)

    review = build_review_list(candidates)

    items = build_items(candidates)

    # ---- update velocity snapshot ----
    new_repos = {full: {"stars": r.get("stargazers_count", 0), "updated_at": utc_now()}
                 for full, r in raw.items()}
    save_stars_state({"updated_at": utc_now(), "repos": {**prev_repos, **new_repos}})

    # ---- write outputs ----
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "trending_items.json").write_text(
        json.dumps({"generated_at": utc_now(), "item_count": len(items), "items": items},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA_DIR / "trending_candidates.json").write_text(
        json.dumps({
            "generated_at": utc_now(),
            "queries_run": queries_run,
            "raw_unique": len(raw),
            "spam_ring_dropped": spam_dropped,
            "filtered_out": filtered_out,
            "review": review,
            "candidate_count": len(candidates),
            "candidates": candidates,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- console summary (ASCII only) ----
    print(f"Queries run: {queries_run} | unique repos: {len(raw)} | "
          f"spam-ring dropped: {spam_dropped} | filtered out: {filtered_out} | "
          f"qualified: {len(candidates)} | suspects flagged: {review['flagged_count']} "
          f"| auto-excluded: {len(review['auto_excluded'])}")
    print("-" * 78)
    for c in candidates[:15]:
        m = c["metrics"]
        tier = {"hot_new": "HOT-NEW", "rising": "RISING "}[c["tier"]]
        print(f"{tier} | {m['velocity_per_day']:7.1f}/day | {m['stars']:6,} stars | "
              f"{m['age_days']:5.1f}d old | {c['language'][:8]:8} | {c['repo']}")
    print("-" * 78)
    print(f"Wrote {len(items)} items -> data/trending_items.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
