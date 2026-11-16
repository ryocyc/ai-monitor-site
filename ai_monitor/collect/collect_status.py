"""
Pillar 4 (outages/errors) collector: poll AI provider status APIs and emit
feed items only when a status indicator CHANGES (e.g. none -> minor = "Claude
partially degraded", minor -> none = "Claude back to normal").

Endpoints verified 2026-11-16 (see docs/source_strategy.md):
  OpenAI status.openai.com (HTML is 403 CF but this API is open)
  Claude  status.claude.com  (moved from status.anthropic.com)
  Cohere  status.cohere.com
  Mistral status.mistral.ai
  Groq    groqstatus.com
  Perplexity status.perplexity.ai (unverified, graceful-fail allowed)

Output: data/status_items.json  State: state/status.json
Exit 1 only if EVERY endpoint failed (patrol alerting).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATE_DIR = BASE_DIR / "state"
STATE_FILE = STATE_DIR / "status.json"

USER_AGENT = "AI-News-Monitor/1.0 (+https://aisignalfeed.com)"
REQUEST_TIMEOUT = 15
SLEEP_SECONDS = 0.8

SEVERITY_RANK = {"none": 0, "minor": 1, "partial": 1, "degraded": 1,
                 "major": 2, "critical": 3, "maintenance": 1}

STATUS_ENDPOINTS = [
    ("OpenAI",      "https://status.openai.com/api/v2/status.json"),
    ("Claude",      "https://status.claude.com/api/v2/status.json"),
    ("Cohere",      "https://status.cohere.com/api/v2/status.json"),
    ("Mistral AI",  "https://status.mistral.ai/api/v2/status.json"),
    ("Groq",        "https://groqstatus.com/api/v2/status.json"),
    ("Perplexity",  "https://status.perplexity.ai/api/v2/status.json"),
]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def fetch_status(url: str) -> dict | None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        status = payload.get("status") or {}
        indicator = (status.get("indicator") or "unknown").strip().lower()
        description = (status.get("description") or "").strip()
        page = (payload.get("page") or {}).get("name") or ""
        return {"indicator": indicator, "description": description, "page_name": page}
    except (urllib.error.URLError, TimeoutError, ConnectionError,
            ValueError, json.JSONDecodeError) as exc:
        print(f"[warn] {url}: {exc}", file=sys.stderr)
        return None


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def make_item(provider: str, indicator: str, description: str, event: str) -> dict:
    if event == "resolved":
        headline = f"{provider} status back to normal: {description or 'All Systems Operational'}"
        summary = f"{provider}'s status page indicator returned to 'none' — the earlier issue is resolved."
    else:
        headline = f"{provider} status: {description or indicator}"
        summary = (
            f"{provider}'s status page reports '{indicator}' — {description}. "
            f"Check the status page before blaming your prompts."
        )
    return {
        "id": "st-" + hashlib.sha1(f"{provider}|{event}|{utc_now()}".encode()).hexdigest()[:12],
        "timestamp": utc_now(),
        "category": "outage",
        "source_name": f"{provider} Status",
        "source_url": dict(STATUS_ENDPOINTS).get(provider, ""),
        "target_url": dict(STATUS_ENDPOINTS).get(provider, ""),
        "headline_en": headline,
        "summary_en": summary,
        "severity": indicator if event != "resolved" else "none",
        "event": event,
    }


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    state = load_state()
    items: list[dict] = []
    ok_count = 0
    new_state: dict = {}

    for provider, url in STATUS_ENDPOINTS:
        result = fetch_status(url)
        if result is None:
            new_state[provider] = state.get(provider) or {
                "indicator": "unknown", "description": "", "updated_at": None}
            time.sleep(SLEEP_SECONDS)
            continue
        ok_count += 1
        indicator = result["indicator"]
        description = result["description"]
        previous = state.get(provider) or {}
        prev_indicator = previous.get("indicator", "unknown")

        changed = indicator != prev_indicator
        new_state[provider] = {
            "indicator": indicator,
            "description": description,
            "updated_at": utc_now(),
        }

        if changed and prev_indicator != "unknown":
            rank_new = SEVERITY_RANK.get(indicator, 1)
            rank_prev = SEVERITY_RANK.get(prev_indicator, 0)
            event = "resolved" if rank_new < rank_prev else "issue"
            items.append(make_item(provider, indicator, description, event))
            print(f"  CHANGE {provider}: {prev_indicator} -> {indicator} ({description})")
        elif changed and prev_indicator == "unknown":
            # First observation. A non-healthy current state is real, live news
            # for users ("Claude is degraded right now") - emit it.
            if SEVERITY_RANK.get(indicator, 0) > 0:
                items.append(make_item(provider, indicator, description, "issue"))
                print(f"  SEED   {provider}: {indicator} ({description}) -> EMITTED (active issue)")
            else:
                print(f"  SEED   {provider}: {indicator} ({description})")
        else:
            print(f"  ok     {provider}: {indicator}")

        time.sleep(SLEEP_SECONDS)

    save_state(new_state)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "status_items.json").write_text(
        json.dumps({"generated_at": utc_now(), "item_count": len(items), "items": items},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Endpoints ok: {ok_count}/{len(STATUS_ENDPOINTS)} | changes: {len(items)} "
          f"-> data/status_items.json")
    return 0 if ok_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
