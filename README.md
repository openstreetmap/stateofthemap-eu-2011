# State of the Map Europe 2011

Static rebuild of the website of the first European State of the Map
conference (Vienna, 15–17 July 2011), reconstructed from Internet Archive
Wayback Machine captures for deployment at <https://2011.stateofthemap.eu/>.

The original site lived at `www.sotm-eu.org` / `sotm-eu.org` (`2011.sotm-eu.org`
was never captured). It was a Drupal 6 site that the organisers converted to a
static HTTrack mirror in 2013. `sotm-eu.org` is now owned by an unknown person
and must not be used; everything here was retrieved exclusively through
`web.archive.org`, using captures made between 2010-11-15 and 2013-11-07:

<https://web.archive.org/web/*/sotm-eu.org/*>

## What is preserved

- Home page/blog (both pages), schedule, printable schedule, programme
  listings (by speaker, theme, title, random), all 56 talk detail pages, the
  two embedded-video pages, attendee lists, sponsors, call for sponsors, call
  for papers (incl. the submission form page), venue, travel, airport
  directions, recordings, proceedings, impressum/about, registration, the
  group picture and the "former SotMs" page — 100 pages in total.
- All slide decks (`slides/`, PDF/PPT/PPTX/ODP), the conference proceedings
  PDF, speaker photos (`persons/`), country flags, sponsor logos and the theme
  (Blueprint CSS, Drupal aggregated CSS/JS incl. jQuery 1.2.6).
- RSS feed (`rss.xml`) and the iCalendar file.
- All 50 talk recordings. The archive holds them as 640×480 Theora/Vorbis Ogg
  (plus seven H.264 MP4 lightning talks), ~14 GB in total, which is far beyond
  GitHub's 100 MB per-file limit. `scripts/compress_videos.py` re-encodes them
  to AV1 (SVT-AV1, CRF 32) with mono AAC audio in MP4 — the recordings are
  slides plus a small camera inset, so this is roughly 9× smaller at
  SSIM ≈ 0.998 (visually lossless) — and falls back to a size-targeted VBR
  encode for anything that would still exceed 95 MB. The published files are
  `videos/<name>.mp4` and are the only thing under `videos/` that is tracked;
  the originals are kept out of the repository under
  `.cache/originals/videos/`. Note that AV1 playback in Safari requires hardware decoding
  (Apple M3 / A17 Pro or newer). Next to every recording's download icon (and
  under the two embedded players) the rebuilt pages add an archive.org icon
  linking to the original, unmodified file in the Wayback Machine.

For every URL the newest successful capture inside the window was used, so
pages reflect the site's final (post-conference) state wherever one exists;
pages that were removed from the live site before the conference (call for
papers, preliminary programme, registration, …) come from early-2011 captures.
`archive-manifest.json` records the source URL, capture timestamp and Wayback
link for every file.

## URL layout

The Drupal site relied on query strings. The static rebuild uses
directory-style routes and ships tiny inline redirect shims so that original
links keep working when served by nginx (which preserves the query string
when redirecting `/talk` to `/talk/`):

| Original                      | Rebuilt                    |
| ----------------------------- | -------------------------- |
| `/`                           | `/`                        |
| `/schedule`, `/about`, …      | `/schedule/`, `/about/`, … |
| `/node/45`                    | `/node/45/`                |
| `/node?page=1`                | `/node/page/1/`            |
| `/talk?61`                    | `/talk/61/`                |
| `/video?17`                   | `/video/17/`               |
| `/programme?order=speaker`    | `/programme/speaker/`      |
| `/attendees?order=country`    | `/attendees/country/`      |
| `/talk@61.html` (2013 mirror) | `/talk/61/`                |

The HTTrack-style `*.html` names of the 2013 mirror are rewritten inside the
pages but are not served as URLs themselves.

## Restored assets

A few files were never captured by the Wayback Machine and were restored from
other sources by `scripts/restore_assets.py`:

- The twelve early-2011 pages predate Drupal's CSS/JS aggregation and link the
  individual files. Drupal 6 core files (`misc/*`, `modules/*`) come from the
  Drupal 6.20 release; their rule content is identical to the captured
  aggregated stylesheet. Blueprint `screen.css` and `print.css` are extracted
  verbatim from the captured aggregated stylesheets (the theme bundled a
  0.9.x snapshot), `src/grid.png` from the Blueprint 0.9 release.
- `print/jquery-1.6.1.min.js` comes from code.jquery.com; `print/sotm.css`
  was reconstructed from the theme's `.altalks` schedule rules.
