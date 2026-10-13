#!/usr/bin/env python3
"""Rebuild the State of the Map Europe 2011 website from the Wayback Machine.

The original site (www.sotm-eu.org / sotm-eu.org) was a Drupal 6 site that was
later converted to an HTTrack-style static mirror. This script:

  1. ``inventory`` - queries the Archive.org CDX API for every capture of the
     site in the archive window and keeps the newest successful capture of
     every distinct URL.
  2. ``fetch``     - downloads each capture (raw, ``id_`` mode). Text resources
     are cached unmodified under ``.cache/raw/`` so that the rewrite rules can
     be re-run offline; binaries are streamed straight into the site tree.
  3. ``build``     - rewrites the cached text resources into a freestanding
     static site using directory-style URLs (``/talk/61/``, ``/node/45/``,
     ``/programme/speaker/``) and writes ``archive-manifest.json``.

Running the script without a sub-command performs all three steps. Every
Archive.org request is sequential, uses a descriptive User-Agent and is
separated by a configurable delay (default two seconds).
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / ".cache"
RAW_DIR = CACHE_DIR / "raw"
# scripts/compress_videos.py moves downloaded recordings here and publishes AV1 .mp4 files.
ORIGINALS_DIR = CACHE_DIR / "originals"
PUBLISHED_VIDEO_SUFFIX = ".mp4"
INVENTORY_FILE = CACHE_DIR / "inventory.json"
MANIFEST_FILE = PROJECT_ROOT / "archive-manifest.json"

NEW_ORIGIN = "https://2011.stateofthemap.eu"
ARCHIVE_HOSTS = ("sotm-eu.org", "www.sotm-eu.org", "2011.sotm-eu.org")
ARCHIVE_FROM = "20101115000000"
ARCHIVE_TO = "20131107001623"
CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
USER_AGENT = (
    "sotm-eu-2011-static-archive/1.0 "
    "(+https://2011.stateofthemap.eu/; rebuilding the SotM-EU 2011 site)"
)
DEFAULT_DELAY = 2.0
MAX_ATTEMPTS = 10
# Global cooldown applied to *all* request starts after an Archive.org error;
# doubles on consecutive failures and resets after a success.
COOLDOWN_BASE_SECONDS = 30.0
DEFAULT_MAX_COOLDOWN_SECONDS = 1800.0
# Permanent errors: Wayback refuses excluded content with 403/451 and removed
# captures with 410. 404 is retried because the API serves it transiently
# while degraded, even for captures the CDX index lists as present.
FATAL_HTTP_CODES = {403, 410, 451}

TEXT_MIME_PREFIXES = ("text/",)
TEXT_MIME_TYPES = {
    "application/javascript",
    "application/x-javascript",
    "application/json",
    "application/xml",
    "application/rss+xml",
    "image/svg+xml",
}
STATIC_EXTENSIONS = {
    ".css", ".js", ".gif", ".png", ".jpg", ".jpeg", ".ico", ".svg",
    ".pdf", ".ppt", ".pptx", ".odp", ".doc", ".docx",
    ".ogg", ".ogv", ".mp4", ".mp3", ".ics", ".xml", ".txt", ".json",
}
VIDEO_EXTENSIONS = {".ogg", ".ogv", ".mp4"}
# Directories that only ever held files, never Drupal pages.
ASSET_DIRECTORIES = ("files", "flags", "js", "persons", "sites", "slides", "userfiles", "videos")

# Dynamic Drupal functionality that cannot work on a static site.
EXCLUDED_PATH_PATTERNS = (
    re.compile(r"^user(/|$)"),
    re.compile(r"^admin(/|$)"),
    re.compile(r"^al(/|$)"),
    re.compile(r"^google[0-9a-f]+\.html$"),
    re.compile(r"^robots\.txt$"),
)

# Query-string pages are mapped onto directory-style routes.
QUERY_ROUTES: dict[str, Callable[[str], Optional[str]]] = {
    "talk": lambda q: f"/talk/{q}/" if re.fullmatch(r"\d+", q) else None,
    "video": lambda q: f"/video/{q}/" if re.fullmatch(r"\d+", q) else None,
    "programme": lambda q: (
        f"/programme/{q[6:]}/" if re.fullmatch(r"order=[a-z]+", q) else None
    ),
    "attendees": lambda q: (
        "/attendees/" if q == "order=name"
        else f"/attendees/{q[6:]}/" if re.fullmatch(r"order=[a-z]+", q) else None
    ),
    "node": lambda q: (
        f"/node/page/{q[5:]}/" if re.fullmatch(r"page=\d+", q) else None
    ),
}

# Pages whose ``?query`` form must keep resolving: a tiny inline script in the
# directory index redirects visitors to the matching directory-style route.
QUERY_SHIMS = {
    "talk": r"var m=/^(\d+)$/.exec(q);if(m)go('/talk/'+m[1]+'/');",
    "video": r"var m=/^(\d+)$/.exec(q);if(m)go('/video/'+m[1]+'/');",
    "programme": r"var m=/^order=([a-z]+)$/.exec(q);if(m)go('/programme/'+m[1]+'/');",
    "attendees": r"var m=/^order=([a-z]+)$/.exec(q);if(m)go('/attendees/'+m[1]+'/');",
    "node": r"var m=/^page=(\d+)$/.exec(q);if(m)go('/node/page/'+m[1]+'/');",
}

# Speaker photos referenced by the April 2011 programme that were never
# captured: use the later PNG of the same user, or the site's generic avatar.
LINK_FIXES = {
    "/persons/steve8.jpg": "/persons/steve8.png",
    "/persons/woodpeck.jpg": "/persons/osm.png",
    "/persons/mikel.jpg": "/persons/osm.png",
    "/persons/texamus.jpg": "/persons/osm.png",
    "/persons/Thomas Emge.jpg": "/persons/osm.png",
}


# --------------------------------------------------------------------------
# Archive.org access
# --------------------------------------------------------------------------

class ArchiveOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).hostname != "web.archive.org":
            raise urllib.error.HTTPError(
                newurl, code, "Refusing redirect outside web.archive.org", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(ArchiveOnlyRedirectHandler())


class Throttle:
    """Space the start of Archive.org requests by ``delay`` seconds, across threads.

    Errors reported through :meth:`report_failure` impose a shared cooldown
    that grows exponentially with consecutive failures, so a struggling
    Archive.org is left alone by every worker, not just the one that failed.
    """

    def __init__(self, delay: float, max_cooldown: float = DEFAULT_MAX_COOLDOWN_SECONDS) -> None:
        self.delay = delay
        self.max_cooldown = max_cooldown
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self._failures = 0

    def wait(self) -> None:
        with self._lock:
            start = max(time.monotonic(), self._next_allowed)
            self._next_allowed = start + self.delay
        time.sleep(max(0.0, start - time.monotonic()))

    def pause(self, seconds: float) -> None:
        with self._lock:
            self._next_allowed = max(self._next_allowed, time.monotonic() + seconds)

    def report_failure(self) -> float:
        """Register an Archive.org error; returns the cooldown now in force."""
        with self._lock:
            self._failures += 1
            cooldown = min(self.max_cooldown, COOLDOWN_BASE_SECONDS * 2 ** (self._failures - 1))
            self._next_allowed = max(self._next_allowed, time.monotonic() + cooldown)
            return cooldown

    def report_success(self) -> None:
        with self._lock:
            self._failures = 0


THROTTLE = Throttle(DEFAULT_DELAY)
PRINT_LOCK = threading.Lock()


def log(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def stream_to_file(response, sink: Path, resume_from: int) -> None:
    """Append the response body to ``sink``'s .part file and finalise it."""
    partial = sink.with_name(sink.name + ".part")
    content_range = response.headers.get("Content-Range", "")
    if resume_from and response.status == 206 and content_range.startswith(f"bytes {resume_from}-"):
        mode, expected = "ab", int(content_range.rsplit("/", 1)[1])
    else:
        # Range ignored (or no partial): start over.
        mode, expected = "wb", int(response.headers.get("Content-Length") or 0)
        resume_from = 0
    with partial.open(mode) as handle:
        shutil.copyfileobj(response, handle, 1 << 20)
    size = partial.stat().st_size
    if expected and size != expected:
        raise OSError(f"Short read: {size} of {expected} bytes")
    partial.replace(sink)


