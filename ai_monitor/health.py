"""
Patrol health report - objective evidence for the 2-week burn-in gate
(user directive 2026-11-16: site stays unpublished until the local
system runs >= 2 weeks stably).

Parses logs/patrol.log (append-only, timestamped) and prints:
  - total patrols, per-step OK/FAIL/TIMEOUT/SKIP counts
  - patrol success rate and median duration
  - failures timeline (what broke, when)
  - current queue depth (bilingual coverage, article count)
  - ALERT.txt presence

Usage: python health.py          (console summary)
       python health.py --json   (machine-readable)
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent
PATROL_LOG = BASE_DIR / "logs" / "patrol.log"
ALERT_FILE = BASE_DIR / "logs" / "ALERT.txt"
FEED_FILE = BASE_DIR / "site" / "data" / "feed.json"
ARTICLES_DIR = BASE_DIR / "site" / "articles"

LINE_RE = re.compile(r"\[(?P<ts>[^\]]+)\] (?P<msg>.+)")


def main() -> int:
    if sys.stdout is not None and sys.stdout.encoding \
            and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    as_json = "--json" in sys.argv

    stats = {
        "patrols_started": 0, "patrols_completed": 0, "skipped_lock": 0,
        "step_ok": {}, "step_fail": {}, "step_timeout": {},
        "durations_s": [], "failures": [],
    }
    if PATROL_LOG.exists():
        for line in PATROL_LOG.read_text(encoding="utf-8").splitlines():
            match = LINE_RE.match(line)
            if not match:
                continue
            msg = match.group("msg")
            if msg.startswith("PATROL START"):
                stats["patrols_started"] += 1
            elif msg.startswith("PATROL COMPLETE in"):
                stats["patrols_completed"] += 1
                dur = re.search(r"in (\d+)s", msg)
                if dur:
                    stats["durations_s"].append(int(dur.group(1)))
            elif msg.startswith("SKIP: another patrol"):
                stats["skipped_lock"] += 1
            elif msg.startswith("OK "):
                stats["step_ok"][msg[3:]] = stats["step_ok"].get(msg[3:], 0) + 1
            elif msg.startswith("FAIL "):
                step = msg.split()[1].split("(")[0]
                stats["step_fail"][step] = stats["step_fail"].get(step, 0) + 1
                stats["failures"].append(f"{match.group('ts')} {msg[:90]}")
            elif msg.startswith("TIMEOUT "):
                step = msg.split()[1]
                stats["step_timeout"][step] = stats["step_timeout"].get(step, 0) + 1
                stats["failures"].append(f"{match.group('ts')} {msg[:90]}")
            elif msg.startswith("PATROL FAIL"):
                stats["failures"].append(f"{match.group('ts')} {msg[:90]}")

    durations = sorted(stats["durations_s"])
    median_dur = durations[len(durations) // 2] if durations else None

    feed_items = bilingual = article_count = 0
    feed_age_hours = None
    if FEED_FILE.exists():
        feed = json.loads(FEED_FILE.read_text(encoding="utf-8"))
        feed_items = feed.get("item_count", 0)
        bilingual = sum(1 for i in feed.get("items", [])
                        if i.get("headline_zh"))
        if feed.get("generated_at"):
            generated = dt.datetime.fromisoformat(feed["generated_at"])
            feed_age_hours = round(
                (dt.datetime.now(dt.timezone.utc) - generated).total_seconds() / 3600, 1)
    if ARTICLES_DIR.exists():
        article_count = sum(
            1 for meta in ARTICLES_DIR.glob("*.meta.json")
            if "fingerprint" in json.loads(meta.read_text(encoding="utf-8")))

    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "patrols": {"started": stats["patrols_started"],
                    "completed": stats["patrols_completed"],
                    "skipped_lock": stats["skipped_lock"],
                    "success_rate": (round(stats["patrols_completed"]
                                           / stats["patrols_started"] * 100, 1)
                                     if stats["patrols_started"] else None),
                    "median_duration_s": median_dur},
        "steps": {"ok": stats["step_ok"], "fail": stats["step_fail"],
                  "timeout": stats["step_timeout"]},
        "content": {"feed_items": feed_items, "bilingual": bilingual,
                    "articles": article_count, "feed_age_hours": feed_age_hours},
        "alert_present": ALERT_FILE.exists(),
        "recent_failures": stats["failures"][-10:],
    }

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    p = report["patrols"]
    print("=" * 62)
    print("PATROL HEALTH REPORT (burn-in evidence)")
    print("=" * 62)
    print(f"patrols: {p['completed']} completed / {p['started']} started "
          f"| success {p['success_rate']}% | skipped(lock) {p['skipped_lock']}")
    print(f"median duration: {p['median_duration_s']}s")
    print(f"steps ok : {report['steps']['ok']}")
    bad: dict = {**report["steps"]["fail"], **report["steps"]["timeout"]}
    print(f"steps bad: {bad or '{}'}")
    c = report["content"]
    print(f"content: feed {c['feed_items']} ({c['bilingual']} bilingual) | "
          f"articles {c['articles']} | feed age {c['feed_age_hours']}h")
    print(f"ALERT.txt present: {report['alert_present']}")
    if report["recent_failures"]:
        print("recent failures:")
        for f in report["recent_failures"]:
            print(f"  {f}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
