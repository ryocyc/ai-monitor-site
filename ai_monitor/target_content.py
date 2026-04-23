from __future__ import annotations

import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlparse


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36 AI-News-Monitor/0.1"
)


def fetch_url(url: str, timeout: int = 25) -> tuple[str, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        text = response.read().decode(charset, errors="replace")
        return text, response.geturl()


def normalize_html(raw: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<!--[\s\S]*?-->", " ", text)
    text = re.sub(r"</(p|div|section|article|li|h1|h2|h3|br)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\uFFFD", " ")
    text = re.sub(r"[^\S\r\n]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def extract_html_title(raw: str, fallback_text: str) -> str:
    for pattern in (
        r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)[\"']",
        r"<meta[^>]+name=[\"']title[\"'][^>]+content=[\"']([^\"']+)[\"']",
        r"<title>([^<]+)</title>",
        r"<h1[^>]*>(.*?)</h1>",
    ):
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", normalize_html(match.group(1))).strip()
    return summarize_text(fallback_text, 90)


def extract_main_text(raw: str) -> str:
    for pattern in (
        r"<article[\s\S]*?</article>",
        r"<main[\s\S]*?</main>",
        r"<body[\s\S]*?</body>",
    ):
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if match:
            return normalize_html(match.group(0))
    return normalize_html(raw)


def extract_github_release(raw: str) -> tuple[str, str]:
    text = extract_main_text(raw)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title = ""
    body_lines: list[str] = []
    for line in lines:
        if not title and len(line) < 140:
            title = line
            continue
        if line.lower().startswith(("releases", "notifications", "jump to")):
            continue
        body_lines.append(line)
        if len(body_lines) >= 20:
            break
    return title or summarize_text(text, 90), " ".join(body_lines)


def github_release_atom_url(release_url: str) -> str | None:
    parsed = urlparse(release_url)
    parts = [part for part in parsed.path.split("/") if part]
    try:
        release_index = parts.index("releases")
    except ValueError:
        return None
    repo_parts = parts[:release_index]
    if len(repo_parts) < 2:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/{'/'.join(repo_parts)}/releases.atom"


def extract_github_release_from_atom(xml_text: str, release_url: str) -> tuple[str, str] | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    normalized_release_url = release_url.rstrip("/")
    for entry in root.findall(".//atom:entry", ns):
        link_node = entry.find("atom:link", ns)
        link = (link_node.get("href", "") if link_node is not None else "").strip().rstrip("/")
        if link != normalized_release_url:
            continue
        title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        content = (entry.findtext("atom:content", default="", namespaces=ns) or "").strip()
        body = extract_main_text(content) if content else ""
        if not body:
            return None
        return title or summarize_text(body, 90), body
    return None


def summarize_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def summarize_content(title: str, body: str) -> tuple[str, str]:
    sentences = re.split(r"(?<=[.!?])\s+", body)
    useful = [s.strip() for s in sentences if len(s.strip()) > 30][:2]
    if useful:
        summary = " ".join(useful)
    else:
        summary = summarize_text(body, 240)
    return summarize_text(title, 120), summarize_text(summary, 260)


def fetch_target_content(url: str) -> dict[str, str]:
    parsed_input = urlparse(url)
    body = ""
    title = ""
    fetched_url = url

    if parsed_input.netloc.lower() == "github.com" and "/releases/tag/" in parsed_input.path.lower():
        atom_url = github_release_atom_url(url)
        if atom_url:
            try:
                atom_raw, _ = fetch_url(atom_url)
                extracted = extract_github_release_from_atom(atom_raw, url)
                if extracted:
                    title, body = extracted
            except Exception:  # noqa: BLE001
                pass

    if not body:
        raw, fetched_url = fetch_url(url)
        parsed = urlparse(fetched_url)
        if parsed.netloc.lower() == "github.com" and "/releases/tag/" in parsed.path.lower():
            title, body = extract_github_release(raw)
        else:
            body = extract_main_text(raw)
            title = extract_html_title(raw, body)
    title, summary = summarize_content(title, body)
    return {
        "fetched_url": fetched_url,
        "title": title,
        "summary": summary,
        "body_excerpt": summarize_text(body, 1200),
    }


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Fetch and normalize a target content page.")
    parser.add_argument("url")
    args = parser.parse_args()
    json.dump(fetch_target_content(args.url), sys.stdout, ensure_ascii=False, indent=2)