def archive_request(url: str, sink: Optional[Path] = None) -> bytes:
    """GET ``url`` from web.archive.org with throttling, retries and resume.

    When ``sink`` is given the body is streamed into that file (resuming an
    existing ``.part`` file with an HTTP Range request) and an empty byte
    string is returned; otherwise the body is returned in memory.
    """
    if urllib.parse.urlsplit(url).hostname != "web.archive.org":
        raise ValueError(f"Refusing non-Wayback request: {url}")

    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        THROTTLE.wait()
        headers = {"User-Agent": USER_AGENT}
        resume_from = 0
        if sink is not None:
            sink.parent.mkdir(parents=True, exist_ok=True)
            partial = sink.with_name(sink.name + ".part")
            if partial.exists() and partial.stat().st_size > 0:
                resume_from = partial.stat().st_size
                headers["Range"] = f"bytes={resume_from}-"
        request = urllib.request.Request(url, headers=headers)
        try:
            with OPENER.open(request, timeout=120) as response:
                if sink is None:
                    data = response.read()
                    THROTTLE.report_success()
                    return data
                if resume_from:
                    log(f"    resuming {sink.name} from {resume_from / 1048576:.0f} MB")
                stream_to_file(response, sink, resume_from)
                THROTTLE.report_success()
                return b""
        except urllib.error.HTTPError as error:
            if error.code == 416 and sink is not None:
                # Range not satisfiable: the partial file is unusable.
                sink.with_name(sink.name + ".part").unlink(missing_ok=True)
            if error.code in FATAL_HTTP_CODES or attempt == MAX_ATTEMPTS:
                raise
            if error.code == 404:
                # Transient while Wayback is degraded, but not a sign of overload:
                # retry a few times without holding up the other workers.
                if attempt >= 4:
                    raise
                log(f"    retry {attempt}/3 in 60s: {error}")
                time.sleep(60)
                continue
            last_error = error
        except (OSError, urllib.error.URLError) as error:
            if attempt == MAX_ATTEMPTS:
                raise
            last_error = error
        cooldown = THROTTLE.report_failure()
        log(f"    retry {attempt}/{MAX_ATTEMPTS} after {cooldown:.0f}s cooldown: {last_error}")
    raise RuntimeError("unreachable")


