"""
Pillar 2 (rankings/strength) collector: watch the LMArena leaderboard
(arena.ai - moved from lmarena.ai, confirmed 301) and emit items when the
top-10 ordering changes materially.

Reality check (2026-11-16): the page is JS-rendered, so this collector uses
Scrapling StealthyFetcher (auto opens AND closes the browser per call -
verified leak-free in test_scrapling_blocked.py). Extraction is defensive:
known model-name families + Elo-range numbers; if nothing parses, we keep
the previous state and exit 0 (a scraping miss must NOT page the user).

Output: data/rankings_items.json
State:  state/rankings.json
Debug:  logs/rankings_dump.txt (gitignored) - visible text of the page
First run seeds the snapshot and emits nothing.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import warnings

warnings.filterwarnings("ignore")

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATE_DIR = BASE_DIR / "state"
STATE_FILE = STATE_DIR / "rankings.json"
DUMP_FILE = BASE_DIR / "logs" / "rankings_dump.txt"

LEADERBOARD_URL = "https://arena.ai/leaderboard"
STEALTH_TIMEOUT_MS = 60000
SCORE_DELTA_THRESHOLD = 5  # Elo points; smaller moves are noise

MODEL_FAMILIES = [
    "GPT", "Claude", "Gemini", "Grok", "DeepSeek", "Kimi", "Qwen",
    "GLM", "Llama", "Mistral", "ERNIE", "Hunyuan", "Seed", "Nova",
    "Ma", "MiniMax", "Command", "Jamba", "o3", "o4",
]
MAX_RANKS = 10


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def fetch_visible_text() -> str:
    from scrapling import StealthyFetcher
    page = StealthyFetcher.fetch(LEADERBOARD_URL, headless=True,
                                 block_images=True, timeout=STEALTH_TIMEOUT_MS)
    if page.status != 200:
        raise RuntimeError(f"HTTP {page.status}")
    text = page.get_all_text()
    DUMP_FILE.parent.mkdir(parents=True, exist_ok=True)
    DUMP_FILE.write_text(text, encoding="utf-8")
    return text


RANK_LINE_RE = re.compile(r"^(\d{1,2})$")
PCT_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,2})?)%$")


def extract_top(text: str) -> list[dict]:
    """Parse the 'Top 10 Agents / Best Overall' table. Live-verified layout
    (2026-11-16): rank\n[org]\nmodel name\nNN.NN%  - consecutive rank lines."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    best_run: list[dict] = []
    i = 0
    while i < len(lines):
        match = RANK_LINE_RE.match(lines[i])
        if match and int(match.group(1)) == 1:
            run: list[dict] = []
            expected_rank = 1
            j = i + 1
            while j < len(lines) and expected_rank <= MAX_RANKS:
                # model name: within next 2 lines (optional org line first)
                name = None
                pct = None
                # entry layout: rank\n[org]\nmodel name\nNN.NN% -> scan a
                # 4-line window (org line is optional, e.g. GPT rows)
                for k in range(j, min(j + 4, len(lines))):
                    if name is None and (k == j or not RANK_LINE_RE.match(lines[k])) \
                            and any(f in lines[k] for f in MODEL_FAMILIES) \
                            and not PCT_RE.match(lines[k]):
                        name = lines[k]
                    pct_match = PCT_RE.match(lines[k])
                    if pct_match and name is not None:
                        pct = float(pct_match.group(1))
                        j = k + 1
                        break
                if name is None or pct is None:
                    break
                run.append({"name": name[:80], "score": pct})
                expected_rank += 1
                # advance to the next rank line
                while j < len(lines) and not RANK_LINE_RE.match(lines[j]):
                    j += 1
                if j < len(lines) and int(RANK_LINE_RE.match(lines[j]).group(1)) != expected_rank:
                    break
            if len(run) >= 5:
                best_run = run
                break
            i += 1
        else:
            i += 1
    return best_run


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def make_item(event: str, detail: str, prev: dict | None, curr: dict) -> dict:
    if event == "new_no1":
        headline = f"LMArena: {curr['name']} takes the #1 spot (Elo {curr['score']})"
        summary = (f"The community leaderboard has a new leader: {curr['name']} at "
                   f"{curr['score']} Elo, overtaking {prev['name']} ({prev['score']}).")
    else:  # climbed
        headline = f"LMArena: {curr['name']} now #{curr['rank']} (Elo {curr['score']})"
        summary = (f"{curr['name']} moved to rank {curr['rank']} on the community "
                   f"leaderboard (Elo {curr['score']}, was {prev['score'] if prev else 'n/a'}).")
    return {
        "id": "rk-" + hashlib.sha1(f"{event}|{curr['name']}|{curr['score']}".encode()).hexdigest()[:12],
        "timestamp": utc_now(),
        "category": "ranking",
        "source_name": "LMArena",
        "source_url": LEADERBOARD_URL,
        "target_url": LEADERBOARD_URL,
        "headline_en": headline,
        "summary_en": summary,
    }


def diff(previous_top: list[dict], current_top: list[dict]) -> list[dict]:
    items: list[dict] = []
    if not previous_top or not current_top:
        return items
    prev_no1, curr_no1 = previous_top[0], current_top[0]
    prev_names = {r["name"]: r for r in previous_top}

    if curr_no1["name"] != prev_no1["name"]:
        items.append(make_item("new_no1", "", prev_no1, {**curr_no1, "rank": 1}))

    for rank, row in enumerate(current_top, start=1):
        before = prev_names.get(row["name"])
        if before is None:
            items.append(make_item("climbed", "", before, {**row, "rank": rank}))
        elif abs(row["score"] - before["score"]) >= SCORE_DELTA_THRESHOLD and rank > 1:
            items.append(make_item("climbed", "", before, {**row, "rank": rank}))
    return items[:MAX_RANKS]


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        text = fetch_visible_text()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] leaderboard fetch failed (kept previous state): {exc}",
              file=sys.stderr)
        (DATA_DIR / "rankings_items.json").write_text(
            json.dumps({"generated_at": utc_now(), "item_count": 0, "items": []},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return 0  # scraping miss is not a patrol failure

    current_top = extract_top(text)
    state = load_state()
    previous_top = state.get("top", [])

    if not current_top:
        print(f"[warn] extraction empty (page structure changed? see {DUMP_FILE.name}); "
              f"state untouched.")
        (DATA_DIR / "rankings_items.json").write_text(
            json.dumps({"generated_at": utc_now(), "item_count": 0, "items": []},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    if not previous_top:
        items: list[dict] = []
        print(f"SEED: recorded top {len(current_top)}; no items emitted.")
    else:
        items = diff(previous_top, current_top)
        for item in items:
            print(f"  {item['headline_en']}")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(
        {"checked_at": utc_now(), "top": current_top}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "rankings_items.json").write_text(
        json.dumps({"generated_at": utc_now(), "item_count": len(items), "items": items},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Top-{'10'} snapshot: {current_top[0]['name'] if current_top else '?'} "
          f"| changes: {len(items)} -> data/rankings_items.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
