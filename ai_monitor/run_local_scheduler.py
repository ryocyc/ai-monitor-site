from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import subprocess
import sys
import time


BASE_DIR = pathlib.Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
RUNNER_LOG = LOG_DIR / "overnight_runner.log"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def append_log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{utc_now()}] {message}"
    print(line, flush=True)
    with RUNNER_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def run_step(label: str, args: list[str], timeout_seconds: int | None = None) -> bool:
    append_log(f"START {label}: {' '.join(args)}")
    try:
        result = subprocess.run(args, cwd=BASE_DIR.parent, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        append_log(f"TIMEOUT {label} after {timeout_seconds}s")
        return False
    if result.returncode == 0:
        append_log(f"OK {label}")
        return True
    append_log(f"FAIL {label} (exit={result.returncode})")
    return False


def run_monitor() -> bool:
    return run_step("monitor", [sys.executable, str(BASE_DIR / "monitor.py")], timeout_seconds=240)


def run_publish_pipeline() -> bool:
    steps = [
        ("publish-initial", [sys.executable, str(BASE_DIR / "publish_site.py"), "--limit", "10"], 120),
        ("enrich-targets-latest", [sys.executable, str(BASE_DIR / "enrich_targets.py"), "--input", str(BASE_DIR / "site" / "data" / "latest.json"), "--limit", "10"], 180),
        ("enrich-targets-archive", [sys.executable, str(BASE_DIR / "enrich_targets.py"), "--input", str(BASE_DIR / "site" / "data" / "archive.json"), "--limit", "50"], 240),
        ("claude-latest", [sys.executable, str(BASE_DIR / "enrich_with_claude.py"), "--input", str(BASE_DIR / "site" / "data" / "latest.json"), "--max-budget-usd", "1.0"], 360),
        ("claude-archive", [sys.executable, str(BASE_DIR / "enrich_with_claude.py"), "--input", str(BASE_DIR / "site" / "data" / "archive.json"), "--max-budget-usd", "3.0"], 600),
        ("publish-final", [sys.executable, str(BASE_DIR / "publish_site.py"), "--limit", "10"], 120),
    ]
    ok = True
    for label, args, timeout_seconds in steps:
        ok = run_step(label, args, timeout_seconds=timeout_seconds) and ok
    return ok


def run_publish_github() -> bool:
    return run_step("publish-github", [sys.executable, str(BASE_DIR / "publish_to_github.py")], timeout_seconds=60)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local AI monitor on a timer for overnight testing.")
    parser.add_argument("--monitor-seconds", type=int, default=300, help="How often to run a monitor pass. Default: 300")
    parser.add_argument("--publish-seconds", type=int, default=900, help="How often to rebuild/enrich/publish the site. Default: 900")
    parser.add_argument("--duration-hours", type=float, default=10.0, help="How long to keep running before exiting. Default: 10")
    parser.add_argument("--skip-initial-publish", action="store_true", help="Skip the immediate publish cycle on startup")
    parser.add_argument("--publish-github", action="store_true", help="After site update, also push to GitHub (requires git remote to be configured)")
    args = parser.parse_args()

    end_time = time.monotonic() + max(args.duration_hours, 0) * 3600
    next_monitor = 0.0
    next_publish = 0.0 if not args.skip_initial_publish else time.monotonic() + args.publish_seconds

    append_log(
        f"RUNNER START monitor={args.monitor_seconds}s publish={args.publish_seconds}s duration={args.duration_hours}h publish-github={args.publish_github}"
    )

    try:
        while time.monotonic() < end_time:
            now = time.monotonic()
            if now >= next_monitor:
                run_monitor()
                next_monitor = now + max(args.monitor_seconds, 30)

            if now >= next_publish:
                ok = run_publish_pipeline()
                if args.publish_github:
                    ok = run_publish_github() and ok
                next_publish = now + max(args.publish_seconds, 60)

            time.sleep(5)
    except KeyboardInterrupt:
        append_log("RUNNER STOP requested by user")
        return 130

    append_log("RUNNER COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
