from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from typing import Iterable


BASE_DIR = pathlib.Path(__file__).resolve().parent
SITE_DATA = BASE_DIR / "site" / "data" / "latest.json"


def load_payload(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def build_prompt(items: list[dict]) -> str:
    compact = []
    for item in items:
        compact.append(
            {
                "id": item["id"],
                "source_name": item.get("source_name", ""),
                "category": item.get("category", ""),
                "target_url": item.get("target_url", ""),
                "target_title": clip(item.get("target_title", ""), 180),
                "target_summary": clip(item.get("target_summary", ""), 500),
                "target_excerpt": clip(item.get("target_excerpt", ""), 1200),
                "headline_en": clip(item.get("headline_en", ""), 180),
                "summary_en": clip(item.get("summary_en", ""), 320),
                "evidence_excerpt": clip(item.get("evidence_excerpt", ""), 900),
            }
        )

    return (
        "Rewrite and translate the following AI news card text for a website UI.\n"
        "Rules:\n"
        "- Improve the English headline and summary so they read like a useful AI news card.\n"
        "- Keep product names, model names, version numbers, and company names in English when appropriate.\n"
        "- Use the evidence_excerpt to infer what changed. Mention the actual updated topic whenever visible.\n"
        "- If target_title, target_summary, or target_excerpt provide more specific details than the feed, prefer them.\n"
        "- For GitHub releases: if target_summary or target_excerpt include release notes, mention the concrete fixes or features instead of only the version tag.\n"
        "- Only fall back to 'release tag changed' wording when neither the feed nor the fetched target content shows meaningful release-note details.\n"
        "- Avoid empty filler such as 'visit the page for details' unless no meaningful detail is visible.\n"
        "- If target_url looks generic, still write the best summary you can from the evidence_excerpt.\n"
        "- Then translate both fields into Traditional Chinese.\n"
        "- Be concise, natural, and factual.\n"
        "- Do not invent facts.\n"
        "- Return strict JSON only, with top-level key \"items\".\n"
        "- Each item must include: id, headline_en, summary_en, headline_zh, summary_zh.\n\n"
        f"{json.dumps({'items': compact}, ensure_ascii=False)}"
    )


def call_claude(prompt: str, max_budget_usd: float) -> dict:
    command = [
        "claude",
        "-p",
        "--bare",
        "--output-format",
        "json",
        "--max-budget-usd",
        str(max_budget_usd),
        "--permission-mode",
        "bypassPermissions",
        prompt,
    ]
    result = subprocess.run(command, capture_output=True)
    stdout_text = result.stdout.decode("utf-8", errors="replace")
    stderr_text = result.stderr.decode("utf-8", errors="replace")
    if result.returncode != 0:
      raise RuntimeError(
          "Claude CLI failed.\n"
          f"STDOUT:\n{stdout_text}\n"
          f"STDERR:\n{stderr_text}"
      )
    payload = json.loads(stdout_text)
    content = payload.get("result", "").strip()
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise RuntimeError(f"Claude returned non-JSON content:\n{content}")
    return json.loads(content[start : end + 1])


def merge_translations(payload: dict, translated: dict) -> dict:
    translated_map = {item["id"]: item for item in translated.get("items", [])}
    for item in payload.get("items", []):
        match = translated_map.get(item["id"])
        if not match:
            continue
        item["headline_en"] = match.get("headline_en", item.get("headline_en", ""))
        item["summary_en"] = match.get("summary_en", item.get("summary_en", ""))
        item["headline_zh"] = match.get("headline_zh", "")
        item["summary_zh"] = match.get("summary_zh", "")
        item["needs_cc_refresh"] = False
    return payload


def chunked(items: list[dict], size: int) -> Iterable[list[dict]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def needs_enrichment(item: dict) -> bool:
    """
    Returns True only if the item genuinely needs Claude rewrite.

    Uses the needs_cc_refresh flag set by publish_site.py (via apply_existing_content)
    as the primary signal. This flag is only True when:
      - The item is brand new (no previous content found)
      - OR content_fingerprint / merge_key shows the content actually changed
    Items that are old and unchanged (moved to archive naturally) will have
    needs_cc_refresh=False and will be skipped here.
    """
    if item.get("needs_cc_refresh"):
        return True
    # Fallback: item has no English enriched fields at all → needs enrichment
    has_en = (item.get("headline_en") or "").strip() and (item.get("summary_en") or "").strip()
    has_zh = (item.get("headline_zh") or "").strip() and (item.get("summary_zh") or "").strip()
    return not (has_en and has_zh)


def main() -> int:
    parser = argparse.ArgumentParser(description="Use Claude CLI to add Traditional Chinese text to generated site cards.")
    parser.add_argument("--input", type=pathlib.Path, default=SITE_DATA, help="Path to latest.json")
    parser.add_argument("--max-budget-usd", type=float, default=1.0, help="Claude max budget for this batch")
    parser.add_argument("--batch-size", type=int, default=8, help="Number of items to send to Claude in each batch")
    parser.add_argument("--force", action="store_true", help="Rewrite all items even if they already have enriched fields")
    parser.add_argument("--dry-run", action="store_true", help="Print the prompt and exit")
    args = parser.parse_args()

    payload = load_payload(args.input)
    all_items = payload.get("items", [])
    items = all_items if args.force else [item for item in all_items if needs_enrichment(item)]
    prompt = build_prompt(items[: args.batch_size])

    if args.dry_run:
        sys.stdout.write(prompt + "\n")
        return 0

    if not items:
        print(f"No enrichment needed for {args.input}.")
        return 0

    translated_items = {"items": []}
    item_batches = list(chunked(items, max(1, args.batch_size)))
    if not item_batches:
        print(f"No items found in {args.input}.")
        return 0

    per_batch_budget = max(args.max_budget_usd / len(item_batches), 0.05)
    for batch in item_batches:
        translated = call_claude(build_prompt(batch), per_batch_budget)
        translated_items["items"].extend(translated.get("items", []))

    merged = merge_translations(payload, translated_items)
    args.input.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Updated translations in {args.input}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
