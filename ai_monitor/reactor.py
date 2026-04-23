from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import urllib.request


BASE_DIR = pathlib.Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
STATE_DIR = BASE_DIR / "state"
EVENT_LOG = LOG_DIR / "events.jsonl"
REACTOR_STATE = STATE_DIR / "reactor_state.json"
REACTIONS_LOG = LOG_DIR / "reactions.jsonl"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: pathlib.Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: pathlib.Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def append_line(path: pathlib.Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip() + "\n")


def safe_print(text: str) -> None:
    encoding = sys.stdout.encoding or "utf-8"
    sys.stdout.write(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))
    sys.stdout.write("\n")


def load_new_events(limit: int) -> list[dict]:
    state = read_json(REACTOR_STATE, {"last_seen_timestamp": None})
    last_seen = state.get("last_seen_timestamp")
    events: list[dict] = []
    if not EVENT_LOG.exists():
        return events

    for line in EVENT_LOG.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        timestamp = event.get("timestamp")
        if last_seen and timestamp <= last_seen:
            continue
        events.append(event)

    return events[:limit]


def build_prompt(events: list[dict]) -> str:
    compact_events = []
    for event in events:
        compact_events.append(
            {
                "timestamp": event.get("timestamp"),
                "source_id": event.get("source_id"),
                "source_name": event.get("source_name"),
                "url": event.get("url"),
                "effective_url": event.get("effective_url"),
                "diff_excerpt": event.get("diff_excerpt", [])[:20],
            }
        )

    return (
        "You are an AI news triage assistant.\n"
        "Classify each event as one of: ignore, watch, publish.\n"
        "If publish, provide a 1-sentence headline and a 2-sentence summary.\n"
        "Be conservative. Ignore cosmetic page changes.\n"
        "Return strict JSON with a top-level 'events' array.\n\n"
        f"EVENTS:\n{json.dumps(compact_events, ensure_ascii=False, indent=2)}"
    )


def call_openai_compatible(prompt: str) -> dict:
    api_base = os.environ.get("LLM_API_BASE")
    api_key = os.environ.get("LLM_API_KEY")
    model = os.environ.get("LLM_MODEL")

    if not api_base or not api_key or not model:
        raise RuntimeError("Set LLM_API_BASE, LLM_API_KEY, and LLM_MODEL to enable live AI reactions.")

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are precise, conservative, and return valid JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }

    request = urllib.request.Request(
        api_base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="React to change events with an LLM only after changes are detected.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum number of new events to review.")
    parser.add_argument("--dry-run", action="store_true", help="Print the prompt instead of calling an LLM.")
    args = parser.parse_args()

    events = load_new_events(args.limit)
    if not events:
        print("No new events to react to.")
        return 0

    prompt = build_prompt(events)

    if args.dry_run:
        safe_print(prompt)
        result = {"mode": "dry-run", "prompt_preview": prompt[:4000]}
    else:
        result = call_openai_compatible(prompt)

    record = {
        "timestamp": utc_now(),
        "event_count": len(events),
        "result": result,
    }
    append_line(REACTIONS_LOG, json.dumps(record, ensure_ascii=False))
    write_json(REACTOR_STATE, {"last_seen_timestamp": events[-1]["timestamp"]})
    print(f"Processed {len(events)} event(s). Logged reaction to {REACTIONS_LOG}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
