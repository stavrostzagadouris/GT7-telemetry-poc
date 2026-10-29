# GT7 Telemetry — Status

## What's done

- Live UDP telemetry, terminal + web (FastAPI/WebSocket) dashboards sharing one parser
- Requests the `~` packet (0x158 bytes); decrypt falls back to `B`/`A` IV keys
- Added fields: shift-light RPM (0x88), clutch, tyre radius, wheel slip ratio, rotation,
  angular velocity, packet ID (dropped-packet count), TCS/ASM/handbrake/rev-limiter flags,
  grid position (pre-race only), steering/sway/heave/surge (B), filtered pedals (~)
- Web: per-client "latest frame only" sending, so a slow phone drops frames instead of lagging
- PS5 IP configurable via `--ps5-ip` / `GT7_PS5_IP`
- 19 track layouts imported from vthinsel/GT7Tracks (`tools/import_gt7tracks.py`)
- Track auto-detection: every layout is a candidate, direction-checked; locks after ~200 m
  with no rival, or provisionally after 1.5 km on shared road (self-corrects)
- Unknown tracks are recorded automatically (one clean lap, line to line) and named
  from the dashboard; start/finish line is learned on imported tracks

## To verify next time at the PS5

- `~` heartbeat actually returns 0x158-byte packets on current firmware
- Wheel slip reads ~1.00 when cruising (assumes 0xA4 is rad/s and 0xB4 is radius in m)
- Nordschleife locks on within a few hundred metres, and the corner names (all 49 turns,
  placed via nring.info pins + OpenStreetMap shape fit, ~6 m RMS) match what's on screen
- Capture a few raw packets to `tests/fixtures/` and add a `parse_packet` test
- Lap boundary detection (lap count / last lap time changes) on a real session, since
  both the recorder and the start/finish learning depend on it
- Skipped imports: Lago Maggiore Centre, Centre Reverse and East End. Their dumps don't
  match their official lengths; the "Centre" one is ~2016 m, i.e. probably East End.
  Just drive them; the recorder will pick them up.

## Voice coach (`coach.py`) — built, tested with fake packets only

- Rule-based detection -> LLM phrasing (endpoints from .env, thinking off) -> cleanTTS -> phone
- Detection thresholds are guesses; tune once real slip values are seen
  (lock-up: front slip < 0.85; wheelspin: rear slip > 1.15 or TCS)
- Corner-name callouts (learn mode, on by default, CORNERS button) tested in a simulated
  Nordschleife drive: each name arrives ~2.5 s before its section. Not yet tried on a real lap
- Phone screen kept on by a silent looping video (Wake Lock needs https); check it on the phone
- Corner names are spoken via Kokoro inline phonemes (`tracks/pronunciations.csv`); tune by ear
- "Safe to talk" needs track position; on unknown tracks it falls back to steering angle

## Ideas

- Pace notes ("Karussell, left, two hundred") as an optional mode
- Live delta to best lap by lap distance
