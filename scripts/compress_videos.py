#!/usr/bin/env python3
"""Re-encode the SotM-EU 2011 talk recordings to AV1 for publication.

The Wayback Machine holds the recordings as 640x480 Theora/Vorbis Ogg files
(and a few H.264 MP4 lightning talks) totalling ~14 GB. They are slides plus a
small camera inset, so a CRF encode shrinks them roughly tenfold while staying
visually lossless (SSIM > 0.99).

For each original in ``.cache/originals/videos/`` (where the import script
stores the downloads, so that nothing but AV1 output ever exists under
``videos/``) that has no AV1 encode yet, the script:

1. encodes it with SVT-AV1 (``--crf``, ``--preset``) and mono AAC audio (the
   recordings are effectively mono) into a temporary file under ``.cache/``;
2. checks the duration matches the source;
3. if the result exceeds ``--cap-mb`` (GitHub rejects files over 100 MB),
   re-encodes with a VBR target computed from the duration, so every output
   fits under the cap;
4. installs the output as ``videos/<name>.mp4``. Originals still found in
   ``videos/`` from the earlier layout are processed the same way and moved
   to ``.cache/originals/videos/``.

``--watch`` keeps scanning until every recording listed in the archive
inventory has been encoded, so it can run alongside the download.
Results are appended to ``.cache/compress-report.json``.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VIDEO_DIR = PROJECT_ROOT / "videos"
ORIGINALS_DIR = PROJECT_ROOT / ".cache" / "originals" / "videos"
INVENTORY_FILE = PROJECT_ROOT / ".cache" / "inventory.json"
REPORT_FILE = PROJECT_ROOT / ".cache" / "compress-report.json"
SOURCE_SUFFIXES = {".ogg", ".ogv", ".mp4", ".m4v", ".webm", ".mov", ".avi"}
AUDIO_KBPS = 48


def run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True)


def probe(path: Path) -> dict:
    result = run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,size:stream=codec_type,codec_name,width,height",
        "-of", "json", str(path),
    ])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    data = json.loads(result.stdout)
    info = {
        "duration": float(data["format"].get("duration") or 0),
        "size": int(data["format"].get("size") or 0),
        "video_codec": None,
        "audio_codec": None,
        "width": None,
        "height": None,
    }
    for stream in data.get("streams", []):
        if stream["codec_type"] == "video" and info["video_codec"] is None:
            info.update(video_codec=stream["codec_name"], width=stream.get("width"), height=stream.get("height"))
        elif stream["codec_type"] == "audio" and info["audio_codec"] is None:
            info["audio_codec"] = stream["codec_name"]
    return info


def encode(source: Path, target: Path, preset: int, crf: Optional[int], video_kbps: Optional[int], has_audio: bool) -> None:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-map", "0:v:0"]
    if has_audio:
        command += ["-map", "0:a:0"]
    command += ["-c:v", "libsvtav1", "-preset", str(preset), "-g", "300", "-pix_fmt", "yuv420p"]
    if video_kbps is not None:
        # Size-capped fallback: single-pass VBR at the computed target bitrate.
        command += ["-b:v", f"{video_kbps}k", "-svtav1-params", "rc=1:tune=0"]
    else:
        command += ["-crf", str(crf), "-svtav1-params", "tune=0"]
    if has_audio:
        command += ["-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ac", "1"]
    command += ["-movflags", "+faststart", str(target)]
    result = run(command)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "ffmpeg failed")


def ssim(source: Path, target: Path) -> Optional[float]:
    result = run([
        "ffmpeg", "-hide_banner", "-i", str(source), "-i", str(target),
        "-lavfi", "[0:v][1:v]ssim", "-f", "null", "-",
    ])
    match = re.search(r"All:([0-9.]+)", result.stderr)
    return float(match.group(1)) if match else None


def load_report() -> list[dict]:
    if REPORT_FILE.exists():
        return json.loads(REPORT_FILE.read_text())
    return []


def save_report(report: list[dict]) -> None:
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(json.dumps(report, indent=1) + "\n")


def expected_names() -> set[str]:
    if not INVENTORY_FILE.exists():
        return set()
    return {
        Path(item["route"]).name
        for item in json.loads(INVENTORY_FILE.read_text())
        if item["route"].startswith("/videos/")
    }


def is_encoded(stem: str) -> bool:
    output = VIDEO_DIR / f"{stem}.mp4"
    try:
        return output.exists() and probe(output)["video_codec"] == "av1"
    except RuntimeError:
        return False


def pending_sources() -> list[Path]:
    """Originals awaiting an AV1 encode.

    Downloads land in ``.cache/originals/videos/``; a source there is pending
    until ``videos/<stem>.mp4`` exists and is AV1. Files still sitting in
    ``videos/`` from before that layout are processed too and moved out.
    """
    sources = []
    for directory in (ORIGINALS_DIR, VIDEO_DIR):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*")):
            if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES or path.name.startswith("."):
                continue
            if path.with_name(path.name + ".part").exists():
                continue
            if directory == ORIGINALS_DIR:
                if is_encoded(path.stem):
                    continue
            else:
                try:
                    if probe(path)["video_codec"] == "av1":
                        continue
                except RuntimeError:
                    continue
            sources.append(path)
    return sources


def human(size: int) -> str:
    return f"{size / 1048576:.1f} MB"


def compress(source: Path, args: argparse.Namespace, report: list[dict]) -> None:
    info = probe(source)
    duration = info["duration"]
    output = VIDEO_DIR / (source.stem + ".mp4")
    temp = PROJECT_ROOT / ".cache" / "encoding" / f"{source.stem}.mp4"
    temp.parent.mkdir(parents=True, exist_ok=True)
    cap_bytes = int(args.cap_mb * 1048576)
    has_audio = info["audio_codec"] is not None
    print(f"{source.name}: {human(info['size'])}, {duration / 60:.1f} min, {info['video_codec']}/{info['audio_codec']}", flush=True)

    started = time.monotonic()
    entry = {"source": source.name, "output": output.name, "source_bytes": info["size"], "duration": round(duration, 1)}
    encode(source, temp, args.preset, args.crf, None, has_audio)
    entry["mode"] = f"crf {args.crf}"
    size = temp.stat().st_size
    print(f"  crf {args.crf}: {human(size)} in {time.monotonic() - started:.0f}s", flush=True)

    attempts = 0
    while size > cap_bytes and attempts < 3 and duration > 0:
        attempts += 1
        total_kbps = cap_bytes * 8 / duration / 1000
        video_kbps = max(24, math.floor((total_kbps - (AUDIO_KBPS if has_audio else 0)) * 0.92 ** attempts))
        print(f"  exceeds cap ({args.cap_mb} MB); VBR {video_kbps} kbps", flush=True)
        encode(source, temp, args.preset, None, video_kbps, has_audio)
        entry["mode"] = f"vbr {video_kbps}k"
        size = temp.stat().st_size
        print(f"  vbr {video_kbps}k: {human(size)}", flush=True)
    if size > cap_bytes:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"could not get below {args.cap_mb} MB")

    out_info = probe(temp)
    if abs(out_info["duration"] - duration) > max(2.0, duration * 0.01):
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"duration mismatch: {out_info['duration']:.1f}s vs {duration:.1f}s")
    if args.ssim:
        entry["ssim"] = ssim(source, temp)
        print(f"  ssim {entry['ssim']}", flush=True)

    ORIGINALS_DIR.mkdir(parents=True, exist_ok=True)
    if source.parent != ORIGINALS_DIR:
        shutil.move(str(source), str(ORIGINALS_DIR / source.name))
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(temp), str(output))
    entry.update(output_bytes=size, ratio=round(info["size"] / size, 1), seconds=round(time.monotonic() - started))
    report[:] = [item for item in report if item["source"] != source.name] + [entry]
    save_report(report)
    print(f"  -> {output.name} {human(size)} ({entry['ratio']}x smaller)", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--crf", type=int, default=32, help="SVT-AV1 CRF (default 32)")
    parser.add_argument("--preset", type=int, default=2, help="SVT-AV1 preset, lower is slower/better (default 2)")
    parser.add_argument("--cap-mb", type=float, default=95, help="maximum output size in MiB (default 95)")
    parser.add_argument("--ssim", action="store_true", help="measure SSIM against the source (slow)")
    parser.add_argument("--watch", action="store_true", help="keep scanning until every inventory recording is encoded")
    parser.add_argument("--only", type=re.compile, help="regex limiting which source file names are processed")
    args = parser.parse_args()

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit("ffmpeg and ffprobe are required")

    report = load_report()
    failures: dict[str, str] = {}
    while True:
        sources = [s for s in pending_sources() if args.only is None or args.only.search(s.name)]
        sources = [s for s in sources if s.name not in failures]
        for source in sources:
            try:
                compress(source, args, report)
            except RuntimeError as error:
                failures[source.name] = str(error)
                print(f"  FAILED: {error}", flush=True)
        if not args.watch:
            break
        done = {item["source"] for item in report} | set(failures)
        remaining = sorted(name for name in expected_names() if name not in done)
        if not remaining:
            break
        print(f"waiting for {len(remaining)} recording(s) to finish downloading ...", flush=True)
        time.sleep(60)

    if failures:
        print(f"\n{len(failures)} recording(s) failed:")
        for name, error in failures.items():
            print(f"  {name}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
