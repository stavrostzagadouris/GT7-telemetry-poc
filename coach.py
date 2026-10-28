"""
Voice coach: spots driving mistakes in the telemetry, has an LLM phrase each
callout fresh (with the facts and what it said recently), speaks it through an
OpenAI-compatible TTS server, and waits for a straight before talking.

Rules decide *what* happened; the LLM only decides *how to say it*, so it
can't invent problems that aren't in the data.
"""

import asyncio
import base64
import csv
import json
import os
import re
import time
from collections import deque
from pathlib import Path

import httpx

import env  # noqa: F401  (loads .env into os.environ)

# Blank LLM URL -> fixed fallback lines. Blank TTS URL -> captions only.
LLM_URL = os.environ.get("GT7_LLM_URL", "").strip().rstrip("/")
LLM_MODEL = os.environ.get("GT7_LLM_MODEL", "").strip()   # blank = first from /models
TTS_URL = os.environ.get("GT7_TTS_URL", "").strip().rstrip("/")
TTS_VOICE = os.environ.get("GT7_TTS_VOICE", "").strip() or "bm_george"

REFS_FILE = Path(__file__).parent / "coach_refs.json"
PRONUNCIATIONS_FILE = Path(__file__).parent / "tracks" / "pronunciations.csv"

FPS = 60                  # GT7 sends ~60 packets/s; frame counts below assume it
MIN_GAP_S = 7             # minimum quiet time between callouts
STALE_S = 10              # drop an in-lap callout if no straight came by then
DEBRIEF_STALE_S = 60      # lap debriefs can wait for a long straight
HISTORY = 8               # recent lines shown to the LLM so it doesn't repeat

SYSTEM_PROMPT = """\
You are a race engineer talking to your driver over team radio during a \
Gran Turismo 7 session. You can't see the screen; you only get telemetry \
facts as JSON.

How to answer:
- Spoken English only. No markdown, lists, emoji, or stage directions.
- In-lap callouts: one sentence, at most 18 words. Lap debriefs: two or three \
short sentences, at most 50 words.
- Name the corner when one is given. Round numbers and say them naturally \
("about eight k p h", "two tenths"). Never read out slip ratios, percentages \
of pedal travel, or field names; translate them into driving language.
- Give one concrete, actionable fix tied to the facts. Calm, direct, a bit \
of warmth; praise genuinely good laps.
- Only use the facts given. Never invent pit calls, flags, rivals, damage, \
weather, or numbers.
- Write corner and track names exactly as they appear in the facts.
- Vary your wording every time. Do not reuse phrasing from the recent lines \
you are shown.
"""

# Plain-English meaning of each event, sent along with the numbers so the
# model knows what phase of the corner it's talking about.
MEANING = {
    "lockup": "front tyres locked under braking on corner entry",
    "wheelspin": "rear tyres spinning under power on corner exit",
    "coasting": "off both pedals for over a second at speed, losing time",
    "rev_limiter": "engine bouncing off the rev limiter, upshift came late",
    "overlap": "pressing brake and throttle at the same time",
    "slow_corner": "minimum corner speed well below the driver's best lap",
    "lap_complete": "lap finished; give a short debrief of the lap",
}

FALLBACK = {
    "lockup": "Front lock-up{at}. Ease the brake pressure.",
    "wheelspin": "Wheelspin{at}. Smoother on the throttle.",
    "coasting": "Coasting{at}. Commit to brake or throttle.",
    "rev_limiter": "You're on the limiter{at}. Shift up earlier.",
    "overlap": "Brake and throttle together{at}. Pick one.",
    "slow_corner": "Slow through {turn} compared to your best.",
    "lap_complete": "Lap done, {lap_time}.",
}


def load_pronunciations(path: Path = PRONUNCIATIONS_FILE):
    """Regex + lookup for names Kokoro mispronounces (German corners etc.)."""
    try:
        with open(path) as f:
            table = {r["name"].lower(): r["phonemes"] for r in csv.DictReader(f)}
    except OSError:
        return None, {}
    if not table:
        return None, {}
    # longest first, so "Kleines Karussell" wins over "Karussell"
    names = sorted(table, key=len, reverse=True)
    pattern = re.compile(
        r"(?<!\w)(" + "|".join(re.escape(n) for n in names) + r")(?!\w)", re.I
    )
    return pattern, table


