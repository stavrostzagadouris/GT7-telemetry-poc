#!/usr/bin/env python3
"""
Import the lap dumps from vthinsel/GT7Tracks (MIT) into tracks/.

Each dump is a raw position log of someone driving the layout; this finds one
clean lap in it (checked against GT7's official length for that layout),
resamples it and auto-detects the turns.

Run from the repo root:  .venv/bin/python tools/import_gt7tracks.py
"""

import csv
import io
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trackbuild import (  # noqa: E402
    TRACKS_DIR, detect_turns, find_clean_lap, path_length, resample, save_track, slugify,
)

RAW = "https://raw.githubusercontent.com/vthinsel/GT7Tracks/master/dumps/{}.csv"
COURSES = "https://raw.githubusercontent.com/ddm999/gt7info/web-new/_data/db/course.csv"

# Course IDs with a dump in that repo. 95 (Nordschleife) is already in
# tracks/ from a hand-cleaned version of the same dump.
DUMP_IDS = [119, 154, 346, 358, 365, 439, 470, 602, 790, 791, 792, 793, 794,
            795, 846, 1224, 1225, 1226, 1231, 1232, 1233]


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode("utf-8")


def main() -> None:
    courses = {int(r["ID"]): r for r in csv.DictReader(io.StringIO(fetch(COURSES)))}
    for cid in DUMP_IDS:
        name = courses[cid]["Name"]
        official = float(courses[cid]["Length"])
        slug = slugify(name)
        rows = csv.DictReader(io.StringIO(fetch(RAW.format(cid))))
        pts = [(float(r["x"]), float(r["z"])) for r in rows]   # their "z" is packet Z
        lap = find_clean_lap(pts, expected_m=official)
        if lap is None:
            print(f"SKIP {cid:5} {name}: no clean lap matching {official:.0f} m")
            continue
        center = resample(lap)
        turns = detect_turns(center)
        save_track(slug, {
            "name": name,
            "course_id": cid,
            "official_length_m": official,
            "source": "vthinsel/GT7Tracks (MIT)",
            "start_offset_m": None,
        }, center, turns)
        print(f"ok   {cid:5} {name}: {path_length(lap):.0f} m "
              f"(official {official:.0f}), {len(turns)} turns -> {slug}")


if __name__ == "__main__":
    main()
