"""
Track position: where on the lap the car is, and which turn it's in.

GT7 telemetry has no track ID, so we match the car's X/Z position against
recorded centerlines in tracks/. A track is "locked" once the car has stayed
close to its centerline for a couple of seconds.

Centerline source: Nordschleife (GT7 course 95) lap dump from
vthinsel/GT7Tracks (MIT), cleaned and resampled every 5 m. Turn boundaries
are auto-detected from curvature; only a few are named so far.
"""

import bisect
import csv
import math
from pathlib import Path

TRACKS_DIR = Path(__file__).parent / "tracks"

MATCH_M = 25          # max distance from centerline to count as "on this track"
SEEK_EVERY = 10       # while unlocked, only full-scan every Nth frame (~0.8 ms each)
LOCK_CHECKS = 12      # consecutive matching checks (~2 s at 60 Hz) to lock on
UNLOCK_FRAMES = 300   # ~5 s off the line before we give it up
SEARCH_WINDOW = 40    # points (x5 m) either side of the last match


class Track:
    def __init__(self, name: str, slug: str) -> None:
        self.name = name
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
        self._last_idx: int | None = None

    def _nearest(self, x: float, z: float, idxs) -> tuple[int, float]:
        best_i, best_d2 = 0, float("inf")
        for i in idxs:
            px, pz = self.xz[i]
            d2 = (px - x) ** 2 + (pz - z) ** 2
            if d2 < best_d2:
                best_i, best_d2 = i, d2
        return best_i, math.sqrt(best_d2)

    def locate(self, x: float, z: float) -> tuple[float, float]:
        """Return (distance along lap in m, offset from centerline in m)."""
        n = len(self.xz)
        i, off = None, float("inf")
        if self._last_idx is not None:
            window = (
                (self._last_idx + k) % n
                for k in range(-SEARCH_WINDOW, SEARCH_WINDOW + 1)
            )
            i, off = self._nearest(x, z, window)
        if off > MATCH_M:
            i, off = self._nearest(x, z, range(n))
        self._last_idx = i
        return self.dist[i], off

    def turn_info(self, dist: float) -> dict:
        """Current turn (or None on a straight), and the next one coming up."""
        k = bisect.bisect_right(self._starts, dist) - 1
        current = None
        if k >= 0 and dist <= self.turns[k]["end"]:
            current = self.turns[k]
        nxt = self.turns[(k + 1) % len(self.turns)]
        to_next = (nxt["start"] - dist) % self.length
        return {"turn": current, "next_turn": nxt, "next_turn_in_m": to_next}

    def map_points(self, step: int = 4) -> list[tuple[float, float]]:
        return self.xz[::step]


class TrackMatcher:
    """Feeds car positions in, says which track (if any) we're on and where."""

    def __init__(self) -> None:
        self.tracks = [Track("Nürburgring Nordschleife", "nordschleife")]
        self.active: Track | None = None
        self._hits = {t.name: 0 for t in self.tracks}
        self._misses = 0
        self._tick = 0

    def update(self, x: float, z: float) -> dict | None:
        if self.active is not None:
            dist, off = self.active.locate(x, z)
            if off > MATCH_M:
                self._misses += 1
                if self._misses > UNLOCK_FRAMES:
                    self.active = None
                return None
            self._misses = 0
            return {
                "track": self.active.name,
                "lap_dist_m": dist,
                "lap_frac": dist / self.active.length,
                "offset_m": off,
                **self.active.turn_info(dist),
            }

        self._tick += 1
        if self._tick % SEEK_EVERY:
            return None
        for t in self.tracks:
            _, off = t.locate(x, z)
            self._hits[t.name] = self._hits[t.name] + 1 if off <= MATCH_M else 0
            if self._hits[t.name] >= LOCK_CHECKS:
                self.active = t
                self._misses = 0
        return None