def for_speech(text: str, pattern, table: dict) -> str:
    """Swap known names for Kokoro's inline pronunciation, [word](/phonemes/).
    Only the audio uses this; captions keep the normal spelling."""
    if pattern is None:
        return text
    return pattern.sub(lambda m: f"[{m.group(0)}](/{table[m.group(0).lower()]}/)", text)


def fmt_laptime(ms: int) -> str | None:
    if ms is None or ms <= 0:
        return None
    m, rem = divmod(ms, 60000)
    return f"{m}:{rem / 1000:06.3f}"


class Detector:
    """Counts consecutive frames a condition holds; fires once per episode."""

    def __init__(self, frames: int) -> None:
        self.frames = frames
        self.count = 0

    def update(self, cond: bool) -> bool:
        if not cond:
            self.count = 0
            return False
        self.count += 1
        return self.count == self.frames


class Coach:
    def __init__(self, broadcast) -> None:
        """`broadcast(payload: str)` must deliver a message to every viewer."""
        self.broadcast = broadcast
        self.events: asyncio.Queue = asyncio.Queue()
        self.history: deque[str] = deque(maxlen=HISTORY)
        self.last: dict | None = None
        self.car_name = ""
        self.model = LLM_MODEL
        self.http = httpx.AsyncClient(timeout=10)
        self.say_pattern, self.say_table = load_pronunciations()

        self.lockup = Detector(4)
        self.wheelspin = Detector(10)
        self.coasting = Detector(FPS)          # 1 s
        self.limiter = Detector(FPS // 2)
        self.overlap = Detector(FPS // 3)

        try:
            self.refs: dict = json.loads(REFS_FILE.read_text())
        except (OSError, ValueError):
            self.refs = {}
        self._new_lap(None)
        self._last_lap_ms: int | None = None
        self._prev_turn: str | None = None
        self._last_said = 0.0

    # ---------- per-frame detection (sync, called from the UDP handler) ----------

    def _new_lap(self, t: dict | None) -> None:
        self.lap_turns: dict[str, dict] = {}
        self.lap_mistakes: list[dict] = []
        self.lap_fuel_start = t["fuel_l"] if t else None
        self.lap_top_kmh = 0.0

    def _where(self, t: dict) -> str | None:
        tp = t.get("track_pos")
        if not tp:
            return None
        if tp["turn"]:
            return tp["turn"]["label"]
        if tp["next_turn"] and tp["next_turn_in_m"] < 200:
            return tp["next_turn"]["label"]
        return None

    def _event(self, kind: str, t: dict, **facts) -> None:
        turn = self._where(t)
        # one callout per kind per corner per lap, so a bad corner isn't nagged
        if turn and any(m["event"] == kind and m.get("turn") == turn for m in self.lap_mistakes):
            return
        e = {"event": kind, "turn": turn, "speed_kmh": round(t["speed_kmh"]), **facts}
        self.lap_mistakes.append(e)
        self.events.put_nowait((time.monotonic(), e))

    def feed(self, t: dict) -> None:
        self.last = t
        self.car_name = t.get("car_name", self.car_name)

        if self._last_lap_ms is None:
            self._last_lap_ms = t["last_lap_ms"]
        elif t["last_lap_ms"] != self._last_lap_ms and t["last_lap_ms"] > 0:
            self._last_lap_ms = t["last_lap_ms"]
            self._lap_complete(t)

        if not t["on_track"] or t["paused"] or t["loading"]:
            return

        self.lap_top_kmh = max(self.lap_top_kmh, t["speed_kmh"])
        self._track_turns(t)

        moving = t["speed_kmh"] > 40
        slip = t["wheel_slip"]
        thr, brk = t["throttle_pct"], t["brake_pct"]

        if slip and self.lockup.update(moving and brk > 20 and min(slip[0], slip[1]) < 0.85):
            self._event("lockup", t, front_slip=round(min(slip[0], slip[1]), 2),
                        brake_pct=round(brk))
        if self.wheelspin.update(
            moving and thr > 50
            and ((slip and max(slip[2], slip[3]) > 1.15) or t["tcs_active"])
        ):
            self._event("wheelspin", t, rear_slip=slip and round(max(slip[2], slip[3]), 2),
                        tcs_active=t["tcs_active"], gear=t["gear"])
        if self.coasting.update(t["speed_kmh"] > 60 and thr < 5 and brk < 5):
            self._event("coasting", t, seconds=1.0)
        if self.limiter.update(t["rev_limiter"] or (t["max_rpm"] > 0 and t["rpm"] >= t["max_rpm"])):
            self._event("rev_limiter", t, gear=t["gear"], rpm=round(t["rpm"]))
        if self.overlap.update(thr > 20 and brk > 20):
            self._event("overlap", t, throttle_pct=round(thr), brake_pct=round(brk))

    def _track_turns(self, t: dict) -> None:
        """Per-corner min speed and braking point, compared to the best lap."""
        tp = t.get("track_pos")
        if not tp:
            self._prev_turn = None
            return
        cur = tp["turn"]["label"] if tp["turn"] else None
        nxt = tp["next_turn"]["label"] if tp["next_turn"] else None

        # braking point = metres before the turn where the brake first goes on
        if nxt and not cur and t["brake_pct"] > 15 and tp["next_turn_in_m"] < 300:
            rec = self.lap_turns.setdefault(nxt, {})
            rec.setdefault("brake_m_before", round(tp["next_turn_in_m"]))

        if cur:
            rec = self.lap_turns.setdefault(cur, {})
            rec["min_kmh"] = round(min(rec.get("min_kmh", 999), t["speed_kmh"]))
            rec["exit_kmh"] = round(t["speed_kmh"])

        if self._prev_turn and cur != self._prev_turn:
            self._turn_done(t, self._prev_turn)
        self._prev_turn = cur

    def _ref_key(self, t: dict) -> str | None:
        tp = t.get("track_pos")
        return f"{tp['track_id']}|{t['car_code']}" if tp else None

    def _turn_done(self, t: dict, turn: str) -> None:
        ref = self.refs.get(self._ref_key(t) or "", {}).get("turns", {}).get(turn)
        mine = self.lap_turns.get(turn, {})
        if not ref or "min_kmh" not in mine or "min_kmh" not in ref:
            return
        lost = ref["min_kmh"] - mine["min_kmh"]
        if lost < 6:
            return
        facts = {"min_kmh": mine["min_kmh"], "best_lap_min_kmh": ref["min_kmh"]}
        if "brake_m_before" in mine and "brake_m_before" in ref:
            facts["braked_m_earlier_than_best"] = mine["brake_m_before"] - ref["brake_m_before"]
        e = {"event": "slow_corner", "turn": turn, **facts}
        self.lap_mistakes.append(e)
        self.events.put_nowait((time.monotonic(), e))

    def _lap_complete(self, t: dict) -> None:
        key = self._ref_key(t)
        ref = self.refs.get(key or "", {})
        is_best = t["best_lap_ms"] > 0 and t["last_lap_ms"] <= t["best_lap_ms"]

        losses = []
        for turn, mine in self.lap_turns.items():
            r = ref.get("turns", {}).get(turn, {})
            if "min_kmh" in mine and "min_kmh" in r and r["min_kmh"] - mine["min_kmh"] >= 3:
                losses.append({"turn": turn, "kmh_slower_than_best": r["min_kmh"] - mine["min_kmh"]})
        losses.sort(key=lambda x: -x["kmh_slower_than_best"])

        fuel_used = (self.lap_fuel_start - t["fuel_l"]) if self.lap_fuel_start is not None else None
        e = {
            "event": "lap_complete",
            "lap_time": fmt_laptime(t["last_lap_ms"]),
            "best_lap": fmt_laptime(t["best_lap_ms"]),
            "delta_to_best_s": round((t["last_lap_ms"] - t["best_lap_ms"]) / 1000, 2)
                if t["best_lap_ms"] > 0 else None,
            "new_personal_best": is_best,
            "laps_done": t["lap_count"] - 1,
            "laps_total": t["total_laps"] if t["total_laps"] > 0 else None,
            "top_speed_kmh": round(self.lap_top_kmh),
            "mistakes": [{"event": m["event"], "turn": m.get("turn")}
                         for m in self.lap_mistakes if m["event"] != "slow_corner"][:6],
            "biggest_corner_losses": losses[:3],
            "hottest_tyre_c": round(max(t["tyre_temp"])),
        }
        if fuel_used and fuel_used > 0.05:
            e["fuel_used_l"] = round(fuel_used, 1)
            e["laps_of_fuel_left"] = round(t["fuel_l"] / fuel_used, 1)
        self.events.put_nowait((time.monotonic(), e))

        # A personal best (that covered most of the lap) becomes the reference.
        if key and is_best and len(self.lap_turns) >= 10:
            self.refs[key] = {"lap_ms": t["last_lap_ms"], "turns": self.lap_turns}
            try:
                REFS_FILE.write_text(json.dumps(self.refs, indent=1))
            except OSError:
                pass
        self._new_lap(t)

    # ---------- talking (async) ----------

    def _safe_to_talk(self) -> bool:
        t = self.last
        if t is None or not t["on_track"] or t["paused"]:
            return True
        tp = t.get("track_pos")
        if tp:
            if tp["next_turn"] is None:
                return True
            speed_ms = t["speed_kmh"] / 3.6
            return tp["turn"] is None and tp["next_turn_in_m"] > max(150, speed_ms * 3.5)
        if "steering_rad" in t:
            return abs(t["steering_rad"]) < 0.08
        return True

    async def _llm(self, event: dict) -> str | None:
        if not self.model:
            r = await self.http.get(f"{LLM_URL}/models")
            self.model = r.json()["data"][0]["id"]
        debrief = event["event"] == "lap_complete"
        facts = {"what": MEANING.get(event["event"]), "car": self.car_name or None, **event}
        if self.last and self.last.get("track_pos"):
            facts["track"] = self.last["track_pos"]["track"]
        recent = "\n".join(f"- {h}" for h in self.history) or "(nothing yet)"
        r = await self.http.post(f"{LLM_URL}/chat/completions", json={
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content":
                    f"Recent lines you said:\n{recent}\n\n"
                    f"Reply type: {'lap debrief' if debrief else 'in-lap callout'}\n"
                    f"Telemetry facts:\n{json.dumps(facts)}"},
            ],
            "temperature": 1.0,
            "top_p": 0.95,
            "presence_penalty": 0.6,
            "max_tokens": 120 if debrief else 50,
            # Qwen3-style models think by default, which costs seconds per
            # callout (or eats the whole token budget). Turn it off.
            "chat_template_kwargs": {"enable_thinking": False},
        })
        r.raise_for_status()
        text = r.json()["choices"][0]["message"].get("content") or ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip().strip('"')
        return text or None

    def _fallback(self, event: dict) -> str:
        turn = event.get("turn")
        return FALLBACK.get(event["event"], "Check your telemetry.").format(
            at=f" into {turn}" if turn else "", turn=turn or "that corner",
            lap_time=event.get("lap_time") or "",
        )

    async def _tts(self, text: str) -> bytes:
        r = await self.http.post(f"{TTS_URL}/audio/speech", json={
            "input": for_speech(text, self.say_pattern, self.say_table),
            "voice": TTS_VOICE, "response_format": "mp3",
        })
        r.raise_for_status()
        return r.content

    async def run(self) -> None:
        while True:
            created, event = await self.events.get()
            # merge anything else that piled up; keep the debrief if there is one
            batch = [(created, event)]
            while not self.events.empty():
                batch.append(self.events.get_nowait())
            debriefs = [b for b in batch if b[1]["event"] == "lap_complete"]
            created, event = debriefs[-1] if debriefs else batch[-1]
            stale = DEBRIEF_STALE_S if event["event"] == "lap_complete" else STALE_S

            text = None
            if LLM_URL:
                try:
                    text = await self._llm(event)
                except Exception as e:
                    print(f"coach: LLM failed ({e!r}), using fallback line")
            text = text or self._fallback(event)

            audio = b""
            if TTS_URL:
                try:
                    audio = await self._tts(text)
                except Exception as e:
                    print(f"coach: TTS failed ({e!r})")

            while (not self._safe_to_talk()
                   or time.monotonic() - self._last_said < MIN_GAP_S):
                if time.monotonic() - created > stale:
                    break
                await asyncio.sleep(0.1)
            else:
                self._last_said = time.monotonic()
                self.history.append(text)
                print(f"coach: {text}")
                self.broadcast(json.dumps({
                    "type": "coach",
                    "event": event["event"],
                    "text": text,
                    "audio": base64.b64encode(audio).decode() if audio else None,
                }))
