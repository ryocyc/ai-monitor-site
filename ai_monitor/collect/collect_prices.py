"""
Pillar 3 (pricing) collector: diff the LiteLLM community model-price JSON
(github raw, verified 2026-11-16) against the previous snapshot and emit feed
items for API price changes.

Why this source: structured JSON, zero scraping, zero ban risk; maintained by
the LiteLLM community and effectively a rolling registry of model API prices.

Output: data/prices_items.json
State:  state/prices_snapshot.json  (gitignored - 4MB)
First run seeds the snapshot and emits nothing.
Exit 1 if the download fails (patrol alerting).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import sys
import urllib.request

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATE_DIR = BASE_DIR / "state"
SNAPSHOT_FILE = STATE_DIR / "prices_snapshot.json"

PRICES_URL = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
              "model_prices_and_context_window.json")
USER_AGENT = "AI-News-Monitor/1.0 (+https://aisignalfeed.com)"
REQUEST_TIMEOUT = 60

COST_FIELDS = {"input": "input_cost_per_token", "output": "output_cost_per_token"}
CHANGE_THRESHOLD = 0.05   # ignore <5% moves (float noise / rounding)
MAX_ITEMS = 10


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def fetch_prices() -> dict:
    request = urllib.request.Request(PRICES_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    return payload


def compact_snapshot(payload: dict) -> dict:
    """Keep only the fields we diff, so the 4MB snapshot stays small (~1MB)."""
    compact = {}
    for model, info in payload.items():
        if not isinstance(info, dict):
            continue
        entry = {}
        for field in COST_FIELDS.values():
            value = info.get(field)
            if isinstance(value, (int, float)):
                entry[field] = value
        if entry:
            compact[model] = entry
    return compact


def diff_prices(previous: dict, current: dict) -> list[dict]:
    changes = []
    for model, now in current.items():
        before = previous.get(model)
        if not before or not isinstance(before, dict):
            continue
        for direction, field in COST_FIELDS.items():
            old = before.get(field)
            new = now.get(field)
            if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
                continue
            if old <= 0 or abs(new - old) / old < CHANGE_THRESHOLD:
                continue
            pct = (new - old) / old * 100.0
            changes.append({
                "model": model,
                "direction": direction,
                "old": old,
                "new": new,
                "pct": round(pct, 1),
            })
    changes.sort(key=lambda c: abs(c["pct"]), reverse=True)
    return changes[:MAX_ITEMS]


def make_item(change: dict) -> dict:
    model = change["model"]
    moved = change["pct"]
    verb = "cut" if moved < 0 else "raised"
    per_million = lambda per_token: per_token * 1_000_000
    old_str = f"${per_million(change['old']):,.2f}"
    new_str = f"${per_million(change['new']):,.2f}"
    headline = (f"{model} API {change['direction']} price {verb} {abs(moved):.0f}% "
                f"({old_str} -> {new_str} per 1M {change['direction']} tokens)")
    summary = (
        f"The LiteLLM community price registry shows {model}'s {change['direction']} price "
        f"moved from {old_str} to {new_str} per 1M tokens ({moved:+.1f}%). "
        f"Worth checking before your next API bill."
    )
    return {
        "id": "pr-" + hashlib.sha1(f"{model}|{change['direction']}|{change['new']}".encode()).hexdigest()[:12],
        "timestamp": utc_now(),
        "category": "pricing",
        "source_name": "LiteLLM Price Registry",
        "source_url": "https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json",
        "target_url": f"https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json",
        "headline_en": headline,
        "summary_en": summary,
        "metrics": {"pct": change["pct"], "old_per_m": change["old"] * 1_000_000,
                    "new_per_m": change["new"] * 1_000_000},
    }


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        payload = fetch_prices()
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print(f"[error] price registry download failed: {exc}", file=sys.stderr)
        return 1

    current = compact_snapshot(payload)
    previous: dict = {}
    seeded = False
    if SNAPSHOT_FILE.exists():
        try:
            previous = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
    else:
        seeded = True

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")

    if seeded:
        items = []
        print(f"SEED: recorded {len(current)} model prices, no items emitted.")
    else:
        changes = diff_prices(previous, current)
        items = [make_item(c) for c in changes]
        for c in changes:
            print(f"  {c['model']}: {c['direction']} {c['pct']:+.1f}%")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "prices_items.json").write_text(
        json.dumps({"generated_at": utc_now(), "item_count": len(items), "items": items},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Models tracked: {len(current)} | price changes: {len(items)} "
          f"-> data/prices_items.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
