from __future__ import annotations

import datetime as dt
import pathlib
import subprocess
import sys


BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
SITE_DIR = BASE_DIR / "ai_monitor" / "site"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def run_git(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git"] + args,
        cwd=BASE_DIR,
        capture_output=True,
        timeout=timeout,
    )


def check_repo_ready() -> tuple[bool, str]:
    """Check that the repo has a remote configured. Returns (ready, message)."""
    result = run_git(["rev-parse", "--is-inside-work-tree"])
    if result.returncode != 0:
        return False, "Not a git repository. Run 'git init' first."

    result = run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    current_branch = result.stdout.decode().strip()

    result = run_git(["config", "--get", "remote.origin.url"])
    remote_url = result.stdout.decode().strip()
    if not remote_url:
        return False, (
            f"No remote.origin.url configured. "
            f"Add one with: git remote add origin https://github.com/<user>/<repo>.git"
        )
    return True, f"Ready - branch '{current_branch}', remote: {remote_url}"


def publish() -> int:
    ready, msg = check_repo_ready()
    print(msg, flush=True)
    if not ready:
        print("ERROR: Cannot publish - git remote not configured.", file=sys.stderr)
        print("FIX: Configure git remote before publishing.", file=sys.stderr)
        return 1

    if not SITE_DIR.exists():
        print(f"ERROR: Site directory {SITE_DIR} does not exist.", file=sys.stderr)
        return 1

    # Stage site files (--force bypasses gitignore so local preview publishing works)
    result = run_git(["add", "--force", str(SITE_DIR.relative_to(BASE_DIR))])
    if result.returncode != 0:
        print(f"ERROR: git add failed: {result.stderr.decode()}", file=sys.stderr)
        return 1

    # Check if there are staged changes
    result = run_git(["diff", "--cached", "--name-only"])
    staged_files = result.stdout.decode().strip()
    if not staged_files:
        print("No site changes to publish.")
        return 0

    timestamp = utc_now()
    commit_msg = f"Site publish {timestamp}"

    result = run_git(["commit", "-m", commit_msg])
    if result.returncode != 0:
        print(f"ERROR: git commit failed: {result.stderr.decode()}", file=sys.stderr)
        return 1

    print(f"Committed: {commit_msg}", flush=True)

    result = run_git(["push", "origin", "HEAD"])
    if result.returncode != 0:
        print(f"ERROR: git push failed: {result.stderr.decode()}", file=sys.stderr)
        return 1

    print("Successfully pushed site to GitHub.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(publish())
