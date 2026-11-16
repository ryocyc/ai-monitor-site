"""
Patrol orchestrator: run all pillar collectors + feed build in one bounded
pass. Designed for schtasks scheduling (2h interval).

Hygiene rules honoured (workspace AGENTS.md):
  - single-instance lock (TCP port bind, exits immediately if already running)
  - per-step timeouts, total pass bounded well under the schedule interval
  - progress log with timestamps (logs/patrol.log)
  - console subprocesses use CREATE_NO_WINDOW (no flashing windows)
  - any failed step -> logs/ALERT.txt (removed when a pass succeeds)

Steps:
  1. discover_trending.py   GitHub exploding AI repos   (pillar 5)
  2. discover_hn.py         HN early signal             (pillar 5)
  3. collect/collect_status.py   provider outages       (pillar 4)
  4. collect/collect_prices.py   API price changes      (pillar 3)
  5. collect/collect_feeds.py    releases + news feeds  (pillar 1)
  6. collect/collect_rankings.py LMArena top-10 diff    (pillar 2, stealth)
  7. build_feed.py          merge -> site/data/feed.json
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import socket
import subprocess
import sys
import time

BASE_DIR = pathlib.Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
ALERT_FILE = LOG_DIR / "ALERT.txt"
PATROL_LOG = LOG_DIR / "patrol.log"

LOCK_HOST = "127.0.0.1"
LOCK_PORT = 8615

# (label, argv relative to BASE_DIR, timeout_seconds)
STEPS = [
    ("trending",  [sys.executable, str(BASE_DIR / "discover_trending.py")], 120),
    # HN source removed (user directive 2026-11-16): HN is itself a news
    # aggregator - reposting its content adds no value for our readers.
    ("status",    [sys.executable, str(BASE_DIR / "collect" / "collect_status.py")], 90),
    ("prices",    [sys.executable, str(BASE_DIR / "collect" / "collect_prices.py")], 120),
    ("feeds",     [sys.executable, str(BASE_DIR / "collect" / "collect_feeds.py")], 180),
    ("rankings",  [sys.executable, str(BASE_DIR / "collect" / "collect_rankings.py")], 180),
    ("build",     [sys.executable, str(BASE_DIR / "build_feed.py")],        60),
    ("enrich",    [sys.executable, str(BASE_DIR / "build" / "enrich.py"),
                 "--max-calls", "20"],                                     900),
    ("articles",  [sys.executable, str(BASE_DIR / "build" / "generate_articles.py"),
                 "--limit", "6"],                                         1200),
]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def log(message: str) -> None:
    line = f"[{utc_now()}] {message}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with PATROL_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def acquire_lock() -> socket.socket | None:
    """Bind the lock port; None means another patrol is already running."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((LOCK_HOST, LOCK_PORT))
        return sock
    except OSError:
        return None


def run_step(label: str, args: list[str], timeout: int) -> tuple[bool, str]:
    log(f"START {label}")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(args, cwd=BASE_DIR, capture_output=True,
                                timeout=timeout, creationflags=creationflags)
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT {label} after {timeout}s")
        return False, f"timeout after {timeout}s"
    stdout_tail = (result.stdout or b"").decode("utf-8", errors="replace").strip()[-400:]
    if result.returncode == 0:
        log(f"OK {label}")
        return True, stdout_tail
    stderr_tail = (result.stderr or b"").decode("utf-8", errors="replace").strip()[-400:]
    log(f"FAIL {label} (exit={result.returncode}) {stderr_tail[:200]}")
    return False, stderr_tail or stdout_tail


def main() -> int:
    # Under schtasks there is no console: sys.stdout is None and touching
    # .encoding crashes before the first log line (caused exit-1 on every
    # scheduled run while manual runs worked). Guard everything.
    if sys.stdout is not None and sys.stdout.encoding \
            and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    lock = acquire_lock()
    if lock is None:
        log("SKIP: another patrol instance is running (port locked).")
        return 0

    started = time.monotonic()
    log(f"PATROL START ({len(STEPS)} steps)")

    failures: list[tuple[str, str]] = []
    for label, args, timeout in STEPS:
        ok, tail = run_step(label, args, timeout)
        if not ok:
            failures.append((label, tail))

    elapsed = time.monotonic() - started
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if failures:
        alert_lines = [f"PATROL FAILED at {utc_now()} ({elapsed:.0f}s)",
                       "=" * 50]
        for label, tail in failures:
            alert_lines.append(f"STEP {label}:")
            alert_lines.append(tail[:800])
            alert_lines.append("-" * 50)
        ALERT_FILE.write_text("\n".join(alert_lines), encoding="utf-8")
        log(f"PATROL FAIL ({len(failures)} step(s) failed) in {elapsed:.0f}s "
            f"-> logs/ALERT.txt written")
    else:
        if ALERT_FILE.exists():
            ALERT_FILE.unlink()
            log("previous ALERT cleared (this pass succeeded)")
        log(f"PATROL COMPLETE in {elapsed:.0f}s - feed updated")

    lock.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
