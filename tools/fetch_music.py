"""Downloads the music bed packs listed in assets/music/<pack>/LICENSES.md and prepares them the same way as on air.

The tracks are public domain (CC0, FreePD.com via its Internet Archive mirror). They are not in git to keep the repo
small; this rebuilds assets/music/<pack>/*.mp3 from the source URLs in each LICENSES.md table:
1.5 s fade in, 2.5 s fade out, two-pass loudnorm to -24 LUFS (true peak -2 dBTP), 48 kHz stereo MP3 128 kb/s,
metadata stripped. Files already there are skipped. Needs ffmpeg and ffprobe.

    python3 tools/fetch_music.py                 # all packs
    python3 tools/fetch_music.py house lofi      # some packs
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "assets/music"
ROW = re.compile(r"^\|\s*([^|]+?\.mp3)\s*\|.*?\|\s*(https://archive\.org/[^|\s]+)\s*\|")


def tracks(pack: Path) -> list[tuple[str, str]]:
    lic = pack / "LICENSES.md"
    return [(m.group(1), m.group(2)) for line in lic.read_text().splitlines() if (m := ROW.match(line))]


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def prepare(src: Path, dst: Path) -> None:
    dur = duration(src)
    fades = f"afade=t=in:d=1.5,afade=t=out:st={max(0.0, dur - 2.5):.3f}:d=2.5"
    norm = "loudnorm=I=-24:TP=-2:LRA=11"
    probe = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(src), "-af",
                            f"{fades},{norm}:print_format=json", "-f", "null", "-"],
                           capture_output=True, text=True, check=True).stderr
    m = json.loads(probe[probe.rindex("{"):probe.rindex("}") + 1])
    second = (f"{norm}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
              f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-af", f"{fades},{second}",
                    "-ar", "48000", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", "-map_metadata", "-1",
                    "-id3v2_version", "0", str(dst)], check=True)


def main(names: list[str]) -> int:
    packs = [ROOT / n for n in names] if names else sorted(p for p in ROOT.iterdir() if (p / "LICENSES.md").exists())
    failed = 0
    for pack in packs:
        for name, url in tracks(pack):
            dst = pack / name
            if dst.exists():
                continue
            print(f"{pack.name}/{name}")
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    src = Path(tmp) / "src.mp3"
                    req = urllib.request.Request(url, headers={"User-Agent": "hubie-music-fetch"})
                    with urllib.request.urlopen(req, timeout=120) as r:
                        src.write_bytes(r.read())
                    prepare(src, dst)
            except Exception as e:      # one bad track never stops the rest; the bed just has one less
                failed += 1
                print(f"  failed: {e}", file=sys.stderr)
                dst.unlink(missing_ok=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
