"""
Standalone article generator - restores the old site's core depth
(2026-04 version had 399 evt-*.html article pages; cards are just the
entry point, the article is the product).

For each feed item:
  1. Gather material: item headline/summary (feeds carry release notes,
     HN carries content excerpts) + live target-page text via Scrapling
     Fetcher (graceful skip if blocked).
  2. One MiniMax call writes a 4-paragraph Traditional-Chinese article:
     what it is (background) / what happened (facts from material) /
     user benefit (before vs after, who should try) / alternatives &
     caveats. Numbers and claims must come from the material only.
  3. Render site/articles/{id}.html (old section layout: body / why it
     matters / source note / original link) + article-mapping.json for
     the homepage cards to link to.

Incremental: .meta.json sidecar stores (material fingerprint + prompt
version); unchanged items are never re-rendered.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import time
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from enrich import load_providers  # noqa: E402


def call_article(provider: dict, prompt: str) -> dict:
    """One MiniMax call that returns any top-level JSON object (article
    schema, not the items-array schema used by card enrichment)."""
    body = json.dumps({
        "model": provider["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 16384,
    }).encode("utf-8")
    import urllib.error
    import urllib.request
    last_error = ""
    for attempt in range(1, provider["retries"] + 1):
        request = urllib.request.Request(
            provider["url"], data=body,
            headers={"Authorization": f"Bearer {provider['key']}",
                     "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=180) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            message = (payload.get("choices") or [{}])[0].get("message", {}) or {}
            content = (message.get("content") or message.get("reasoning_content") or "").strip()
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.S)
            start, end = content.find("{"), content.rfind("}")
            if start == -1 or end < start:
                raise ValueError(f"no JSON object in response: {content[:100]}")
            parsed = json.loads(content[start:end + 1])
            if not isinstance(parsed, dict):
                raise ValueError("article response is not an object")
            return parsed
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:100]}"
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        print(f"    {provider['name']} attempt {attempt} failed: {last_error[:110]}")
        if attempt < provider["retries"]:
            time.sleep(provider.get("backoff", 10))
    raise RuntimeError(f"article call failed: {last_error[:150]}")

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
SITE_DATA = BASE_DIR / "site" / "data"
ARTICLES_DIR = BASE_DIR / "site" / "articles"
MAPPING_FILE = ARTICLES_DIR / "article-mapping.json"

ARTICLE_PROMPT_VERSION = "a1"
FETCH_TIMEOUT = 20
PAGE_TEXT_CAP = 6000

SYSTEM_PROMPT = (
    "你係 AI 新聞網站嘅特約編輯,為一般 AI 用戶寫 300-450 字繁體中文文章。"
    "段落骨架(必跟,四段):"
    "第一段=呢件係咩:主角係咩(當讀者完全未聽過,例:Blender 係免費開源 3D 軟件)+今次發生咩事;"
    "第二段=具體細節:新增咗咩功能/支援咩/幾多星幾多錢——必須來自材料,揀最有資訊量嘅講;"
    "第三段=對你嘅實際好處:以前要點做,而家點做,邊類人應該試;"
    "第四段=同類選擇同注意位:有咩類似工具(用你知識,唔肯定就明講唔確定)、裝之前要留意咩。"
    "鐵律:所有數字、功能宣稱、日期必須來自材料,一個唔准作;背景解釋可用你知識。"
    "禁止套語(「值得关注」「讓我們拭目以待」之類全部禁止)。"
    "只回覆 strict JSON,唔好 markdown 圍欄。"
)

USER_PROMPT = (
    "為以下新聞寫繁體中文文章,回覆 JSON:"
    "{{\"seo_title_en\": \"...\", \"h1_zh\": \"...\", \"standfirst_zh\": \"...\","
    " \"body_zh\": [\"第一段\", \"第二段\", \"第三段\", \"第四段\"],"
    " \"why_zh\": [\"要點1\", \"要點2\", \"要點3\"],"
    " \"source_note_zh\": \"一句講明來源同可靠性\"}}\n"
    "標題規則:h1_zh 30字內有資訊量;seo_title_en 英文 12 字內。\n"
    "MATERIAL:\n{material}"
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def fetch_page_text(url: str) -> str:
    """Target-page text via Scrapling Fetcher (plain HTTP ladder step)."""
    if not url or not url.startswith("http"):
        return ""
    try:
        from scrapling import Fetcher
        page = Fetcher(auto_match=False).get(
            url, timeout=FETCH_TIMEOUT, headers={
                "User-Agent": "AI-News-Monitor/1.0 (+https://aisignalfeed.com)"})
        if page.status != 200:
            return ""
        text = page.get_all_text() or ""
        # mid-page longest lines beat nav boilerplate
        lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 80]
        body = " ".join(lines[:40]) if lines else text
        return body[:PAGE_TEXT_CAP]
    except Exception:  # noqa: BLE001 - blocked/timeout is normal, skip
        return ""


def item_fingerprint(item: dict) -> str:
    material = f"{ARTICLE_PROMPT_VERSION}|{item.get('id')}|{item.get('summary_en', '')}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]


def needs_article(item: dict) -> bool:
    slug = item["id"]
    meta = ARTICLES_DIR / f"{slug}.meta.json"
    if not meta.exists() or not (ARTICLES_DIR / f"{slug}.html").exists():
        return True
    try:
        stored = json.loads(meta.read_text(encoding="utf-8")).get("fingerprint", "")
    except json.JSONDecodeError:
        return True
    return stored != item_fingerprint(item)


def render_article(item: dict, art: dict) -> str:
    def esc(text: str) -> str:
        return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    paragraphs = "".join(
        f"<p>{esc(p)}</p>" for p in art.get("body_zh", []) if p)
    bullets = "".join(
        f"<li>{esc(b)}</li>" for b in art.get("why_zh", []) if b)
    url = item.get("target_url") or item.get("source_url") or "#"
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(art.get('h1_zh', ''))} | AI Signal Feed</title>
<meta name="description" content="{esc(art.get('standfirst_zh', '')[:150])}">
<style>
:root {{ --bg:#f6efe4; --surface:rgba(255,255,255,.88); --ink:#1a1f18; --muted:#586255;
--accent:#0f766e; --accent-soft:rgba(15,118,110,.12); --border:rgba(15,118,110,.16);
--shadow:0 18px 42px rgba(27,39,33,.09); }}
*{{box-sizing:border-box}} body{{margin:0;font-family:"Segoe UI",system-ui,sans-serif;
color:var(--ink);background:linear-gradient(180deg,#fcf8f2 0%,var(--bg) 100%)}}
main{{max-width:860px;margin:0 auto;padding:32px 18px 64px}}
.topline{{display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px;
color:var(--muted);font-size:.9rem;margin-bottom:20px}}
.chip{{display:inline-flex;padding:6px 10px;border-radius:999px;background:var(--accent-soft);
color:var(--accent);font-weight:700;font-size:.74rem;text-transform:uppercase;
letter-spacing:.05em}}
h1{{font-family:Georgia,"Times New Roman",serif;font-size:clamp(1.6rem,4.5vw,2.6rem);
line-height:1.15;letter-spacing:-.02em;margin:14px 0 10px}}
.standfirst{{color:var(--muted);font-size:1.05rem;line-height:1.7;margin:0 0 8px}}
.meta{{color:var(--muted);font-size:.86rem;margin-bottom:22px}}
.section{{padding:20px 22px;background:var(--surface);border:1px solid var(--border);
border-radius:18px;box-shadow:var(--shadow);margin-bottom:16px}}
.section h2{{margin:0 0 10px;font-size:1.05rem}}
.section p{{margin:0 0 12px;line-height:1.85;color:#3a423b}}
.section p:last-child{{margin:0}}
.section ul{{margin:0;padding-left:1.1rem;line-height:1.8;color:#3a423b}}
a{{color:var(--accent);text-decoration:none;font-weight:700}}
</style>
</head>
<body>
<main>
<div class="topline">
  <div><a href="../index.html">← AI Signal Feed</a></div>
  <div>{esc(item.get('source_name', ''))} · {esc(item.get('timestamp', '')[:16])}</div>
</div>
<article class="section" style="padding:26px 28px">
  <span class="chip">{esc(item.get('category', 'update'))}</span>
  <h1>{esc(art.get('h1_zh', ''))}</h1>
  <p class="standfirst">{esc(art.get('standfirst_zh', ''))}</p>
  <div class="meta">英文搜尋標題:{esc(art.get('seo_title_en', ''))}</div>
</article>
<section class="section"><h2>內文</h2>{paragraphs}</section>
<section class="section"><h2>點解要理</h2><ul>{bullets}</ul></section>
<section class="section"><h2>來源</h2>
  <p>{esc(art.get('source_note_zh', ''))}</p>
  <p><a href="{esc(url)}" rel="noopener">睇原始來源 →</a></p>
</section>
</main>
</body>
</html>"""


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=3, help="articles per run")
    parser.add_argument("--only", type=str, default="", help="single item id (testing)")
    args = parser.parse_args()

    feed = json.loads((SITE_DATA / "feed.json").read_text(encoding="utf-8"))
    items = feed.get("items", [])
    if args.only:
        items = [i for i in items if i.get("id") == args.only]

    candidates = [i for i in items if needs_article(i)][:args.limit]
    if not candidates:
        print("no articles due this run")
        return 0

    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    providers = load_providers()
    mapping: dict[str, str] = {}
    if MAPPING_FILE.exists():
        try:
            mapping = json.loads(MAPPING_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            mapping = {}

    generated = 0
    for item in candidates:
        page_text = fetch_page_text(item.get("target_url") or "")
        material = json.dumps({
            "headline_en": item.get("headline_en", ""),
            "summary_en": (item.get("summary_en", "") or "")[:1200],
            "category": item.get("category", ""),
            "source_name": item.get("source_name", ""),
            "target_page_text": page_text[:PAGE_TEXT_CAP],
        }, ensure_ascii=False)
        print(f"  article {item['id']} ({item.get('source_name','')}) "
              f"[page text: {len(page_text)} chars] ...")
        try:
            art = call_article(providers[0], USER_PROMPT.format(material=material))
        except RuntimeError as exc:
            print(f"    [warn] skipped: {str(exc)[:120]}")
            continue
        if not art.get("body_zh"):
            print("    [warn] no body_zh, skipped")
            continue

        slug = item["id"]
        (ARTICLES_DIR / f"{slug}.html").write_text(
            render_article(item, art), encoding="utf-8")
        (ARTICLES_DIR / f"{slug}.meta.json").write_text(json.dumps({
            "fingerprint": item_fingerprint(item),
            "generated_at": utc_now(),
        }, ensure_ascii=False), encoding="utf-8")
        mapping[item["id"]] = slug
        generated += 1
        print(f"    OK -> articles/{slug}.html "
              f"({len(art.get('body_zh', []))} paragraphs)")
        time.sleep(2)

    MAPPING_FILE.write_text(json.dumps(mapping, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    (SITE_DATA / "articles.json").write_text(json.dumps(mapping, ensure_ascii=False),
                                             encoding="utf-8")
    print(f"generated {generated}/{len(candidates)} articles; mapping "
          f"{len(mapping)} -> articles/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
