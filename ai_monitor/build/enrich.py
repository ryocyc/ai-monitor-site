"""
User-perspective bilingual enrichment (the "Claude Code conversion" reborn,
without the old pipeline's sins).

Old flow (2026-04, autopsy'd): claude CLI shell-out with bypassPermissions,
no timeout, translated Chinese then _english_only() wiped it - 100% waste.
New flow: direct MiniMax API (native /text/chatcompletion_v2 endpoint),
batch per call, fingerprint-keyed cache so enriched text is NEVER lost
between patrols, hard call cap per run.

Config: reads the MiniMax key from the local ZCode provider config at
  C:\\Users\\ryocy\\.zcode\\v2\\provider_config.json  (rule providerName=Minimax)
The key never leaves this machine and is never committed.

Verified live 2026-11-16: MiniMax-M3 + subscription key -> 200.
Note: the OpenAI-style /chat/completions path returned 529 (overloaded);
the native path is the reliable one.

Usage:
  python build/enrich.py                 # enrich new items in site/data/feed.json
  python build/enrich.py --max-calls 4   # cost/call cap per run
Output: patches site/data/feed.json in place (+ state/enriched_cache.json,
which build_feed.py applies on every rebuild so enrichment survives patrols).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
SITE_DATA = BASE_DIR / "site" / "data"
FEED_FILE = SITE_DATA / "feed.json"
ARCHIVE_FILE = SITE_DATA / "archive.json"
CACHE_FILE = BASE_DIR / "state" / "enriched_cache.json"
ZCODE_PROVIDER_CONFIG = pathlib.Path(r"C:\Users\ryocy\.zcode\v2\provider_config.json")

# Provider chain: MiniMax (subscription key, primary) -> DeepSeek (fallback).
# Rationale: MiniMax clusters shed subscription traffic with HTTP 529 under
# load (verified live 2026-11-16: whole-cluster 529 for ~30 min while a tiny
# probe succeeded earlier); DeepSeek v4 flash answered 200 with Traditional
# Chinese the same minute. Fallback keeps enrichment flowing.
REQUEST_TIMEOUT = 120
PROVIDER_RETRIES = 2
PROVIDER_BACKOFF_SECONDS = 30
BATCH_SIZE = 8
CACHE_CAP = 2000

# Fallback policy (user directive 2026-11-16): the MiniMax TokenPlanPlus is
# effectively unlimited (verified in console: 5h quota 2% used, weekly 0%,
# M3 rate limit 200 RPM / 10M TPM vs our ~72K tokens/day). The day's 529s
# were cluster-side overload shedding ("overloaded_error 2064"), NOT quota.
#   - 529 overloaded  -> NO fallback; queue waits for the next patrol.
#   - explicit quota/limit error (403/429/quota wording) -> treat as the
#     5h window being exhausted: fallback allowed, back to MiniMax after
#     5h (state/minimax_quota.json).
QUOTA_BLOCK_FILE = BASE_DIR / "state" / "minimax_quota.json"
QUOTA_BLOCK_HOURS = 5
QUOTA_MARKERS = ("配额", "额度", "限額", "已用完", "exceeded", "quota", "insufficient")


def load_quota_block_until() -> dt.datetime | None:
    if QUOTA_BLOCK_FILE.exists():
        try:
            data = json.loads(QUOTA_BLOCK_FILE.read_text(encoding="utf-8"))
            until = dt.datetime.fromisoformat(data.get("block_until", ""))
            if until > dt.datetime.now(dt.timezone.utc):
                return until
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def mark_quota_exhausted() -> None:
    """Explicit quota error from MiniMax -> trust the 5h window: block
    MiniMax and let the fallback serve until it auto-expires."""
    QUOTA_BLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=QUOTA_BLOCK_HOURS)
    QUOTA_BLOCK_FILE.write_text(json.dumps(
        {"block_until": until.isoformat(), "marked_at": utc_now()},
        ensure_ascii=False), encoding="utf-8")
    print(f"    MiniMax quota window exhausted -> fallback until {until.isoformat()}")


def is_quota_error(status: int, body: str) -> bool:
    if status in (403, 429):
        return True
    lowered = body.lower()
    return status == 529 and any(m in body or m in lowered for m in QUOTA_MARKERS) \
        and not any(m in body for m in ("负载", "overloaded"))


def load_providers() -> list[dict]:
    """MiniMax primary (prepaid plan, user preference). DeepSeek serves ONLY
    while an explicit quota-exhaustion window is active (5h)."""
    cfg = json.loads(ZCODE_PROVIDER_CONFIG.read_text(encoding="utf-8"))
    minimax = deepseek = None
    for rule in cfg["config"]["providerConfigRules"]["providerRules"]:
        name = rule.get("providerName", "").lower()
        cfg_access = rule.get("config", {})
        key = cfg_access.get("access", {}).get("apiKey", "")
        base = cfg_access.get("api", {}).get("baseUrl", "").rstrip("/")
        if not key or not base:
            continue
        if name == "minimax":
            minimax = {"name": "MiniMax-M3", "url": base + "/text/chatcompletion_v2",
                       "key": key, "model": "MiniMax-M3", "retries": 2,
                       "backoff": 10}
        elif "deepseek" in name:
            deepseek = {"name": name, "url": base + "/chat/completions",
                        "key": key, "model": "deepseek-v4-flash", "retries": 2,
                        "backoff": 15}
    blocked_until = load_quota_block_until()
    if blocked_until is not None:
        print(f"MiniMax quota window active until {blocked_until.isoformat()} - fallback serving")
        providers = [p for p in (deepseek,) if p]
    else:
        providers = [p for p in (minimax,) if p]
    if not providers:
        raise RuntimeError("no usable provider (Minimax/deepseek) in ZCode provider config")
    return providers

SYSTEM_PROMPT = (
    "你係 AI Signal Feed 嘅新聞編輯,為一般 AI 用戶(唔係專家)改寫 AI 動態。"
    "規則:只可以根據提供嘅內容改寫,唔准加任何新事實、數字或日期。"
    "資料夠就寫得具體;資料真係得個標題,就一句講完個重點,唔准用固定套語"
    "(例如唔好每次都寫『官方未提供細節』,可以唔提,或者用唔同講法)。"
    "只回覆 strict JSON,唔好 markdown 圍欄,唔好解釋。"
)

USER_PROMPT_TEMPLATE = (
    "將以下 AI 動態改寫成用戶視角嘅雙語卡片。對每一條輸出:\n"
    "- headline_en: 自然英文標題(有事實,冇 clickbait,12 字內)\n"
    "- summary_en: 1-2 句英文:第一句講發生咩事,第二句(如有料)講對用戶即係咩"
    "(幾錢/幾勁/掛咗未/要不要理);冇料就只保留第一句\n"
    "- headline_zh: 繁體中文標題(24字內,人名/產品名保留英文)\n"
    "- summary_zh: 繁體中文 40-70 字,同上結構;五條唔好用同一句式結尾\n\n"
    "ITEMS:\n{items_json}\n\n"
    "回覆格式: {{\"items\": [{{\"id\": \"...\", \"headline_en\": \"...\", "
    "\"summary_en\": \"...\", \"headline_zh\": \"...\", \"summary_zh\": \"...\"}}]}}"
)


def call_provider(provider: dict, prompt: str) -> dict:
    """One provider attempt-chain. Raises on final failure so the next
    provider in the chain gets its turn."""
    body = json.dumps({
        "model": provider["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        # reasoning models spend thinking tokens before the answer; too small
        # a budget yields empty content (observed live on deepseek-v4-flash)
        "max_tokens": 16384,
    }).encode("utf-8")
    last_error = ""
    for attempt in range(1, provider["retries"] + 1):
        request = urllib.request.Request(
            provider["url"], data=body,
            headers={"Authorization": f"Bearer {provider['key']}",
                     "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            message = (payload.get("choices") or [{}])[0].get("message", {}) or {}
            content = message.get("content") or message.get("reasoning_content") or ""
            if not content.strip():
                raise ValueError("empty content (reasoning spent all tokens?)")
            return parse_items_json(content)
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode(errors="replace")
            last_error = f"HTTP {exc.code}: {body_text[:120]}"
            if provider["name"].startswith("MiniMax") and is_quota_error(exc.code, body_text):
                mark_quota_exhausted()
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        print(f"    {provider['name']} attempt {attempt}/{provider['retries']} failed: {last_error[:110]}")
        if attempt < provider["retries"]:
            time.sleep(provider.get("backoff", 15) * attempt)
    raise RuntimeError(f"{provider['name']} failed: {last_error[:150]}")


def call_with_fallback(providers: list[dict], prompt: str) -> dict:
    errors = []
    for provider in providers:
        try:
            result = call_provider(provider, prompt)
            if provider["name"] != providers[0]["name"]:
                print(f"    (served by fallback: {provider['name']})")
            return result
        except RuntimeError as exc:
            errors.append(str(exc))
    raise RuntimeError(" all providers failed: " + " | ".join(errors)[:300])


def parse_items_json(content: str) -> dict:
    content = (content or "").strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.S)
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise ValueError(f"no JSON in response: {content[:120]}")
    parsed = json.loads(content[start:end + 1])
    if not isinstance(parsed.get("items"), list):
        raise ValueError("response missing 'items' array")
    return parsed


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


import hashlib as _hashlib


def src_hash(item: dict) -> str:
    """Hash of the SOURCE material an item was enriched from. When the source
    summary changes (e.g. feeds now carry release notes), the cached zh is
    stale and the item re-joins the enrichment queue automatically."""
    material = f"{item.get('headline_en', '')}|{item.get('summary_en', '')}"
    return _hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]


def save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    trimmed = dict(list(cache.items())[-CACHE_CAP:])
    CACHE_FILE.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-calls", type=int, default=8,
                        help="hard cap on API calls this run (default 8 = up to 64 items)")
    parser.add_argument("--refresh", action="store_true",
                        help="re-enrich ALL items (overwrites cache) - use after prompt changes")
    parser.add_argument("--dry-run", action="store_true", help="show queue size, no API calls")
    args = parser.parse_args()

    if not FEED_FILE.exists():
        print("no feed.json yet - run the patrol first")
        return 0
    feed = json.loads(FEED_FILE.read_text(encoding="utf-8"))
    items = feed.get("items", [])
    cache = load_cache()

    # apply cached enrichment first (free), find what genuinely needs the API
    pending: list[dict] = []
    cached_applied = 0
    for item in items:
        cached = cache.get(item.get("id", ""))
        current_src = src_hash(item)
        if cached and not args.refresh and cached.get("src_hash") == current_src:
            item.update({k: v for k, v in cached.items() if k != "src_hash"})
            item["enriched"] = True
            cached_applied += 1
        else:
            # no cache, --refresh, or source material changed since enrichment
            pending.append(item)

    print(f"feed items: {len(items)} | cache-applied: {cached_applied} | need API: {len(pending)}")

    if args.dry_run or not pending:
        FEED_FILE.write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{len(items)} items finalised (enrichment cache {len(cache)} entries)")
        return 0

    providers = load_providers()
    print(f"providers: {' -> '.join(p['name'] for p in providers)}")
    batches = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
    batches = batches[:args.max_calls]
    done = 0
    for index, batch in enumerate(batches, start=1):
        compact = [{"id": i["id"], "category": i.get("category", ""),
                    "source_name": i.get("source_name", ""),
                    "headline_en": i.get("headline_en", ""),
                    "summary_en": i.get("summary_en", "")[:400]}
                   for i in batch]
        print(f"  batch {index}/{len(batches)} ({len(batch)} items) ...")
        try:
            result = call_with_fallback(providers, USER_PROMPT_TEMPLATE.format(
                items_json=json.dumps(compact, ensure_ascii=False)))
        except RuntimeError as exc:
            print(f"    [warn] batch skipped:{exc}")
            continue
        by_id = {entry.get("id"): entry for entry in result.get("items", [])}
        for item in batch:
            entry = by_id.get(item["id"])
            if not entry or not entry.get("headline_zh"):
                continue
            enrichment = {
                "headline_en": (entry.get("headline_en") or item.get("headline_en", "")).strip(),
                "summary_en": (entry.get("summary_en") or item.get("summary_en", "")).strip(),
                "headline_zh": (entry.get("headline_zh") or "").strip(),
                "summary_zh": (entry.get("summary_zh") or "").strip(),
                "src_hash": src_hash(item),
            }
            item.update(enrichment)
            item["enriched"] = True
            cache[item["id"]] = enrichment
            done += 1
        time.sleep(2)

    save_cache(cache)
    FEED_FILE.write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")

    # patch archive too so enrichment survives archive-only rebuilds
    # (same src_hash guard as build_feed - never apply stale zh over
    # richer source material)
    if ARCHIVE_FILE.exists():
        archive = json.loads(ARCHIVE_FILE.read_text(encoding="utf-8"))
        patched = 0
        for item in archive.get("items", []):
            cached = cache.get(item.get("id", ""))
            if cached and not item.get("enriched") \
                    and cached.get("src_hash") == src_hash(item):
                item.update({k: v for k, v in cached.items() if k != "src_hash"})
                item["enriched"] = True
                patched += 1
        if patched:
            ARCHIVE_FILE.write_text(json.dumps(archive, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
            print(f"archive patched: {patched}")

    print(f"enriched {done}/{len(pending)} items this run "
          f"({len(batches)} API calls, cache {len(cache)}) -> feed.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
