from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any


BASE_DIR = pathlib.Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
SNAPSHOT_DIR = STATE_DIR / "snapshots"
LOG_DIR = BASE_DIR / "logs"
EVENT_LOG = LOG_DIR / "events.jsonl"
STATUS_LOG = LOG_DIR / "status.log"
DEFAULT_TIMEOUT = 20
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36 AI-News-Monitor/0.1"
)


@dataclass
class FetchResult:
    content: str
    content_type: str
    etag: str | None
    last_modified: str | None


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def ensure_dirs() -> None:
    for path in (STATE_DIR, SNAPSHOT_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def read_json(path: pathlib.Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: pathlib.Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def append_line(path: pathlib.Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip() + "\n")


def fetch_url(url: str, headers: dict[str, str] | None = None, timeout: int = DEFAULT_TIMEOUT) -> FetchResult:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        request_headers.update(headers)

    request = urllib.request.Request(
        url,
        headers=request_headers,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        content = raw.decode(charset, errors="replace")
        return FetchResult(
            content=content,
            content_type=response.headers.get_content_type(),
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )


def apply_include_patterns(text: str, patterns: list[str]) -> str:
    matched: list[str] = []
    for pattern in patterns:
        found = re.findall(pattern, text, flags=re.IGNORECASE)
        matched.extend(found)
    return "\n".join(matched) if matched else text


def normalize_html(html: str) -> str:
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<!--[\s\S]*?-->", " ", html)
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"&nbsp;|&#160;", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"&amp;", "&", html, flags=re.IGNORECASE)
    html = re.sub(r"\s+", " ", html)
    return html.strip()


def normalize_feed(xml_text: str, max_entries: int = 20) -> str:
    try:
      root = ET.fromstring(xml_text)
    except ET.ParseError:
      return xml_text.strip()

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "rss": "",
    }

    entries: list[str] = []
    atom_entries = root.findall(".//atom:entry", ns)
    if atom_entries:
        for entry in atom_entries[:max_entries]:
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            updated = (entry.findtext("atom:updated", default="", namespaces=ns) or "").strip()
            summary = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
            content = (entry.findtext("atom:content", default="", namespaces=ns) or "").strip()
            link_node = entry.find("atom:link", ns)
            link = link_node.get("href", "").strip() if link_node is not None else ""
            normalized_content = normalize_html(content) if content else ""
            entries.append(
                f"title={title}\nupdated={updated}\nlink={link}\nsummary={summary}\ncontent={normalized_content}"
            )
    else:
        for item in root.findall(".//item")[:max_entries]:
            title = (item.findtext("title", default="") or "").strip()
            pub_date = (item.findtext("pubDate", default="") or "").strip()
            description = (item.findtext("description", default="") or "").strip()
            link = (item.findtext("link", default="") or "").strip()
            entries.append(f"title={title}\npublished={pub_date}\nlink={link}\ndescription={description}")

    return "\n\n".join(entries).strip()


def extract_content(source: dict[str, Any], fetched: FetchResult) -> str:
    text = fetched.content
    source_type = source.get("type", "html")
    extract = source.get("extract", {})
    patterns = extract.get("include_patterns", [])

    if source_type == "html":
        if patterns:
            text = apply_include_patterns(text, patterns)
        text = normalize_html(text)
    elif source_type == "rss":
        text = normalize_feed(text)

    return text


def compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def trim_diff(previous: str, current: str, max_lines: int = 40) -> list[str]:
    diff = list(
        difflib.unified_diff(
            previous.splitlines(),
            current.splitlines(),
            fromfile="previous",
            tofile="current",
            lineterm="",
            n=2,
        )
    )
    if len(diff) <= max_lines:
        return diff
    head = diff[: max_lines // 2]
    tail = diff[-max_lines // 2 :]
    return head + ["... DIFF TRUNCATED ..."] + tail


def snapshot_path(source_id: str) -> pathlib.Path:
    return SNAPSHOT_DIR / f"{source_id}.txt"


def metadata_path(source_id: str) -> pathlib.Path:
    return STATE_DIR / f"{source_id}.json"


def load_sources(path: pathlib.Path) -> list[dict[str, Any]]:
    payload = read_json(path, default=[])
    if not isinstance(payload, list):
        raise ValueError("sources.json must contain a JSON array")
    return payload


def monitor_source(source: dict[str, Any]) -> dict[str, Any]:
    source_id = source["id"]
    source_name = source.get("name", source_id)
    url = source["url"]
    effective_url = url
    try:
        fetched = fetch_url(url, headers=source.get("headers"))
    except urllib.error.HTTPError as exc:
        fallback_url = source.get("fallback_url")
        if exc.code == 403 and fallback_url:
            fetched = fetch_url(fallback_url, headers=source.get("headers"))
            effective_url = fallback_url
        else:
            raise
    extracted = extract_content(source, fetched)
    current_hash = compute_hash(extracted)

    snapshot_file = snapshot_path(source_id)
    metadata_file = metadata_path(source_id)
    previous_snapshot = snapshot_file.read_text(encoding="utf-8") if snapshot_file.exists() else ""
    previous_metadata = read_json(metadata_file, default={})
    previous_hash = previous_metadata.get("content_hash")
    changed = previous_hash != current_hash

    metadata = {
        "id": source_id,
        "name": source_name,
        "url": url,
        "effective_url": effective_url,
        "content_hash": current_hash,
        "etag": fetched.etag,
        "last_modified": fetched.last_modified,
        "checked_at": utc_now(),
        "changed": changed,
    }

    write_json(metadata_file, metadata)

    if not snapshot_file.exists() or changed:
        snapshot_file.write_text(extracted, encoding="utf-8")

    if changed:
        event = {
            "timestamp": utc_now(),
            "source_id": source_id,
            "source_name": source_name,
            "url": url,
            "effective_url": effective_url,
            "previous_hash": previous_hash,
            "current_hash": current_hash,
            "etag": fetched.etag,
            "last_modified": fetched.last_modified,
            "diff_excerpt": trim_diff(previous_snapshot, extracted),
        }
        append_line(EVENT_LOG, json.dumps(event, ensure_ascii=False))
        append_line(
            STATUS_LOG,
            f"[{event['timestamp']}] CHANGE {source_name} ({source_id}) -> {url}",
        )
    else:
        append_line(
            STATUS_LOG,
            f"[{metadata['checked_at']}] NO_CHANGE {source_name} ({source_id})",
        )

    return metadata


def run_once(sources_path: pathlib.Path) -> int:
    ensure_dirs()
    sources = load_sources(sources_path)
    failures = 0
    for source in sources:
        try:
            result = monitor_source(source)
            print(
                f"{result['checked_at']} | {'CHANGED' if result['changed'] else 'OK':7} | {result['name']}"
            )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
            failures += 1
            timestamp = utc_now()
            append_line(
                STATUS_LOG,
                f"[{timestamp}] ERROR {source.get('name', source.get('id', 'unknown'))}: {exc}",
            )
            print(f"{timestamp} | ERROR   | {source.get('name', source.get('id', 'unknown'))} | {exc}")
    return 1 if failures else 0


def run_loop(sources_path: pathlib.Path, interval_seconds: int) -> int:
    print(f"Monitoring {sources_path} every {interval_seconds} seconds. Press Ctrl+C to stop.")
    while True:
        exit_code = run_once(sources_path)
        if exit_code:
            print("One or more sources failed in this cycle. Check logs/status.log for details.")
        time.sleep(interval_seconds)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor AI source pages and log meaningful changes.")
    parser.add_argument(
        "--sources",
        type=pathlib.Path,
        default=BASE_DIR / "sources.json",
        help="Path to the sources JSON config.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=180,
        help="Polling interval in seconds when --loop is enabled. Default: 180 (3 minutes).",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously instead of a single pass.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.loop:
        return run_loop(args.sources, args.interval)
    return run_once(args.sources)


if __name__ == "__main__":
    raise SystemExit(main())
