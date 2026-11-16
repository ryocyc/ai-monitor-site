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
BATCH_SIZE = 1   # v5 skeleton prompt is long: with 4-item batches M3 spent its
                 # whole reasoning budget before emitting JSON (12-call run:
                 # 8/51). Single-item calls verified reliable across the day.
CACHE_CAP = 2000

# Provider policy (user directive 2026-11-16, final): MiniMax ONLY.
# NO DeepSeek fallback, ever. If MiniMax is unavailable (529 waves, quota,
# anything), the batch is skipped and the enrichment queue waits for the
# next patrol (every 2h) - the cache makes progress cumulative.


def load_providers() -> list[dict]:
    """MiniMax only (user directive). Read from the local ZCode provider
    config; the key never leaves this machine and is never committed."""
    cfg = json.loads(ZCODE_PROVIDER_CONFIG.read_text(encoding="utf-8"))
    for rule in cfg["config"]["providerConfigRules"]["providerRules"]:
        if rule.get("providerName", "").lower() == "minimax":
            cfg_access = rule.get("config", {})
            key = cfg_access.get("access", {}).get("apiKey", "")
            base = cfg_access.get("api", {}).get("baseUrl", "").rstrip("/")
            if key and base:
                # M3 pool: wave-like 529s but verified open again right now;
                # M2.1 pool answers 200 with EMPTY responses while degraded
                # (observed 2026-11-16). All language models share the
                # 200 RPM / 10M TPM console limit.
                return [{"name": "MiniMax-M3", "url": base + "/text/chatcompletion_v2",
                         "key": key, "model": "MiniMax-M3", "retries": 2,
                         "backoff": 10}]
    raise RuntimeError("Minimax provider rule not found in ZCode provider config")

SYSTEM_PROMPT = (
    "你係幫一般 AI 用戶寫新聞卡嘅編輯。寫作骨架(必跟):假設讀者未聽過任何產品——"
    "第一句要用幾個字講主角係咩(例:「Blender(免費開源3D軟件)有個新外掛…」),"
    "跟住講佢做得到咩;第二句講對用戶嘅實際好處(以前要點,而家點),"
    "結尾用半句提一個同類選擇(用你自己知識,唔肯定就唔提)。"
    "熱度數字(points/留言/星數)只可做句尾佐證,唔准做主角。"
    "數字/功能宣稱只可來自提供內容;背景解釋用你自己知識,唔准作數。"
    "禁止「社群正在討論」開頭。只回覆 strict JSON,唔好 markdown 圍欄。"
)

USER_PROMPT_TEMPLATE = (
    "將以下 AI 動態改寫成用戶視角嘅雙語新聞卡。對每一條輸出:\n"
    "- headline_en: 自然英文標題(有事實,冇 clickbait,12 字內)\n"
    "- summary_en: 1-2 句:第一句係件事,第二句(如有料)係對用戶嘅意義;冇料就只保留第一句\n"
    "- headline_zh: 繁體中文標題(24字內,人名/產品名保留英文)\n"
    "- summary_zh: 繁體中文 70-110 字,按骨架:主角係咩(帶背景)→做到咩→對用戶好處(以前要點而家點)→半句同類選擇;唔好條條同一句式結尾\n\n"
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

# Bump when the prompt changes: cached enrichments written by an older
# prompt must re-enter the queue automatically (a v2-prompt background
# job once locked 37 paraphrase-quality cards into the cache).
PROMPT_VERSION = "v5"


# Trending summaries end with live star counts ("Gained 8,852 GitHub
# stars in 18 days") that change every patrol - hashing them made every
# patrol invalidate the bilingual cache (overnight: 32 bilingual -> 4).
# The hash must track MEANINGFUL material only.
_VOLATILE_TAIL_RE = re.compile(r" Gained [\d,]+ GitHub stars.*$")


def src_hash(item: dict) -> str:
    """Hash of (prompt version + stable SOURCE material). Re-enriches when
    the summary's meaningful content changes (e.g. feeds now carry release
    notes) OR when the prompt is upgraded - but NOT when live star counts
    tick."""
    summary = _VOLATILE_TAIL_RE.sub("", item.get("summary_en", "") or "")
    material = f"{PROMPT_VERSION}|{item.get('headline_en', '')}|{summary}"
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
