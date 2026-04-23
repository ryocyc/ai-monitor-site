from __future__ import annotations

import argparse
import json
import pathlib
import time

from target_content import fetch_target_content


BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "site" / "data"


def load_payload(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch target page content and cache it into site data.")
    parser.add_argument("--input", type=pathlib.Path, default=DATA_DIR / "latest.json")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--sleep-ms", type=int, default=250)
    args = parser.parse_args()

    payload = load_payload(args.input)
    updated = 0
    for item in payload.get("items", [])[: args.limit]:
        if item.get("target_excerpt") and not args.refresh:
            continue
        target_url = item.get("target_url")
        if not target_url:
            continue
        try:
            fetched = fetch_target_content(target_url)
        except Exception as exc:  # noqa: BLE001
            item["target_fetch_error"] = str(exc)
            continue
        item["target_fetched_url"] = fetched["fetched_url"]
        item["target_title"] = fetched["title"]
        item["target_summary"] = fetched["summary"]
        item["target_excerpt"] = fetched["body_excerpt"]
        item.pop("target_fetch_error", None)
        updated += 1
        time.sleep(args.sleep_ms / 1000)

    args.input.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Updated target content for {updated} item(s) in {args.input}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