def replay_url(timestamp: str, original_url: str) -> str:
    encoded = urllib.parse.quote(original_url, safe=":/?&=%#+,;@[]!$'()*")
    return f"https://web.archive.org/web/{timestamp}id_/{encoded}"


# --------------------------------------------------------------------------
# URL routing
# --------------------------------------------------------------------------

def is_our_host(host: Optional[str]) -> bool:
    if not host:
        return False
    host = host.lower().split(":")[0]
    return host in ARCHIVE_HOSTS


def split_site_url(url: str) -> Optional[tuple[str, str, str]]:
    """Return ``(path, query, fragment)`` for URLs on the original site."""
    parsed = urllib.parse.urlsplit(url)
    if not is_our_host(parsed.hostname):
        return None
    path = urllib.parse.unquote(parsed.path).lstrip("/")
    return path, parsed.query, parsed.fragment


def normalise_httrack(path: str, query: str) -> tuple[str, str]:
    """Translate HTTrack-style file names back into Drupal paths.

    ``talk@61.html`` -> ``talk?61``, ``node@page=1.html`` -> ``node?page=1``,
    ``sponsors.1.html`` -> ``sponsors``, ``schedule.html`` -> ``schedule``,
    ``index.html`` -> ````.
    """
    if path.endswith(".html") and "@" in path:
        path, _, query = path[:-5].partition("@")
        query = query.replace("&amp;", "&")
        return path, query
    if path.endswith(".html"):
        stem = path[:-5]
        stem = re.sub(r"\.\d+$", "", stem)
        if stem.rsplit("/", 1)[-1] == "index":
            stem = stem[: -len("index")].rstrip("/")
        return stem, query
    return path, query


def route_for(path: str, query: str = "") -> str:
    """Map an original site path/query onto a route on the rebuilt site."""
    path, query = normalise_httrack(path.strip("/"), query)
    suffix = Path(path).suffix.lower()
    if suffix in STATIC_EXTENSIONS or path.split("/", 1)[0] in ASSET_DIRECTORIES:
        return "/" + path
    if not path:
        return "/"
    if query:
        mapper = QUERY_ROUTES.get(path)
        mapped = mapper(query) if mapper else None
        if mapped:
            return mapped
    return "/" + path + "/"


def output_path_for(route: str, mime_type: str = "") -> Path:
    relative = route.lstrip("/")
    if route.endswith("/"):
        relative += "index.html"
    elif not Path(relative).suffix and mime_type.startswith("text/html"):
        relative += "/index.html"
    return PROJECT_ROOT / relative


def is_excluded(path: str) -> bool:
    return any(pattern.search(path) for pattern in EXCLUDED_PATH_PATTERNS)


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------

