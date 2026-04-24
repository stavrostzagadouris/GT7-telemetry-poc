# GT7 Telemetry — Status

## What's done

- Live UDP telemetry dashboard reading encrypted GT7 packets from PS5
- Fixed several wrong packet offsets (suspension, wheel RPS, car code, loading flag)
- Car names resolved at startup by fetching ddm999/gt7info `cars.csv`
- Lap timing shows best and last lap only (no current lap — GT7 doesn't broadcast it)

## Nürburgring turn detection — planned, not started

Goal: show the current turn name as the car passes through it.

**Findings:**
- GT7 packet has no track ID field — track is unidentifiable from telemetry alone
- No public coordinate-to-turn-name database exists for GT7's Nürburgring
- The GT7Tracks ML project (vthinsel/GT7Tracks) is overkill; a simple nearest-neighbor lookup is sufficient

**Plan:**
1. Add a lap recorder mode to `gt7_telemetry.py` — logs X/Z positions to CSV during a lap
2. Build a small annotation script — scrub the recording and tag turn entry points with names
3. At runtime, do a nearest-neighbor lookup against the annotated CSV to show current turn

## Next session

**Build a web app** (not a terminal dashboard).

Start fresh or port `gt7_telemetry.py` as a backend. Frontend displays the same telemetry data in a browser.
