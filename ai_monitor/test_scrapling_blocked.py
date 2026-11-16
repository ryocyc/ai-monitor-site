"""
One-time engine test: can Scrapling fetch the sources urllib was blocked on?

Targets (from data/rss_probe_report.json, 2026-11-16):
  - OpenAI Newsroom    urllib 403 (Cloudflare)
  - Perplexity Blog    urllib 403 (Cloudflare)
  - Cohere Blog        urllib network error (curl 200 -> Python TLS fingerprint block)
  - Microsoft AI Blog  urllib network error (curl 301 -> Cloudflare front)

Escalation ladder (per scrapling skill, v0.5.1):
  1. Fetcher with realistic browser headers  (light, no browser)
  2. StealthyFetcher.fetch(headless=True, block_images=True)  (auto opens AND
     closes a stealth Chrome per call -> no zombie-browser risk)
  3. if still blocked: record as needs-RSS/manual, do NOT hard-ram

Output:
  data/scrapling_engine_test.json  per-source verdict + evidence
Console: verdict table + chrome-process delta (hygiene proof).
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import time

from scrapling import Fetcher, StealthyFetcher

BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}

TARGETS = [
    ("OpenAI Newsroom",   "https://openai.com/newsroom/"),
    ("Perplexity Blog",   "https://www.perplexity.ai/hub/blog"),
    ("Cohere Blog",       "https://cohere.com/blog"),
    ("Microsoft AI Blog", "https://blogs.microsoft.com/ai/"),
]

POLITE_DELAY_SECONDS = 3.0
STEALTH_TIMEOUT_MS = 45000


def chrome_process_count() -> int | None:
    """Count chrome-family processes (Windows) for hygiene verification."""
    import subprocess
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process | Where-Object {$_.ProcessName -match 'chrome|chromium|headless'}).Count"],
            capture_output=True, timeout=30, creationflags=creationflags)
        return int(result.stdout.decode().strip() or 0)
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
        return None


def try_fetcher(url: str):
    """Attempt 1: plain HTTP with realistic headers. Returns page or None."""
    try:
        page = Fetcher(auto_match=False).get(url, headers=BROWSER_HEADERS, timeout=20)
        return page if page.status == 200 else None
    except Exception as exc:  # noqa: BLE001
        print(f"    [fetcher] failed: {type(exc).__name__}: {str(exc)[:100]}")
        return None


def try_stealthy(url: str):
    """Attempt 2: stealth browser. fetch() opens+closes per call (no zombies)."""
    try:
        page = StealthyFetcher.fetch(url, headless=True, block_images=True,
                                     timeout=STEALTH_TIMEOUT_MS)
        return page if page.status == 200 else None
    except Exception as exc:  # noqa: BLE001
        print(f"    [stealthy] failed: {type(exc).__name__}: {str(exc)[:100]}")
        return None


def extract_evidence(page) -> dict:
    """Pull title + top article-ish links as proof the content is usable."""
    evidence: dict = {"title": "", "sample_links": []}
    try:
        evidence["title"] = (page.css_first("title").text or "").strip()[:120]
    except Exception:  # noqa: BLE001
        pass
    try:
        seen: set[str] = set()
        for a in page.css("a[href]"):
            href = (a.attrib or {}).get("href", "")
            text = (a.text or "").strip()
            if not href or not text or len(text) < 25:
                continue
            if href in seen:
                continue
            seen.add(href)
            evidence["sample_links"].append({"text": text[:90], "href": href[:160]})
            if len(evidence["sample_links"]) >= 3:
                break
    except Exception:  # noqa: BLE001
        pass
    return evidence


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    processes_before = chrome_process_count()
    print(f"Chrome-family processes before: {processes_before}")
    started = time.monotonic()

    results = []
    for name, url in TARGETS:
        print(f"[{name}] {url}")
        verdict = {
            "name": name, "url": url,
            "fetcher_status": None, "stealthy_status": None,
            "engine": None, "usable": False,
        }

        page = try_fetcher(url)
        if page is not None:
            verdict["fetcher_status"] = page.status
            verdict["engine"] = "Fetcher"
            verdict["usable"] = True
            verdict["evidence"] = extract_evidence(page)
            print(f"    Fetcher -> {page.status} OK (no browser needed)")
        else:
            print("    Fetcher -> blocked, escalating to StealthyFetcher...")
            page = try_stealthy(url)
            if page is not None:
                verdict["stealthy_status"] = page.status
                verdict["engine"] = "StealthyFetcher"
                verdict["usable"] = True
                verdict["evidence"] = extract_evidence(page)
                print(f"    StealthyFetcher -> {page.status} OK (browser, auto-closed)")
            else:
                verdict["engine"] = "none"
                print("    BLOCKED on both engines -> mark as needs-RSS/manual")

        results.append(verdict)
        time.sleep(POLITE_DELAY_SECONDS)

    processes_after = chrome_process_count()
    elapsed = time.monotonic() - started

    print("-" * 72)
    for r in results:
        engine = r["engine"] or "none"
        ok = "USABLE" if r["usable"] else "BLOCKED"
        print(f"{ok:8} | via {engine:15} | {r['name']}")
    print("-" * 72)
    print(f"Chrome-family processes after: {processes_after} "
          f"(before: {processes_before})")
    print(f"Total elapsed: {elapsed:.0f}s")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "scrapling_engine_test.json").write_text(
        json.dumps({
            "generated_at": dt.datetime.now(dt.timezone.utc)
            .replace(microsecond=0).isoformat(),
            "chrome_processes_before": processes_before,
            "chrome_processes_after": processes_after,
            "elapsed_seconds": round(elapsed),
            "results": results,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print("Wrote data/scrapling_engine_test.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
