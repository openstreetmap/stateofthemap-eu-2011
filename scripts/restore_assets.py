#!/usr/bin/env python3
"""Restore assets of the SotM-EU 2011 site that the Wayback Machine never captured.

Twelve pages captured in early 2011 (before Drupal's CSS/JS aggregation was
enabled) reference the un-aggregated Drupal 6 core files and the Blueprint CSS
framework bundled with the theme, and the printable schedule references jQuery
1.6.1 and its own stylesheet. These were never archived, but they can be
restored faithfully:

* Drupal 6 core CSS/JS/images come from the Drupal 6.20 release on GitHub. The
  rule content of the core stylesheets is identical to the captured aggregated
  ``css_33e91dd0...css`` file.
* Blueprint ``screen.css`` and ``print.css`` are extracted verbatim from the
  captured aggregated stylesheets (the theme bundled a 0.9.x snapshot that
  differs slightly from the 0.9 release); ``src/grid.png`` comes from the
  Blueprint 0.9 release on GitHub.
* ``print/jquery-1.6.1.min.js`` comes from code.jquery.com.
* ``print/sotm.css`` is reconstructed from the ``.altalks`` rules of the
  captured theme stylesheet.
* The 16px Facebook and Xing icons are the services' own 2011 favicons from
  the Wayback Machine; ``osm12.png`` is a scaled copy of the captured
  ``osm16.png``. ``archive16.png`` is archive.org's favicon, used by the
  rebuilt pages to link each recording to its original file on archive.org.

Image conversion uses ``sips`` (macOS) or Pillow when available.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = (
    "sotm-eu-2011-static-archive/1.0 "
    "(+https://2011.stateofthemap.eu/; rebuilding the SotM-EU 2011 site)"
)
REQUEST_DELAY = 2.0

DRUPAL_RAW = "https://raw.githubusercontent.com/drupal/drupal/6.20/"
BLUEPRINT_RAW = "https://raw.githubusercontent.com/joshuaclayton/blueprint-css/v0.9/"
BLUEPRINT_DIR = "sites/all/themes/blueprint/blueprint/blueprint/"

DOWNLOADS = {
    # Drupal 6.20 core files referenced by the early-2011 pages and core CSS.
    **{
        path: DRUPAL_RAW + path
        for path in (
            "misc/drupal.js",
            "misc/jquery.js",
            "misc/draggable.png",
            "misc/grippie.png",
            "misc/menu-collapsed.png",
            "misc/menu-expanded.png",
            "misc/menu-leaf.png",
            "misc/progress.gif",
            "misc/throbber.gif",
            "misc/tree-bottom.png",
            "misc/tree.png",
            "modules/node/node.css",
            "modules/system/defaults.css",
            "modules/system/system.css",
            "modules/system/system-menus.css",
            "modules/user/user.css",
        )
    },
    BLUEPRINT_DIR + "src/grid.png": BLUEPRINT_RAW + "blueprint/src/grid.png",
    "print/jquery-1.6.1.min.js": "https://code.jquery.com/jquery-1.6.1.min.js",
}

FAVICONS = {
    "userfiles/facebook.png": "https://web.archive.org/web/20110701id_/http://www.facebook.com/favicon.ico",
    "userfiles/xing.png": "https://web.archive.org/web/20110701id_/http://www.xing.com/favicon.ico",
    # Used by the rebuilt site to link each recording to its original on archive.org.
    "userfiles/archive16.png": "https://archive.org/favicon.ico",
}

AGGREGATED_SCREEN = PROJECT_ROOT / "sites/default/files/css/css_62c18ef81cf73417e5cbad4f78acd3a8.css"
AGGREGATED_PRINT = PROJECT_ROOT / "sites/default/files/css/css_c991ec007de3220f92cd50e73040af57.css"
THEME_BLUEPRINT_CSS = PROJECT_ROOT / "sites/all/themes/blueprint/css/blueprint.css"
THEME_SOTM_CSS = PROJECT_ROOT / "sites/all/themes/sotm/sotm.css"

PRINT_SOTM_CSS = """\
/* print/sotm.css - reconstructed for the static archive of sotm-eu.org.
   The original file was never captured by the Wayback Machine; these rules
   are the ".altalks" schedule styles from sites/all/themes/sotm/sotm.css
   plus the highlight behaviour used by the printable schedule. */

