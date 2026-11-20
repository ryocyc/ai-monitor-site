"""
Per-article content QA: verify every number/claim in every generated
article against (a) the item's own material (feed summary) and (b) a
FRESH fetch of the target page. Any number found in neither source is
flagged as a potential hallucination. Also checks structure integrity.

Usage: python build/qa_articles.py            (summary table)
       python build/qa_articles.py --full     (per-article detail)
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import time
import urllib.parse

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
ARTICLES_DIR = BASE_DIR / "site" / "articles"
SITE_DATA = BASE_DIR / "site" / "data"

FETCH_TIMEOUT = 20
PAGE_CAP = 12000


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def numbers_in(text: str) -> set[str]:
    """Normalize all numbers (strip commas, keep dots) for set comparison."""
    out = set()
    for m in re.finditer(r"\d[\d,\.]*\d|\d", text or ""):
        out.add(m.group(0).replace(",", ""))
    return out


def article_body(slug: str) -> dict:
    html = (ARTICLES_DIR / f"{slug}.html").read_text(encoding="utf-8")
    def section(title):
        m = re.search(rf"<h2>{title}</h2>(.*?)</section>", html, re.S)
        return m.group(1) if m else ""
    body_html = section("內文")
    paras = [clean(re.sub(r"<[^>]+>", "", p)) for p in re.findall(r"<p>(.*?)</p>", body_html, re.S)]
    bullets = [clean(re.sub(r"<[^>]+>", "", b)) for b in re.findall(r"<li>(.*?)</li>", section("點解要理"), re.S)]
    note = ""
    note_m = re.search(r"<h2>來源</h2>\s*<p>(.*?)</p>", html, re.S)
    if note_m:
        note = clean(re.sub(r"<[^>]+>", "", note_m.group(1)))
    h1_m = re.search(r"<h1>(.*?)</h1>", html, re.S)
    stand_m = re.search(r'class="standfirst">(.*?)</p>', html, re.S)
    return {
        "h1": clean(re.sub(r"<[^>]+>", "", h1_m.group(1))) if h1_m else "",
        "standfirst": clean(re.sub(r"<[^>]+>", "", stand_m.group(1))) if stand_m else "",
        "paras": [p for p in paras if p],
        "bullets": [b for b in bullets if b],
        "note": note,
    }


def fetch_page(url: str) -> str:
    if not url or not url.startswith("http"):
        return ""
    try:
        from scrapling import Fetcher
        page = Fetcher(auto_match=False).get(
            url, timeout=FETCH_TIMEOUT,
            headers={"User-Agent": "AI-News-Monitor/1.0 (+https://aisignalfeed.com)"})
        if page.status != 200:
            return ""
        text = page.get_all_text() or ""
        lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 60]
        return " ".join(lines)[:PAGE_CAP]
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    if sys.stdout is not None and sys.stdout.encoding \
            and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    full = "--full" in sys.argv

    archive = {}
    for f in ("site/data/feed.json", "site/data/archive.json"):
        p = BASE_DIR / f
        if p.exists():
            for item in json.loads(p.read_text(encoding="utf-8")).get("items", []):
                archive.setdefault(item["id"], item)

    mapping = json.loads((ARTICLES_DIR / "article-mapping.json").read_text(encoding="utf-8"))
    # new-system slugs only (id == slug, not evt-*)
    new_slugs = sorted(set(mapping.values()) - {s for s in set(mapping.values()) if s.startswith("evt-")})

    report = []
    for idx, slug in enumerate(new_slugs, 1):
        item = archive.get(slug) or {}
        art = article_body(slug)
        full_text = art["h1"] + " " + art["standfirst"] + " " + " ".join(art["paras"]) \
            + " " + " ".join(art["bullets"]) + " " + art["note"]

        # source material numbers
        item_numbers = numbers_in(
            (item.get("headline_en", "") or "") + " " + (item.get("summary_en", "") or ""))

        # fresh page fetch
        page_text = fetch_page(item.get("target_url") or "")
        page_numbers = numbers_in(page_text)

        # every number in the article must exist in item material or page
        art_numbers = numbers_in(full_text)
        unknown = sorted(art_numbers - item_numbers - page_numbers,
                         key=lambda n: -(len(n)))

        # structural checks
        thin_paras = [p for p in art["paras"] if len(p) < 25]
        issues = []
        if len(art["paras"]) < 3:
            issues.append(f"段數不足({len(art['paras'])})")
        if thin_paras:
            issues.append(f"過薄段落 x{len(thin_paras)}: {thin_paras[0][:40]}")
        if len(art["bullets"]) < 2:
            issues.append(f"要點不足({len(art['bullets'])})")
        if not art["note"]:
            issues.append("缺來源註")
        if "..." in full_text or "………" in full_text:
            issues.append("佔位符殘留")
        if unknown:
            issues.append(f"未證實數字 x{len(unknown)}: {', '.join(unknown[:6])}")

        verdict = "PASS" if not issues else ("SUSPECT" if unknown else "WARN")
        report.append({
            "n": idx, "slug": slug,
            "source": item.get("source_name", "?"),
            "h1": art["h1"][:40],
            "chars": sum(len(p) for p in art["paras"]),
            "page_fetched": len(page_text),
            "unknown_numbers": unknown,
            "issues": issues, "verdict": verdict,
        })
        if full:
            print(f"[{idx}] {verdict} | {slug} | {item.get('source_name','?')} | {art['h1'][:36]}")
            print(f"    內文 {sum(len(p) for p in art['paras'])} 字 / {len(art['paras'])} 段 | "
                  f"頁面重抓 {len(page_text)} 字")
            if issues:
                for i in issues:
                    print(f"    ⚠ {i}")
        time.sleep(0.5)

    # summary
    print("=" * 70)
    counts = {"PASS": 0, "WARN": 0, "SUSPECT": 0}
    for r in report:
        counts[r["verdict"]] += 1
    print(f"total {len(report)} articles | PASS {counts['PASS']} | "
          f"WARN {counts['WARN']} | SUSPECT {counts['SUSPECT']}")
    for r in report:
        if r["verdict"] != "PASS":
            print(f"  {r['verdict']:7} [{r['n']:2}] {r['slug']} | {r['h1'][:36]}")
            for i in r["issues"]:
                print(f"          {i}")
    (BASE_DIR / "data" / "qa_articles_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("detail -> data/qa_articles_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
