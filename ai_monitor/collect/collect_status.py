"""
Pillar 4 (outages/errors) collector: poll AI provider status and emit
feed items only when a status indicator CHANGES. The same state also
powers the homepage status board (site/data/status_board.json).

Full consumer coverage (user directive: the board must cover ALL the
AIs people actually use), via four adapter kinds:

  statuspage   - classic Statuspage status.json (5 providers)
  components   - component-level JSON, filtered (GitHub Copilot)
  incidents    - Google Cloud incidents.json, filtered for Gemini
  html-phrases - DeepSeek's custom page (state phrases in SSR HTML)

Verified live 2026-11-16 (see docs/source_strategy.md):
  status.openai.com / status.claude.com / status.cohere.com /
  status.mistral.ai / groqstatus.com        -> statuspage JSON
  www.githubstatus.com/api/v2/components.json-> Copilot components
  status.cloud.google.com/incidents.json     -> Gemini incidents
  status.perplexity.com/api/v2/status.json   -> (moved off .ai)
  status.deepseek.com                        -> HTML phrases
Dropped after probing: xAI/Grok (no public status page),
Hugging Face (statuspage host returns 401).

Output: data/status_items.json   State: state/status.json
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

# (display name, kind, url)
PROVIDERS = [
    ("ChatGPT",    "statuspage", "https://status.openai.com/api/v2/status.json"),
    ("Claude",     "statuspage", "https://status.claude.com/api/v2/status.json"),
    ("Gemini",     "incidents",  "https://status.cloud.google.com/incidents.json"),
    ("Copilot",    "components", "https://www.githubstatus.com/api/v2/components.json"),
    ("Perplexity", "statuspage", "https://status.perplexity.com/api/v2/status.json"),
    ("DeepSeek",   "html",       "https://status.deepseek.com/"),
    ("Mistral",    "statuspage", "https://status.mistral.ai/api/v2/status.json"),
    ("Groq",       "statuspage", "https://groqstatus.com/api/v2/status.json"),
    ("Cohere",     "statuspage", "https://status.cohere.com/api/v2/status.json"),
]

GEMINI_KEYWORDS = ("Gemini", "Vertex AI", "AI Studio")
COPILOT_KEYWORD = "Copilot"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _get(url: str, timeout: int = REQUEST_TIMEOUT) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def fetch_statuspage(url: str) -> dict | None:
    _, body = _get(url)
    payload = json.loads(body)
    status = payload.get("status") or {}
    return {"indicator": (status.get("indicator") or "unknown").strip().lower(),
            "description": (status.get("description") or "").strip()}


def fetch_components(url: str) -> dict | None:
    """GitHub-style component JSON: worst non-operational component that
    matches the keyword wins."""
    _, body = _get(url)
    payload = json.loads(body)
    worst = ("none", "All Systems Operational")
    for component in payload.get("components", []):
        if COPILOT_KEYWORD not in (component.get("name") or ""):
            continue
        status = (component.get("status") or "").strip().lower()
        if status in ("operational", ""):
            continue
        rank = {"degraded_performance": 1, "partial_outage": 2,
                "major_outage": 3, "under_maintenance": 1}.get(status, 1)
        if SEVERITY_RANK.get(worst[0], 0) < rank:
            worst = (
                {1: "minor", 2: "major", 3: "major"}.get(rank, "minor"),
                f"{component.get('name')}: {status.replace('_', ' ')}")
    return {"indicator": worst[0], "description": worst[1]}


def fetch_incidents(url: str) -> dict | None:
    """Google Cloud incidents.json: any ONGOING incident whose text mentions
    Gemini/Vertex AI/AI Studio flags the Gemini board entry."""
    _, body = _get(url, timeout=25)
    incidents = json.loads(body)
    if not isinstance(incidents, list):
        return None
    for incident in incidents:
        if incident.get("end"):
            continue  # resolved
        text = " ".join(filter(None, [
            incident.get("external_desc") or "",
            " ".join((u.get("title") or "") + " " + (u.get("text") or "")
                     for u in incident.get("updates", [])),
        ]))
        if any(k.lower() in text.lower() for k in GEMINI_KEYWORDS):
            severity = "major" if "outage" in text.lower() else "minor"
            return {"indicator": severity,
                    "description": (incident.get("external_desc") or "")[:160]}
    return {"indicator": "none", "description": "All Systems Operational"}


def fetch_html_phrases(url: str) -> dict | None:
    """DeepSeek's custom page embeds real component states in the SSR HTML
    (each state phrase appears exactly once - verified 2026-11-16). Worst
    phrase present wins; unknown if nothing matches."""
    _, body = _get(url)
    if "Major Outage" in body or "major_outage" in body:
        return {"indicator": "major", "description": "Major Outage"}
    if "Partial Outage" in body or "Degraded Performance" in body:
        return {"indicator": "minor",
                "description": "Degraded Performance (some components)"}
    if "All Systems Operational" in body:
        return {"indicator": "none", "description": "All Systems Operational"}
    return None


FETCHERS = {"statuspage": fetch_statuspage, "components": fetch_components,
            "incidents": fetch_incidents, "html": fetch_html_phrases}


def fetch_provider(kind: str, url: str) -> dict | None:
    try:
        return FETCHERS[kind](url)
    except (urllib.error.URLError, TimeoutError, ConnectionError,
            ValueError, json.JSONDecodeError, KeyError) as exc:
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
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def make_item(provider: str, indicator: str, description: str, event: str) -> dict:
    if event == "resolved":
        headline = f"{provider} status back to normal: {description or 'All Systems Operational'}"
        summary = f"{provider}'s status returned to normal — the earlier issue is resolved."
    else:
        headline = f"{provider} status: {description or indicator}"
        summary = (f"{provider}'s status page reports '{indicator}' — {description}. "
                   f"Check the status page before blaming your prompts.")
    return {
        "id": "st-" + hashlib.sha1(f"{provider}|{event}|{utc_now()}".encode()).hexdigest()[:12],
        "timestamp": utc_now(),
        "category": "outage",
        "source_name": f"{provider} Status",
        "source_url": PROVIDER_URLS.get(provider, ""),
        "target_url": PROVIDER_URLS.get(provider, ""),
        "headline_en": headline,
        "summary_en": summary,
        "severity": indicator if event != "resolved" else "none",
        "event": event,
    }


PROVIDER_URLS = {name: url for name, _, url in PROVIDERS}


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

    for provider, kind, url in PROVIDERS:
        result = fetch_provider(kind, url)
        if result is None:
            # keep last known indicator so the board never flickers on a
            # single failed poll
            new_state[provider] = state.get(provider) or {
                "indicator": "unknown", "description": "", "updated_at": None}
            print(f"  ?      {provider}: poll failed (kept last known)")
            time.sleep(SLEEP_SECONDS)
            continue
        ok_count += 1
        indicator = result["indicator"]
        description = result["description"]
        previous = state.get(provider) or {}
        prev_indicator = previous.get("indicator", "unknown")

        new_state[provider] = {"indicator": indicator, "description": description,
                               "updated_at": utc_now()}

        if indicator != prev_indicator and prev_indicator != "unknown":
            rank_new = SEVERITY_RANK.get(indicator, 1)
            rank_prev = SEVERITY_RANK.get(prev_indicator, 0)
            event = "resolved" if rank_new < rank_prev else "issue"
            items.append(make_item(provider, indicator, description, event))
            print(f"  CHANGE {provider}: {prev_indicator} -> {indicator} ({description})")
        elif indicator != prev_indicator and prev_indicator == "unknown":
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
        json.dumps({"generated_at": utc_now(), "item_count": len(items),
                    "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"Providers ok: {ok_count}/{len(PROVIDERS)} | changes: {len(items)} "
          f"-> data/status_items.json")
    return 0 if ok_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