body {
  margin: 1em;
  font-family: "Helvetica Neue", Arial, Helvetica, sans-serif;
  color: #333;
  background: #fff;
}
h2 { margin: 1em 0 0.5em 0; }
a { text-decoration: none; color: #39c; }

.altalks a:link,
.altalks a:visited,
.altalks a:hover,
.altalks a:active,
.altalks a:focus { color: #333333; }

.altalks table {
  border-collapse: collapse;
  font-family: Arial Narrow, Verdana, Arial, Helvetica, Sans-Serif;
}
.altalks td {
  border: medium solid white;
  margin: 0;
  padding: 5px;
  vertical-align: top;
}
.altalks .timeline {
  border: 0;
  font-weight: bold;
  text-align: center;
  vertical-align: top;
  background-color: #F7F7F7;
}
.altalks .break {
  font-family: Verdana, Arial, Helvetica, Sans-Serif;
  text-align: center;
  background-color: #F7F7F7;
}
.altalks th {
  font-family: Verdana, Arial, Helvetica, Sans-Serif;
  font-weight: bold;
  text-align: center;
}
.altalks td.highlight {
  outline: 3px solid #333;
  font-weight: bold;
}

@media print {
  body { font-size: 9pt; }
  .altalks td { border-color: #fff; }
  .altalks td.highlight { outline: 2px solid #000; }
}
"""


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    time.sleep(REQUEST_DELAY)
    return data


def write(relative: str, data: bytes, note: str) -> None:
    target = PROJECT_ROOT / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    print(f"  {relative}  ({len(data)} bytes, {note})")


def css_rules(text: str) -> list[str]:
    """Split a Drupal-aggregated (comment-free, minified) stylesheet into rules."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return [chunk.strip() + "}" for chunk in text.split("}") if chunk.strip()]


def first_rule_of(path: Path) -> str:
    text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([{};:,])\s*", r"\1", text)
    return text.split("}")[0].strip() + "}"


def extract_blueprint() -> None:
    header = (
        "/* Blueprint CSS Framework 0.9.x as bundled with the Drupal Blueprint theme.\n"
        "   Extracted verbatim from the Drupal-aggregated stylesheet captured by the\n"
        "   Wayback Machine ({source}) for the static archive of sotm-eu.org. */\n"
    )
    screen_rules = css_rules(AGGREGATED_SCREEN.read_text(encoding="utf-8"))
    boundary = screen_rules.index(first_rule_of(THEME_BLUEPRINT_CSS))
    write(
        BLUEPRINT_DIR + "screen.css",
        (header.format(source=AGGREGATED_SCREEN.name) + "\n".join(screen_rules[:boundary]) + "\n").encode(),
        f"{boundary} rules extracted",
    )
    print_rules = css_rules(AGGREGATED_PRINT.read_text(encoding="utf-8"))
    write(
        BLUEPRINT_DIR + "print.css",
        (header.format(source=AGGREGATED_PRINT.name) + "\n".join(print_rules) + "\n").encode(),
        f"{len(print_rules)} rules extracted",
    )


def convert_image(source: Path, target: Path, size: int | None = None) -> bool:
    """Convert/scale an image to PNG with sips (macOS) or Pillow."""
    if shutil.which("sips"):
        command = ["sips", "-s", "format", "png"]
        if size:
            command += ["-z", str(size), str(size)]
        command += [str(source), "--out", str(target)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0 and target.exists():
            return True
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return False
    with Image.open(source) as image:
        image = image.convert("RGBA")
        if size:
            image = image.resize((size, size), Image.LANCZOS)
        image.save(target, "PNG")
    return True


def restore_icons() -> None:
    scratch = PROJECT_ROOT / ".cache" / "icons"
    scratch.mkdir(parents=True, exist_ok=True)
    for relative, url in FAVICONS.items():
        target = PROJECT_ROOT / relative
        if target.exists():
            continue
        raw = scratch / (Path(relative).stem + ".ico")
        raw.write_bytes(fetch(url))
        if convert_image(raw, target, size=16):
            print(f"  {relative}  (favicon from {url})")
        else:
            print(f"  {relative}  SKIPPED: no image converter available (install Pillow)")
    osm12 = PROJECT_ROOT / "userfiles/osm12.png"
    if not osm12.exists():
        if convert_image(PROJECT_ROOT / "userfiles/osm16.png", osm12, size=12):
            print("  userfiles/osm12.png  (scaled from osm16.png)")
        else:
            print("  userfiles/osm12.png  SKIPPED: no image converter available (install Pillow)")


def main() -> int:
    print("Downloading published copies ...")
    for relative, url in DOWNLOADS.items():
        if (PROJECT_ROOT / relative).exists():
            continue
        try:
            write(relative, fetch(url), url)
        except Exception as error:  # noqa: BLE001
            print(f"  {relative}  FAILED: {error}")
    print("Extracting Blueprint stylesheets ...")
    extract_blueprint()
    print("Reconstructing print/sotm.css ...")
    write("print/sotm.css", PRINT_SOTM_CSS.encode(), "reconstructed")
    print("Restoring icons ...")
    restore_icons()
    return 0


if __name__ == "__main__":
    sys.exit(main())
