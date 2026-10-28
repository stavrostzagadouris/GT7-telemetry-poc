"""
Track position: which track the car is on, where on the lap, which turn.

GT7 telemetry has no track ID, so we match the car's X/Z position (and
direction of travel) against recorded centerlines in tracks/. Every known
layout is a candidate; each builds up "evidence" (metres driven while on its
line, going the right way) and the matcher locks on once one layout is
clearly ahead. Overlapping layouts (Lago Maggiore's many variants) stay tied
until the car drives a piece only one of them has; reverse layouts lose on
direction straight away.

If the car laps a track nobody has recorded, LapRecorder saves that lap as a
new track (start/finish = the line GT7 counts laps at) and it's recognised
from then on.
"""

import bisect
import csv
import json
import math
import time
from pathlib import Path

from trackbuild import (
    TRACKS_DIR, detect_turns, path_length, resample, save_track, write_meta,
)

GRID_M = 50            # spatial index cell; must be >= 2 * MATCH_M
MATCH_M = 25           # max distance from a centerline to count as on it
MIN_DOT = 0.3          # car heading vs track direction (cos ~73°)
LOCK_M = 200           # metres of matching before locking onto a clear leader
TIE_LOCK_M = 1500      # layouts still tied after this: take the leader for now
                       # (fixes itself when the car leaves the shared road)
KNOWN_LAP = 0.9        # matched this share of a layout's length in one go = known
UNLOCK_FRAMES = 180    # ~3 s off the locked line before giving it up
SEEK_EVERY = 10        # candidates are re-checked every Nth frame
TELEPORT_M = 100       # moved further than this between checks = reset/menu
MIN_LAP_M = 800        # shortest lap the recorder will save
LAP_DEBOUNCE_S = 5     # GT7 bumps lap count and lap time a few frames apart