@dataclass
class Resource:
    original_url: str
    timestamp: str
    mime_type: str
    length: int
    route: str
    captures: int = 1
    extra: dict = field(default_factory=dict)

    @property
    def is_text(self) -> bool:
        return self.mime_type.startswith(TEXT_MIME_PREFIXES) or (
            self.mime_type in TEXT_MIME_TYPES
        )

    @property
    def is_html(self) -> bool:
        return self.mime_type.startswith("text/html")

    @property
    def is_video(self) -> bool:
        return Path(self.route).suffix.lower() in VIDEO_EXTENSIONS

    @property
    def output_path(self) -> Path:
        return output_path_for(self.route, self.mime_type)

    @property
    def raw_path(self) -> Path:
        return RAW_DIR / self.output_path.relative_to(PROJECT_ROOT)

    @property
    def published_path(self) -> Path:
        """Where the file is served from: recordings are re-encoded to .mp4."""
        if self.is_video:
            return self.output_path.with_suffix(PUBLISHED_VIDEO_SUFFIX)
        return self.output_path

    @property
    def download_path(self) -> Path:
        """Where the capture is written: recordings never touch videos/ so that
        only the AV1 re-encodes can ever be committed."""
        if self.is_video:
            return ORIGINALS_DIR / self.output_path.relative_to(PROJECT_ROOT)
        return self.raw_path if self.is_text else self.output_path

    @property
    def already_fetched(self) -> bool:
        target = self.download_path
        if target.exists() and target.stat().st_size > 0:
            return True
        if self.is_video:
            # Legacy location (before originals were kept under .cache) or already encoded.
            return self.output_path.exists() or self.published_path.exists()
        return False

    def to_json(self) -> dict:
        return {
            "original_url": self.original_url,
            "timestamp": self.timestamp,
            "mime_type": self.mime_type,
            "length": self.length,
            "route": self.route,
            "captures": self.captures,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Resource":
        return cls(
            data["original_url"], data["timestamp"], data["mime_type"],
            data["length"], data["route"], data.get("captures", 1),
        )


def cdx_rows(host: str) -> list[list[str]]:
    query = urllib.parse.urlencode(
        {
            "url": f"{host}/*",
            "from": ARCHIVE_FROM,
            "to": ARCHIVE_TO,
            "output": "json",
            "fl": "timestamp,original,statuscode,mimetype,length",
            "filter": "statuscode:200",
        }
    )
    payload = archive_request(f"{CDX_ENDPOINT}?{query}")
    if not payload.strip():
        return []
    rows = json.loads(payload)
    return rows[1:]


def build_inventory() -> list[Resource]:
    best: dict[str, Resource] = {}
    seen_hosts: set[str] = set()
    for host in ARCHIVE_HOSTS:
        print(f"CDX inventory for {host} ...")
        rows = cdx_rows(host)
        print(f"  {len(rows)} successful captures")
        for timestamp, original_url, _status, mime_type, length in rows:
            parts = split_site_url(original_url)
            if parts is None:
                continue
            path, query, _fragment = parts
            if is_excluded(path):
                continue
            route = route_for(path, query)
            if query and route == "/" + path + "/":
                # Unknown query string on a plain page: keep only the bare page.
                continue
            canonical = urllib.parse.urlunsplit(
                ("http", "sotm-eu.org", "/" + urllib.parse.quote(path), query, "")
            )
            resource = Resource(canonical, timestamp, mime_type or "", int(length or 0), route)
            current = best.get(route)
            if current is None:
                best[route] = resource
            else:
                current.captures += 1
                if timestamp > current.timestamp:
                    resource.captures = current.captures
                    best[route] = resource
        seen_hosts.add(host)

    if not best:
        raise RuntimeError("The CDX API returned no captures")
    return sorted(best.values(), key=lambda item: item.route)


def load_inventory() -> list[Resource]:
    if not INVENTORY_FILE.exists():
        raise SystemExit("No inventory found - run `inventory` first")
    return [Resource.from_json(item) for item in json.loads(INVENTORY_FILE.read_text())]


def save_inventory(resources: Iterable[Resource]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    INVENTORY_FILE.write_text(
        json.dumps([item.to_json() for item in resources], indent=1) + "\n"
    )


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

def fetch_resources(
    resources: list[Resource],
    skip_videos: bool,
    only: Optional[re.Pattern],
    parallel: int = 1,
) -> None:
    failures: list[str] = []
    todo = [
        item for item in resources
        if not (skip_videos and item.is_video)
        and (only is None or only.search(item.route))
    ]
    log(f"Fetching {len(todo)} resources ...")

    def fetch_one(index: int, item: Resource) -> None:
        target = item.download_path
        if item.already_fetched:
            return
        log(f"[{index}/{len(todo)}] {item.route}  <-  {item.timestamp} {item.original_url}")
        try:
            url = replay_url(item.timestamp, item.original_url)
            if item.is_text:
                data = archive_request(url)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            else:
                archive_request(url, sink=target)
            log(f"    done {item.route}")
        except Exception as error:  # noqa: BLE001 - report and continue
            failures.append(f"{item.original_url}: {error}")
            log(f"    FAILED {item.route}: {error}")

    if parallel > 1:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            for index, item in enumerate(todo, 1):
                pool.submit(fetch_one, index, item)
    else:
        for index, item in enumerate(todo, 1):
            fetch_one(index, item)

    if failures:
        log(f"\n{len(failures)} resources failed:")
        for failure in failures:
            log("  " + failure)


# --------------------------------------------------------------------------
# Build (rewrite)
# --------------------------------------------------------------------------

WAYBACK_PREFIX = re.compile(r"(?:https?://web\.archive\.org)?/web/\d{1,14}[a-z_]*/", re.I)
ATTR_URL = re.compile(r"""(?P<attr>\b(?:href|src|action))\s*=\s*(?P<quote>["'])(?P<url>.*?)(?P=quote)""", re.I | re.S)
# Recording links: published route -> (original file name, Wayback URL of the original).
VIDEO_ORIGINALS: dict[str, tuple[str, str]] = {}
ARCHIVE_ICON = (
    '<img src="/userfiles/archive16.png" width="16" height="16" border="0" alt="archive.org">'
)
VIDEO_DOWNLOAD_LINK = re.compile(
    r"""(?P<link><a href="(?P<route>/videos/[^"]+)" title="[^"]*" target="_blank">"""
    r"""<img src="/userfiles/dl\.png"[^>]*></a>)"""
)
VIDEO_PLAYER = re.compile(
    r"""(?P<player><video\b[^>]*>\s*<source src="(?P<route>/videos/[^"]+)"[^>]*/?>\s*</video>)"""
)
# TU Wien's LectureTube (Opencast Matterhorn) streaming service no longer exists;
# its camera-icon links are pointed at the recording itself instead.
LECTURETUBE_CAMERA_LINK = re.compile(
    r"""<a href="http://(?:matterhorntest\.zserv|mh-engage\.ltcc)\.tuwien\.ac\.at/engage/[^"]*" """
    r"""title="Video" target="_blank">(?P<img><img src="/userfiles/camera\.png"[^>]*>)</a>"""
    r"""(?P<sep>\s*)<a href="(?P<route>/videos/[^"]+)\""""
)
# Social-sharing metadata must stay absolute, so it is rewritten to the new origin.
ABSOLUTE_META = re.compile(
    r"""(?P<prefix><(?:link\b[^>]*?\brel=["']image_src["'][^>]*?\bhref|meta\b[^>]*?\bproperty=["']og:[a-z:_]+["'][^>]*?\bcontent))"""
    r"""=(?P<quote>["'])(?P<url>[^"']*)(?P=quote)""",
    re.I,
)
CSS_URL = re.compile(r"""url\(\s*(?P<quote>["']?)(?P<url>[^"')]+)(?P=quote)\s*\)""", re.I)


def rewrite_url(value: str, base_url: str, absolute: bool = False) -> str:
    raw = html.unescape(value).strip()
    if not raw or raw.startswith(("#", "mailto:", "javascript:", "data:", "tel:")):
        return value
    raw = WAYBACK_PREFIX.sub("", raw)
    if raw.startswith("//"):
        raw = "http:" + raw
    joined = urllib.parse.urljoin(base_url, raw)
    parts = split_site_url(joined)
    if parts is None:
        return value if raw == html.unescape(value).strip() else html.escape(raw, quote=True)
    path, query, fragment = parts
    route = route_for(path, query)
    route = LINK_FIXES.get(route, route)
    if route.startswith("/videos/") and Path(route).suffix.lower() in VIDEO_EXTENSIONS:
        route = str(Path(route).with_suffix(PUBLISHED_VIDEO_SUFFIX))
    route = urllib.parse.quote(route, safe="/")
    if absolute:
        route = NEW_ORIGIN + route
    if fragment:
        route += "#" + fragment
    return html.escape(route, quote=True)


def rewrite_html(text: str, base_url: str) -> str:
    text = re.sub(
        r"<!-- BEGIN WAYBACK TOOLBAR INSERT -->.*?<!-- END WAYBACK TOOLBAR INSERT -->",
        "", text, flags=re.S,
    )
    text = re.sub(r"<script[^>]*>\s*_?_wm\.wombat.*?</script>", "", text, flags=re.S)

    def meta(match: re.Match) -> str:
        return f"{match.group('prefix')}={match.group('quote')}{rewrite_url(match.group('url'), base_url, absolute=True)}{match.group('quote')}"

    text = ABSOLUTE_META.sub(meta, text)

    def attr(match: re.Match) -> str:
        return f"{match.group('attr')}={match.group('quote')}{rewrite_url(match.group('url'), base_url)}{match.group('quote')}"

    text = ATTR_URL.sub(attr, text)
    # Embedded players: the recordings are published as AV1/AAC in MP4.
    text = re.sub(
        r"""(<source\b[^>]*\bsrc="/videos/[^"]*\.mp4"[^>]*\btype=")video/ogg(")""",
        r"\1video/mp4\2", text,
    )
    text = LECTURETUBE_CAMERA_LINK.sub(
        lambda m: f'<a href="{m.group("route")}" title="Video" target="_blank">{m.group("img")}</a>'
                  f'{m.group("sep")}<a href="{m.group("route")}"',
        text,
    )
    text = add_archive_links(text)

    # Dead third-party widgets: Google +1, Facebook Like, Twitter timelines.
    text = re.sub(
        r"<!-- \+1 button -->.*?<!-- end \+1 button -->\s*", "", text, flags=re.S
    )
    text = re.sub(
        r"""<iframe src="https://www\.facebook\.com/plugins/like\.php[^"]*"[^>]*></iframe>\s*""",
        "", text, flags=re.S,
    )
    text = re.sub(
        r"""<script src="(?:/js/widget\.js|https?://widgets\.twimg\.com/j/2/widget\.js)" type="text/javascript"></script><script type="text/javascript">\s*new TWTR\.Widget\(.*?</script>""",
        "", text, flags=re.S,
    )
    text = re.sub(r"""<div class="twitcol">\s*(?:<p>\s*</p>\s*)*</div>""", "", text, flags=re.S)
    text = re.sub(
        r"""<div id="block-block-3"[^>]*>\s*<div class="content">\s*</div>\s*</div>\s*""",
        "", text, flags=re.S,
    )
    # Drupal editorial login link (already commented out in the 2013 static mirror).
    text = re.sub(
        r"""\s*&middot;\s*<a href="/user/"[^>]*>(?:Editorial )?Login</a>""", "", text
    )
    text = text.replace(
        '<!-- removed for static version (N.W.)  &middot; <a href="/user/" title="Editorial Login" rel="nofollow">Editorial Login</a> -->',
        "",
    )
    # "Login to post comments" links on blog posts.
    text = re.sub(
        r"""<div class="node-links"><ul class="links inline"><li class="comment_forbidden first last">"""
        r"""<span><a href="/user/login/">Login</a> to post comments</span></li>\s*</ul></div>\s*""",
        "", text,
    )
    # Drupal "User login" sidebar block present in early-2011 captures.
    text = re.sub(
        r"""<div id="block-user-0"[^>]*>.*?</form>\s*</div>\s*</div>\s*""",
        "", text, flags=re.S,
    )
    return remove_dead_mailto(text)


# ``mailto:`` links to the sotm-eu.org domain, which now belongs to a third
# party. The pages obfuscate them with character entities, hence both forms.
DEAD_MAILTO = (
    r"""<a href="(?:mailto:|&#109;&#97;&#105;&#108;&#116;&#111;&#58;)[^"]*?"""
    r"""(?:sotm-eu\.org|&#115;&#111;&#116;&#109;&#45;&#101;&#117;&#46;&#111;&#114;&#103;)"[^>]*>[^<]*</a>"""
)


def remove_dead_mailto(text: str) -> str:
    # Footer: "Impressum · info@… · OSM Austria · …"
    text = re.sub(r"\s*&middot;\s*" + DEAD_MAILTO, "", text)
    # Call for papers: "For questions please contact info@…."
    text = re.sub(r"<p>For questions please contact " + DEAD_MAILTO + r"\.</p>\s*", "", text)
    # Call for sponsors: "…to receive our sponsorship brochure at<br /><b>sponsors@…</b>."
    text = re.sub(r" at<br />\s*<b>" + DEAD_MAILTO + r"</b>\.", ".", text)
    return re.sub(DEAD_MAILTO, "", text)


def add_archive_links(text: str) -> str:
    """Link every recording to its original file on archive.org.

    The icon row of each talk gets an archive.org icon after the download
    icon, and the two embedded players get a caption.
    """
    def original_for(route: str) -> Optional[tuple[str, str]]:
        return VIDEO_ORIGINALS.get(html.unescape(route))

    def icon_link(match: re.Match) -> str:
        info = original_for(match.group("route"))
        if info is None:
            return match.group(0)
        name, wayback = info
        title = html.escape(f"Original recording ({name}) on archive.org", quote=True)
        return (
            f'{match.group("link")} <a href="{wayback}" title="{title}" '
            f'target="_blank" rel="nofollow">{ARCHIVE_ICON}</a>'
        )

    def caption(match: re.Match) -> str:
        info = original_for(match.group("route"))
        if info is None:
            return match.group(0)
        name, wayback = info
        return (
            f'{match.group("player")}\n<p style="margin-top:1em;font-size:small;">'
            f'<a href="{wayback}" target="_blank" rel="nofollow">{ARCHIVE_ICON} '
            f'Original recording ({html.escape(name)}) on archive.org</a></p>'
        )

    text = VIDEO_DOWNLOAD_LINK.sub(icon_link, text)
    return VIDEO_PLAYER.sub(caption, text)


GITHUB_REPOSITORY = "https://github.com/openstreetmap/stateofthemap-eu-2011"
IMPRESSUM_CONTENT = re.compile(
    r"""(?P<open><div id="node-2" class="node clear-block">.*?<div class="content clear-block">\s*)"""
    r"""(?P<body>.*?)(?P<close>\s*</div>)""",
    re.S,
)


def annotate_impressum(text: str, wayback_url: str) -> str:
    """Mark the original Impressum as historical and name the current host.

    The original legal notice is kept verbatim but struck through, preceded
    by a note that this is a re-created copy hosted by the OpenStreetMap
    Foundation with a link to the repository.
    """
    notice = (
        '<div class="archive-notice" style="border:1px solid #39c;background:#ddecf3;'
        'padding:10px 15px;margin:0 0 1.5em 0;">\n'
        "<p><strong>About this copy of the website</strong></p>\n"
        "<p>This is a re-created historical copy of the original State of the Map Europe 2011 "
        "website (<code>sotm-eu.org</code>), rebuilt from the Internet Archive&#8217;s Wayback Machine "
        'and hosted by the <a href="https://osmfoundation.org/" title="OpenStreetMap Foundation">'
        "OpenStreetMap Foundation</a>. The organisations and addresses named in the original "
        "Impressum below are no longer responsible for this site; please direct enquiries and "
        f'corrections to the project repository on GitHub: <a href="{GITHUB_REPOSITORY}">'
        f'{GITHUB_REPOSITORY.split("://", 1)[1]}</a>.</p>\n'
        "<p>The original Impressum is retained below, struck through, for the historical record "
        f'(<a href="{wayback_url}" title="Original page in the Wayback Machine">archived original</a>).</p>\n'
        "</div>\n"
    )

    def wrap(match: re.Match) -> str:
        return (
            f'{match.group("open")}{notice}<del cite="{wayback_url}">\n'
            f'{match.group("body")}\n</del>{match.group("close")}'
        )

    return IMPRESSUM_CONTENT.sub(wrap, text, count=1)


def rewrite_css(text: str, base_url: str) -> str:
    text = WAYBACK_PREFIX.sub("", text)

    def css(match: re.Match) -> str:
        url = match.group("url").strip()
        if url.startswith("data:"):
            return match.group(0)
        joined = urllib.parse.urljoin(base_url, url)
        parts = split_site_url(joined)
        if parts is None:
            return match.group(0)
        return f"url({route_for(parts[0], parts[1])})"

    return CSS_URL.sub(css, text)


def rewrite_plain(text: str) -> str:
    text = WAYBACK_PREFIX.sub("", text)
    return re.sub(
        r"https?://(?:www\.)?sotm-eu\.org(?::80)?(?=[/\s\"'<]|$)", NEW_ORIGIN, text, flags=re.I
    )


def rewrite_xml(text: str, base_url: str) -> str:
    text = WAYBACK_PREFIX.sub("", text)

    def repl(match: re.Match) -> str:
        parts = split_site_url(match.group(0))
        if parts is None:
            return match.group(0)
        route = route_for(parts[0], parts[1])
        return NEW_ORIGIN + route + ("#" + parts[2] if parts[2] else "")

    return re.sub(r"https?://(?:www\.)?sotm-eu\.org(?::80)?[^\s\"'<]*", repl, text, flags=re.I)


def shim_script(key: str) -> str:
    body = QUERY_SHIMS[key]
    return (
        "<script type=\"text/javascript\">\n"
        "/* Static-site shim: redirect original ?query URLs to directory-style routes. */\n"
        "(function(){var q=location.search.replace(/^\\?/,'');if(!q)return;"
        "function go(u){location.replace(u+location.hash);}"
        f"{body}"
        "})();\n"
        "</script>\n"
    )


def inject_shim(text: str, key: str) -> str:
    script = shim_script(key)
    if script in text:
        return text
    match = re.search(r"<head[^>]*>", text, flags=re.I)
    if match:
        return text[: match.end()] + "\n" + script + text[match.end():]
    return script + text


def decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def build_site(resources: list[Resource]) -> None:
    manifest = []
    missing = []
    VIDEO_ORIGINALS.clear()
    for item in resources:
        if item.is_video:
            published_route = str(Path(item.route).with_suffix(PUBLISHED_VIDEO_SUFFIX))
            VIDEO_ORIGINALS[urllib.parse.quote(published_route, safe="/")] = (
                Path(item.route).name,
                f"https://web.archive.org/web/{item.timestamp}/{item.original_url}",
            )
    for item in resources:
        output = item.output_path
        if item.is_text:
            if not item.raw_path.exists():
                missing.append(item.route)
                continue
            text = decode(item.raw_path.read_bytes())
            if item.is_html:
                text = rewrite_html(text, item.original_url)
                key = item.route.strip("/")
                if key in QUERY_SHIMS:
                    text = inject_shim(text, key)
                if 'id="node-2"' in text and "<h2>Impressum</h2>" in text:
                    text = annotate_impressum(
                        text, f"https://web.archive.org/web/{item.timestamp}/{item.original_url}"
                    )
            elif item.mime_type == "text/css":
                text = rewrite_css(text, item.original_url)
            elif item.mime_type in ("application/xml", "application/rss+xml", "text/xml"):
                text = rewrite_xml(text, item.original_url)
            else:
                text = rewrite_plain(text)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="utf-8")
        elif not output.exists() and not item.is_video:
            missing.append(item.route)
            continue
        published = item.published_path
        manifest.append(
            {
                "path": str(published.relative_to(PROJECT_ROOT)),
                "route": item.route,
                "source": item.original_url,
                "timestamp": item.timestamp,
                "wayback": f"https://web.archive.org/web/{item.timestamp}/{item.original_url}",
                "mime_type": item.mime_type,
                "bytes": published.stat().st_size if published.exists() else None,
            }
        )

    write_generated_shims(resources)

    manifest.sort(key=lambda entry: entry["path"])
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")
    print(f"Built {len(manifest)} resources; manifest written to {MANIFEST_FILE.name}")
    if missing:
        print(f"{len(missing)} resources have not been fetched yet:")
        for route in missing:
            print("  " + route)


def write_generated_shims(resources: list[Resource]) -> None:
    """Create index pages for query-routed sections that have no capture."""
    routes = {item.route for item in resources}
    for key in QUERY_SHIMS:
        route = f"/{key}/"
        if route in routes:
            continue
        target = output_path_for(route)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
            "<meta charset=\"utf-8\" />\n"
            f"<title>State Of The Map Europe 2011 | {key}</title>\n"
            + shim_script(key)
            + "<meta http-equiv=\"refresh\" content=\"0; url=/schedule/\" />\n"
            "</head>\n<body>\n"
            "<p>Redirecting to the <a href=\"/schedule/\">schedule</a>.</p>\n"
            "</body>\n</html>\n",
            encoding="utf-8",
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=("inventory", "fetch", "build", "all"), default="all")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="seconds between Archive.org requests")
    parser.add_argument("--skip-videos", action="store_true", help="do not download videos/*")
    parser.add_argument("--only", type=re.compile, help="regex limiting which routes are fetched")
    parser.add_argument(
        "--parallel", type=int, default=1,
        help="concurrent downloads (request starts stay --delay apart); useful for videos/*",
    )
    parser.add_argument(
        "--max-cooldown", type=float, default=DEFAULT_MAX_COOLDOWN_SECONDS,
        help="longest pause (seconds) imposed on all requests after repeated Archive.org errors",
    )
    parser.add_argument(
        "--start-delay", type=float, default=0.0,
        help="seconds to wait before the first Archive.org request (e.g. 3600 to pause for an hour)",
    )
    parser.add_argument("--refresh-inventory", action="store_true", help="re-query the CDX API even if cached")
    args = parser.parse_args(argv)

    if args.delay < 0.5:
        parser.error("--delay must be at least 0.5 seconds")
    if not 1 <= args.parallel <= 8:
        parser.error("--parallel must be between 1 and 8")
    THROTTLE.delay = args.delay
    THROTTLE.max_cooldown = max(COOLDOWN_BASE_SECONDS, args.max_cooldown)
    if args.start_delay > 0:
        log(f"Waiting {args.start_delay:.0f}s before contacting Archive.org ...")
        THROTTLE.pause(args.start_delay)

    if args.command in ("inventory", "all"):
        if args.refresh_inventory or not INVENTORY_FILE.exists() or args.command == "inventory":
            resources = build_inventory()
            save_inventory(resources)
            print(f"Inventory: {len(resources)} distinct resources")
    if args.command in ("fetch", "all"):
        fetch_resources(load_inventory(), args.skip_videos, args.only, args.parallel)
    if args.command in ("build", "all"):
        build_site(load_inventory())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("Interrupted")
