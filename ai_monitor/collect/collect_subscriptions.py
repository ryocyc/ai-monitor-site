"""
Consumer subscription pricing canary (pillar 3 consumer side).

Generic approach - no per-site selectors: for each pricing page, snapshot
every price-like string ($19.99 / ¥129 / US$20 ...) and diff the snapshot
between patrols. Any change emits ONE honest item: "the pricing page now
shows different price points" with the added/removed strings - no claim
about WHICH product changed unless the strings make it obvious.

Reachability verified 2026-11-20: x.ai/grok 200, mistral.ai/pricing 200,
kimi (302 follows), minimax (307 follows), platform.deepseek.com 202 (JS).
NOT reachable (CF 403): chatgpt.com/pricing, perplexity - excluded.

Output: data/subscriptions_items.json
State:  state/subscriptions.json (price-string snapshots)
First run seeds the baseline and emits nothing.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import time
import urllib.request

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATE_DIR = BASE_DIR / "state"
STATE_FILE = STATE_DIR / "subscriptions.json"

USER_AGENT = "AI-News-Monitor/1.0 (+https://aisignalfeed.com)"
REQUEST_TIMEOUT = 20
SLEEP_SECONDS = 0.8
MAX_DIFF_SHOWN = 6

PAGES = [
    ("Groq",        "https://groq.com/pricing/"),
    ("Mistral",     "https://mistral.ai/pricing/"),
    ("Grok (xAI)",  "https://x.ai/grok"),
    ("Kimi",        "https://kimi.moonshot.cn/pricing"),
    ("MiniMax",     "https://www.minimax.io/pricing"),
    ("DeepSeek API","https://platform.deepseek.com/pricing"),
]

PRICE_RE = re.compile(r"[$¥]\s?\d[\d,\.]*")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def page_text(url: str) -> str:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        raw = resp.read().decode(charset, errors="replace")
    raw = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", raw)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw))


def price_strings(text: str) -> set[str]:
    return set(m.group(0).replace(" ", "") for m in PRICE_RE.finditer(text))


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def make_item(name: str, url: str, added: list[str], removed: list[str]) -> dict:
    parts = []
    if removed:
        parts.append("消失咗: " + ", ".join(removed[:MAX_DIFF_SHOWN]))
    if added:
        parts.append("新增: " + ", ".join(added[:MAX_DIFF_SHOWN]))
    detail = "；".join(parts)
    return {
        "id": "sb-" + hashlib.sha1(f"{name}|{utc_now()}|{detail}".encode()).hexdigest()[:12],
        "timestamp": utc_now(),
        "category": "pricing",
        "source_name": f"{name} Pricing",
        "source_url": url,
        "target_url": url,
        "headline_en": f"{name} pricing page changed — recheck your plan costs",
        "summary_en": (f"The price points shown on {name}'s pricing page changed. "
                       f"{detail}. Verify the exact plan prices on the page before you pay."),
    }


def main() -> int:
    if sys.stdout is not None and sys.stdout.encoding \
            and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    state = load_state()
    items: list[dict] = []
    ok_count = 0
    new_state: dict = {}

    for name, url in PAGES:
        try:
            text = page_text(url)
            prices = price_strings(text)
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                ValueError) as exc:
            print(f"  ?      {name}: fetch failed ({type(exc).__name__})")
            time.sleep(SLEEP_SECONDS)
            continue
        ok_count += 1

        prev = state.get(name) or {}
        prev_set = set(prev.get("prices", []))
        if not prev.get("prices"):
            new_state[name] = {"prices": sorted(prices), "checked_at": utc_now()}
            print(f"  SEED   {name}: {len(prices)} price strings recorded")
            time.sleep(SLEEP_SECONDS)
            continue

        added = sorted(prices - prev_set)
        removed = sorted(prev_set - prices)
        new_state[name] = {"prices": sorted(prices), "checked_at": utc_now()}
        if added or removed:
            items.append(make_item(name, url, added, removed))
            print(f"  CHANGE {name}: +{added[:3]} -{removed[:3]}")
        else:
            print(f"  ok     {name}: {len(prices)} price strings unchanged")
        time.sleep(SLEEP_SECONDS)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(new_state, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "subscriptions_items.json").write_text(
        json.dumps({"generated_at": utc_now(), "item_count": len(items),
                    "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"Pages ok: {ok_count}/{len(PAGES)} | price changes: {len(items)} "
          f"-> data/subscriptions_items.json")
    return 0 if ok_count > 0 else 0  # canary failures are not patrol failures


if __name__ == "__main__":
    raise SystemExit(main())