- The 16px Facebook and Xing icons on the talk pages are the services' own
  2011 favicons (via the Wayback Machine); `userfiles/osm12.png` is a scaled
  copy of the captured `osm16.png`.
- Five speaker photos used only by the April 2011 preliminary programme were
  never archived: `steve8.jpg` is replaced by the later `steve8.png` of the
  same user, the other four by the site's own generic avatar `persons/osm.png`.

Never captured and still missing: `slides/LT09` (linked from the schedule),
`userfiles/quisse.gif` and `userfiles/unibrau2.jpg` (two blog-post images; the
alt text remains).

## Changes from the original

- All links to `sotm-eu.org` are relative to the new site root; `og:image`,
  `image_src`, the RSS feed and the iCalendar file point at
  `https://2011.stateofthemap.eu/`. Recording links and the two embedded
  players point at the re-encoded `videos/*.mp4` files.
- The Impressum (also its earlier captures at `/about/` and `/node/2/`) is
  prefixed with a notice that this is a re-created historical copy hosted by
  the OpenStreetMap Foundation, linking to this repository and to the archived
  original; the original legal text is retained but struck through.
- All `mailto:` links to `info@sotm-eu.org` / `sponsors@sotm-eu.org` were
  removed (footer, call for papers, call for sponsors): the domain is no
  longer controlled by the organisers.
- Dead third-party widgets were removed: the Twitter timeline widgets, the
  Google +1 button and the Facebook Like iframe. The static social links
  (Twitter, Flickr, YouTube, Delicious, iCal, OSM, RSS) remain.
- Drupal's user-login block, the "Editorial Login" footer link, "Login to post
  comments" links and the `/user/*`, `/admin` and `/al/*` routes were dropped;
  they cannot function on a static site. The call-for-papers submission form
  is kept as a historical page but is inert.
- External links (OSM wiki, sponsors, …) are kept as they were in 2011–2013
  and may no longer resolve. The one exception is TU Wien's LectureTube
  streaming service (`matterhorntest.zserv` / `mh-engage.ltcc.tuwien.ac.at`),
  which has been shut down: the 123 camera-icon links that opened a talk there
  now open the recording itself (`videos/*.mp4`).

## Rebuild

The checked-in site is ready to serve from the repository root. To repeat or
resume the import (Python 3.9+, standard library only; image conversion uses
`sips` on macOS or Pillow; video encoding needs ffmpeg with SVT-AV1):

```sh
python3 scripts/import_wayback.py inventory             # query the CDX API
python3 scripts/import_wayback.py fetch --skip-videos   # pages and assets
python3 scripts/import_wayback.py fetch --only '^/videos/' --parallel 4   # ~14 GB
python3 scripts/compress_videos.py --watch --ssim       # AV1 re-encode, can run alongside
python3 scripts/import_wayback.py build                 # rewrite into the site tree
python3 scripts/restore_assets.py                       # see "Restored assets"
python3 scripts/validate_site.py
```

`import_wayback.py` with no sub-command runs inventory, fetch and build. It
only ever contacts `web.archive.org`, identifies itself with a descriptive
User-Agent, keeps request starts at least `--delay` seconds apart (default
2 s, also across `--parallel` streams), backs off on errors and resumes
existing files. Raw text captures are cached under `.cache/raw/` so the
rewrite rules can be re-run offline with `build`. `compress_videos.py` writes
a per-file report (sizes, mode, SSIM) to `.cache/compress-report.json`.

## Local preview

```sh
docker compose up --build
```

and open <http://localhost:8080/>, or run `python3 -m http.server 8000` in the
repository root.

## Container image

The `Dockerfile` is arranged so that the two architectures built by CI share
their large layers. All content is prepared once in a native build stage with
normalised timestamps (pages: the commit time from `SOURCE_DATE_EPOCH`; media:
a fixed date), then copied with `COPY --link` into top-level directories
(`/videos`, `/slides`, `/site`) that nginx reaches through symlinks. Because a
layer archive records every parent directory it touches except the root, this
makes each content layer depend on nothing but its own files: the ~1.4 GB
`videos` and ~0.4 GB `slides` layers have the same digest on amd64 and arm64
and are only re-uploaded when a file actually changes.

## Thank you, Internet Archive

None of this would exist without the [Internet Archive](https://archive.org/)
and its Wayback Machine, which quietly kept every page, slide deck and 14 GB
of talk recordings for more than a decade after the original site went dark.
If this archive is useful to you, please consider
[donating to the Internet Archive](https://archive.org/donate).

## AI assistance

GitHub Copilot (Claude) was used to survey the Wayback Machine captures, write
the import, restoration and validation scripts, and check the reconstructed
site.
