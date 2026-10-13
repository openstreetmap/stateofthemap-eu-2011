#!/usr/bin/env python3
"""Validate the rebuilt SotM-EU 2011 static site.

Checks that every internal link, image, stylesheet, script and CSS ``url()``
reference resolves to a file in the repository, that no references to the
original ``sotm-eu.org`` domain or to ``web.archive.org`` remain, and reports
the external hosts that are still linked. Missing talk recordings under
``videos/`` are reported separately because they are not committed.
"""

from __future__ import annotations

import html
import re
import sys
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".cache", ".git", ".github", "scripts", "node_modules"}
ATTR_URL = re.compile(r"""\b(?:href|src|action)\s*=\s*(["'])(.*?)\1""", re.I | re.S)
META_URL = re.compile(r"""<meta\b[^>]*\bproperty=["']og:[a-z:_]+["'][^>]*\bcontent=(["'])(.*?)\1""", re.I | re.S)
CSS_URL = re.compile(r"""url\(\s*(["']?)([^"')]+)\1\s*\)""", re.I)
FORBIDDEN = re.compile(
    r"(?:https?:)?//(?:www\.|2011\.)?sotm-eu\.org|web\.archive\.org|[\w.+-]+@sotm-eu\.org", re.I
)
# Deliberate links to the originals on archive.org (recordings, the Impressum
# citation) are allowed; anything else pointing at web.archive.org is not.
ALLOWED_ARCHIVE_LINK = re.compile(
    r"""<a href="https://web\.archive\.org/web/\d{14}/[^"]*"[^>]*>"""
    r"""|\bcite="https://web\.archive\.org/web/\d{14}/[^"]*\""""
)
# IE-only stylesheets inside conditional comments were already 404 on the original site.
CONDITIONAL_COMMENT = re.compile(r"<!--\[if[^\]]*\]>.*?<!\[endif\]-->", re.S)


def site_files() -> list[Path]:
    files = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(PROJECT_ROOT).parts):
            continue
        if path.suffix.lower() in {".html", ".css", ".xml", ".ics", ".js"}:
            files.append(path)
    return sorted(files)


def references(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".css":
        return [match.group(2) for match in CSS_URL.finditer(text)]
    if path.suffix.lower() == ".html":
        text = CONDITIONAL_COMMENT.sub("", text)
        found = [html.unescape(match.group(2)) for match in ATTR_URL.finditer(text)]
        found += [html.unescape(match.group(2)) for match in META_URL.finditer(text)]
        found += [match.group(2) for match in CSS_URL.finditer(text)]
        return found
    return []


def resolve(target: str, source: Path) -> Path:
    parsed = urllib.parse.urlsplit(target)
    route = urllib.parse.unquote(parsed.path)
    if route.startswith("/"):
        local = PROJECT_ROOT / route.lstrip("/")
    else:
        local = source.parent / route
    if route.endswith("/") or route == "":
        local = local / "index.html"
    return local


def main() -> int:
    missing: dict[str, set[str]] = defaultdict(set)
    missing_videos: set[str] = set()
    forbidden: dict[str, int] = Counter()
    external_hosts: Counter = Counter()
    checked = 0

    files = site_files()
    for path in files:
        rel = str(path.relative_to(PROJECT_ROOT))
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = len(FORBIDDEN.findall(html.unescape(ALLOWED_ARCHIVE_LINK.sub("", text))))
        if hits:
            forbidden[rel] = hits
        for target in references(path):
            target = target.strip()
            if not target or target.startswith(("#", "mailto:", "javascript:", "data:", "tel:")):
                continue
            parsed = urllib.parse.urlsplit(target)
            if parsed.scheme in ("http", "https") or target.startswith("//"):
                external_hosts[parsed.netloc.lower()] += 1
                continue
            if parsed.scheme:
                continue
            checked += 1
            local = resolve(target, path)
            if local.exists():
                continue
            route = urllib.parse.unquote(parsed.path)
            if route.startswith("/videos/"):
                missing_videos.add(route)
            else:
                missing[route].add(rel)

    print(f"Scanned {len(files)} text files, {checked} internal references.")
    status = 0
    if forbidden:
        status = 1
        print(f"\nFiles still referencing sotm-eu.org / web.archive.org ({len(forbidden)}):")
        for rel, hits in sorted(forbidden.items()):
            print(f"  {rel}: {hits}")
    if missing:
        status = 1
        print(f"\nBroken internal references ({len(missing)}):")
        for route, sources in sorted(missing.items()):
            sample = sorted(sources)[:3]
            more = "" if len(sources) <= 3 else f" (+{len(sources) - 3} more)"
            print(f"  {route}  <- {', '.join(sample)}{more}")
    if missing_videos:
        print(f"\nRecordings not present locally ({len(missing_videos)}), see README:")
        for route in sorted(missing_videos):
            print(f"  {route}")
    print("\nExternal hosts referenced:")
    for host, count in external_hosts.most_common():
        print(f"  {count:5d}  {host}")
    print("\nOK" if status == 0 else "\nFAILED")
    return status


if __name__ == "__main__":
    sys.exit(main())
