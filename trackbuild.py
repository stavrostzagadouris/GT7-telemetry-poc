"""
Turn a recorded lap (a list of X/Z positions) into a track in tracks/:

  <slug>.csv        centerline, one point every 5 m
  <slug>_turns.csv  turns auto-detected from curvature
  <slug>.json       name, source, official length, start/finish offset

Used by tools/import_gt7tracks.py and by the live lap recorder in track.py.
"""

import csv
import json
import math
import re
from pathlib import Path

TRACKS_DIR = Path(__file__).parent / "tracks"
STEP_M = 5
TURN_RADIUS_M = 250     # tighter than this counts as cornering
MIN_TURN_DEG = 25       # ...and a corner must turn at least this much
TIGHT_RADIUS_M = 60     # (or be this tight, however short)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "track"


def path_length(pts) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))


def find_clean_lap(pts, expected_m: float | None = None,
                   jump_m: float = 15, close_m: float = 15):
    """Find one continuous closed lap in a raw position log.

    Splits the log wherever the car teleports (resets, menus), then looks for
    a stretch that comes back to where it started. If `expected_m` is given
    the lap must be within 6% of it. Returns the lap's points, or None.
    """
    segments, cur = [], []
    for p in pts:
        if cur and math.hypot(p[0] - cur[-1][0], p[1] - cur[-1][1]) > jump_m:
            segments.append(cur)
            cur = []
        if not cur or math.hypot(p[0] - cur[-1][0], p[1] - cur[-1][1]) > 0.05:
            cur.append(p)   # drop samples where the car is standing still
    segments.append(cur)

    min_m = 0.9 * expected_m if expected_m else 500
    for seg in segments:
        for i in range(0, len(seg), 30):
            dist = 0.0
            for j in range(i + 1, len(seg)):
                dist += math.hypot(seg[j][0] - seg[j - 1][0], seg[j][1] - seg[j - 1][1])
                if dist < min_m:
                    continue
                if expected_m and dist > 1.1 * expected_m:
                    break
                if math.hypot(seg[j][0] - seg[i][0], seg[j][1] - seg[i][1]) < close_m:
                    if not expected_m or abs(dist - expected_m) / expected_m < 0.06:
                        return seg[i:j + 1]
                    break
    return None


def resample(pts, step: float = STEP_M):
    """Points every `step` metres along the path: [(dist, x, z), ...]."""
    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    out, i, d = [], 0, 0.0
    while d < cum[-1]:
        while cum[i + 1] < d:
            i += 1
        a, b = pts[i], pts[i + 1]
        f = (d - cum[i]) / (cum[i + 1] - cum[i])
        out.append((d, a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
        d += step
    return out


def detect_turns(center):
    """Corners from curvature: [(start_m, end_m, 'L'|'R'), ...].

    GT7's X/Z plane is mirrored relative to a standard math plot, so a
    positive heading change is a right turn (checked against the
    Nordschleife: Aremberg is a right, the Karussell a left).
    """
    n = len(center)

    def heading(i):
        a, b = center[i], center[(i + 2) % n]
        return math.atan2(b[2] - a[2], b[1] - a[1])

    H = [heading(i) for i in range(n)]

    def dh(i, w):
        d = H[(i + w) % n] - H[(i - w) % n]
        return (d + math.pi) % (2 * math.pi) - math.pi

    K = [dh(i, 3) / (6 * STEP_M) for i in range(n)]                  # 1/m
    Ks = [sum(K[(i + j) % n] for j in range(-2, 3)) / 5 for i in range(n)]

    segs, cur = [], None
    for i in range(n):
        on = abs(Ks[i]) > 1 / TURN_RADIUS_M
        sign = 1 if Ks[i] > 0 else -1
        if on and (cur is None or cur[2] != sign):
            if cur:
                segs.append(cur)
            cur = [i, i, sign]
        elif on:
            cur[1] = i
        elif cur and i - cur[1] > 6:
            segs.append(cur)
            cur = None
    if cur:
        segs.append(cur)

    merged = []
    for s in segs:   # same-direction pieces less than ~40 m apart are one corner
        if merged and merged[-1][2] == s[2] and s[0] - merged[-1][1] <= 8:
            merged[-1][1] = s[1]
        else:
            merged.append(list(s))

    turns = []
    for a, b, sign in merged:
        total_deg = abs(sum(K[a:b + 1]) * STEP_M * 180 / math.pi)
        r_min = 1 / max(abs(k) for k in Ks[a:b + 1])
        if total_deg >= MIN_TURN_DEG or r_min < TIGHT_RADIUS_M:
            turns.append((center[a][0], center[b][0], "R" if sign > 0 else "L"))
    return turns


def save_track(slug: str, meta: dict, center, turns, names: dict | None = None) -> None:
    """Write the three track files. `names` maps turn number -> name."""
    TRACKS_DIR.mkdir(exist_ok=True)
    names = names or {}
    with open(TRACKS_DIR / f"{slug}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["dist_m", "x", "z"])
        for d, x, z in center:
            w.writerow([f"{d:.0f}", f"{x:.2f}", f"{z:.2f}"])
    with open(TRACKS_DIR / f"{slug}_turns.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["turn", "name", "start_m", "end_m", "direction"])
        for k, (a, b, d) in enumerate(turns, 1):
            w.writerow([k, names.get(k, ""), f"{a:.0f}", f"{b:.0f}", d])
    write_meta(slug, meta)


def write_meta(slug: str, meta: dict) -> None:
    (TRACKS_DIR / f"{slug}.json").write_text(json.dumps(meta, indent=2) + "\n")