class Track:
    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.meta = json.loads((TRACKS_DIR / f"{slug}.json").read_text())
        with open(TRACKS_DIR / f"{slug}.csv") as f:
            rows = list(csv.DictReader(f))
        self.dist = [float(r["dist_m"]) for r in rows]
        self.xz = [(float(r["x"]), float(r["z"])) for r in rows]
        last, first = self.xz[-1], self.xz[0]
        self.length = self.dist[-1] + math.hypot(first[0] - last[0], first[1] - last[1])

        with open(TRACKS_DIR / f"{slug}_turns.csv") as f:
            self.turns = [
                {
                    "label": r["name"] or f"Turn {r['turn']}",
                    "start": float(r["start_m"]),
                    "end": float(r["end_m"]),
                    "dir": r["direction"],
                }
                for r in csv.DictReader(f)
            ]
        self._starts = [t["start"] for t in self.turns]

        self._grid: dict[tuple[int, int], list[int]] = {}
        for i, (x, z) in enumerate(self.xz):
            self._grid.setdefault((int(x // GRID_M), int(z // GRID_M)), []).append(i)

    @property
    def name(self) -> str:
        return self.meta["name"]

    @property
    def named(self) -> bool:
        return self.meta.get("named", True)

    @property
    def start_offset(self) -> float | None:
        return self.meta.get("start_offset_m")

    def nearest(self, x: float, z: float) -> tuple[int | None, float]:
        """Closest centerline point within ~GRID_M, and its distance."""
        cx, cz = int(x // GRID_M), int(z // GRID_M)
        best_i, best_d2 = None, float("inf")
        for gx in (cx - 1, cx, cx + 1):
            for gz in (cz - 1, cz, cz + 1):
                for i in self._grid.get((gx, gz), ()):
                    px, pz = self.xz[i]
                    d2 = (px - x) ** 2 + (pz - z) ** 2
                    if d2 < best_d2:
                        best_i, best_d2 = i, d2
        return best_i, math.sqrt(best_d2)

    def match(self, x: float, z: float, vx: float, vz: float) -> tuple[int, float] | None:
        """(index, offset) if the car is on this line going the right way."""
        i, off = self.nearest(x, z)
        if i is None or off > MATCH_M:
            return None
        speed = math.hypot(vx, vz)
        if speed > 3:
            a, b = self.xz[i], self.xz[(i + 1) % len(self.xz)]
            seg = math.hypot(b[0] - a[0], b[1] - a[1]) or 1
            dot = ((b[0] - a[0]) * vx + (b[1] - a[1]) * vz) / (seg * speed)
            if dot < MIN_DOT:
                return None
        return i, off

    def turn_info(self, dist: float) -> dict:
        """Current turn (or None on a straight), and the next one coming up."""
        if not self.turns:
            return {"turn": None, "next_turn": None, "next_turn_in_m": None}
        k = bisect.bisect_right(self._starts, dist) - 1
        current = None
        if k >= 0 and dist <= self.turns[k]["end"]:
            current = self.turns[k]
        nxt = self.turns[(k + 1) % len(self.turns)]
        to_next = (nxt["start"] - dist) % self.length
        return {"turn": current, "next_turn": nxt, "next_turn_in_m": to_next}

    def position(self, i: int, off: float) -> dict:
        raw = self.dist[i]
        start = self.start_offset or 0.0
        lap_dist = (raw - start) % self.length
        return {
            "track": self.name,
            "track_id": self.slug,
            "lap_dist_m": lap_dist,
            "lap_frac": lap_dist / self.length,
            # where the drawn centerline starts relative to start/finish,
            # so the map can light up the lap from the right place
            "start_frac": start / self.length,
            "offset_m": off,
            **self.turn_info(raw),
        }

    def set_start(self, i: int) -> None:
        self.meta["start_offset_m"] = round(self.dist[i])
        write_meta(self.slug, self.meta)

    def rename(self, name: str) -> None:
        self.meta["name"] = name
        self.meta["named"] = True
        write_meta(self.slug, self.meta)

    def map_points(self, step: int = 4) -> list[tuple[float, float]]:
        return self.xz[::step]


def load_tracks() -> list[Track]:
    return [Track(p.stem) for p in sorted(TRACKS_DIR.glob("*.json"))]


class LapRecorder:
    """Collects X/Z for a lap, line to line, so unknown tracks can be saved."""

    def __init__(self) -> None:
        self.points: list[tuple[float, float]] | None = None

    def abort(self) -> None:
        self.points = None

    def feed(self, x: float, z: float) -> None:
        if self.points is None:
            return
        if self.points:
            px, pz = self.points[-1]
            step = math.hypot(x - px, z - pz)
            if step > TELEPORT_M / 4:
                self.abort()   # reset or rewind mid-lap: this lap is no good
                return
            if step < 0.5:
                return
        self.points.append((x, z))

    def boundary(self, known: bool) -> list[tuple[float, float]] | None:
        """Crossed the line: return the finished lap if worth saving, start a new one."""
        lap, self.points = self.points, []
        if not lap or known or len(lap) < 10:
            return None
        if math.hypot(lap[-1][0] - lap[0][0], lap[-1][1] - lap[0][1]) > 40:
            return None   # didn't come back to where it started
        if path_length(lap) < MIN_LAP_M:
            return None
        return lap


class TrackMatcher:
    """Feed telemetry frames in; get lap position for the current track."""

    def __init__(self, on_recorded=None) -> None:
        """`on_recorded(track)` is called when a new track has been saved."""
        self.tracks = load_tracks()
        self.on_recorded = on_recorded
        self.active: Track | None = None
        self.evidence = {t.slug: 0.0 for t in self.tracks}
        self.recorder = LapRecorder()
        self._misses = 0
        self._tick = 0
        self._last_check_xz: tuple[float, float] | None = None
        self._lap_key: tuple[int, int] | None = None
        self._last_boundary = 0.0

    def get(self, slug: str) -> Track | None:
        return next((t for t in self.tracks if t.slug == slug), None)

    def _check_candidates(self, x, z, vx, vz) -> None:
        moved = 0.0
        if self._last_check_xz:
            moved = math.hypot(x - self._last_check_xz[0], z - self._last_check_xz[1])
        self._last_check_xz = (x, z)
        if moved > TELEPORT_M:
            self.evidence = dict.fromkeys(self.evidence, 0.0)
            return
        for t in self.tracks:
            hit = t.match(x, z, vx, vz)
            self.evidence[t.slug] = self.evidence[t.slug] + moved if hit else 0.0

    def _pick(self) -> Track | None:
        ranked = sorted(self.tracks, key=lambda t: (-self.evidence[t.slug], t.length))
        if not ranked:
            return None
        best = ranked[0]
        lead = self.evidence[best.slug]
        rivals = [t for t in ranked[1:] if self.evidence[t.slug] > 0]
        if lead >= LOCK_M and not rivals:
            return best
        # A layout matched for a whole lap beats one that merely shares road
        # with it; failing that, after TIE_LOCK_M take the leader for now.
        full = [t for t in ranked if self.evidence[t.slug] >= KNOWN_LAP * t.length]
        if full:
            return max(full, key=lambda t: t.length)
        if lead >= TIE_LOCK_M:
            return best
        return None

    def _lap_was_known(self) -> bool:
        return any(self.evidence[t.slug] >= KNOWN_LAP * t.length for t in self.tracks)

    def _is_boundary(self, t: dict) -> bool:
        """True when the car has just crossed the start/finish line."""
        key = (t["lap_count"], t["last_lap_ms"])
        prev, self._lap_key = self._lap_key, key
        if prev is None or key == prev:
            return False
        lap_up = key[0] > prev[0] and t["speed_kmh"] > 30   # not a standing start
        new_time = key[1] != prev[1] and key[1] > 0
        now = time.monotonic()
        if (lap_up or new_time) and now - self._last_boundary > LAP_DEBOUNCE_S:
            self._last_boundary = now
            return True
        return False

    def update(self, t: dict) -> dict | None:
        x, z = t["pos"][0], t["pos"][2]
        vx, vz = t["vel"][0], t["vel"][2]
        boundary = self._is_boundary(t)

        if not t["on_track"]:
            self.recorder.abort()
            self._last_check_xz = None
            return None

        self._tick += 1
        if self._tick % SEEK_EVERY == 0:
            self._check_candidates(x, z, vx, vz)
            if self.active is None or self._misses:
                pick = self._pick()
                if pick is not None and pick is not self.active:
                    self.active, self._misses = pick, 0

        pos = None
        if self.active is not None:
            hit = self.active.match(x, z, vx, vz)
            if hit is None:
                self._misses += 1
                if self._misses > UNLOCK_FRAMES:
                    self.active = None
            else:
                self._misses = 0
                if boundary and self.active.start_offset is None:
                    self.active.set_start(hit[0])
                pos = self.active.position(*hit)

        self.recorder.feed(x, z)
        if boundary:
            # Known = one layout matched the whole lap without a break. Not
            # "something was locked": a provisional lock on a layout that
            # shares road with this one must not stop it being recorded.
            lap = self.recorder.boundary(known=self._lap_was_known())
            if lap:
                self._save_recorded(lap)
        return pos

    def _save_recorded(self, lap) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M")
        slug = "recorded-" + time.strftime("%Y%m%d-%H%M%S")
        center = resample(lap)
        save_track(slug, {
            "name": f"New track ({stamp})",
            "named": False,
            "course_id": None,
            "official_length_m": None,
            "recorded_length_m": round(path_length(lap)),
            "source": f"recorded {stamp}",
            "start_offset_m": 0,
        }, center, detect_turns(center))
        track = Track(slug)
        self.tracks.append(track)
        self.evidence[slug] = 0.0
        self.active, self._misses = track, 0
        print(f"Recorded a lap of an unknown track ({track.length:.0f} m) -> tracks/{slug}")
        if self.on_recorded:
            self.on_recorded(track)
