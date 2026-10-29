![GT7 Telemetry web dashboard](webDash.png)

# GT7 Telemetry

Decrypts Gran Turismo 7's Salsa20-encrypted UDP telemetry from a PS5 and shows it live. Two front-ends, same parser:

- **`gt7_web.py`**: phone-friendly browser dashboard with a live track map and a voice coach. Serves on port `7000`.
- **`gt7_telemetry.py`**: ANSI terminal dashboard.

Both show speed, gear, RPM with shift lights, pedals, per-wheel slip, tyre temperatures, fuel, lap times, TCS/ASM activity, and where you are on the track.

## Getting it running

You need:

- **A PS5 running Gran Turismo 7.**
- **A computer on the same home network** (Linux, macOS or Windows) with **Python 3.10 or newer**. Check with `python3 --version` (on Windows, `python --version`).
- **A phone** (optional) to show the dashboard while you drive. Any browser works.
- **Voice coach** (optional): an OpenAI-compatible LLM server and text-to-speech server, e.g. [cleanTTS](https://github.com/stavrostzagadouris/cleanTTS). Without them you still get the dashboard, and the coach falls back to fixed lines or captions.

### 1. Download and install (once)

```sh
git clone https://github.com/stavrostzagadouris/GT7-telemetry-poc.git
cd GT7-telemetry-poc
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

On Windows, use `python` instead of `python3`, `.venv\Scripts\pip` instead of `.venv/bin/pip`, and `copy` instead of `cp`.

### 2. Tell it where your PS5 is (once)

On the PS5, go to **Settings → Network → Connection Status → View Connection Status** and note the **IPv4 Address**, e.g. `192.168.1.50`.

Open `.env` in any text editor and put it after `GT7_PS5_IP=`:

```ini
GT7_PS5_IP=192.168.1.50
```

For the voice coach, also fill in the server addresses. Leave a line blank to skip it.

```ini
GT7_LLM_URL=http://<llm-host>:1234/v1
GT7_TTS_URL=http://<tts-host>:5000/v1
```

`.env` is git-ignored, so your addresses never end up in the repo.

> If your PS5's address keeps changing, give it a fixed one ("DHCP reservation") in your router's settings. For a one-off, you can also pass it when starting: `.venv/bin/python gt7_web.py --ps5-ip 192.168.1.50`.

### 3. Start it (every time you play)

1. Turn on the PS5 and start **Gran Turismo 7**.
2. On the computer, in the project folder, run:

   ```sh
   .venv/bin/python gt7_web.py
   ```

   (Windows: `.venv\Scripts\python gt7_web.py`.) Leave this window open while you play. **Ctrl+C** stops it.
3. Find the computer's own IP address:

   | System | Command |
   |---|---|
   | Linux | `hostname -I` (first address) |
   | macOS | `ipconfig getifaddr en0` |
   | Windows | `ipconfig` (the "IPv4 Address" line) |

4. On your phone, connected to the **same Wi-Fi**, open `http://<computer-ip>:7000`, e.g. `http://192.168.1.20:7000`.
5. Tap **TAP FOR COACH** to turn on the voice. Phones only allow sound after a tap. Tap again to mute. That first tap also keeps the phone's screen from turning off (see [Notes](#notes)).
6. Drive. The top-left badge says **ON TRACK** once data is flowing. The track map appears a few hundred metres into a known track.

Prefer the terminal? Run `.venv/bin/python gt7_telemetry.py` instead for the text dashboard. It has no coach or map. Run one or the other, not both.

### If it shows NO DATA

1. GT7 is running and you're past the title screen.
2. The PS5 IP is right. The computer's window prints `heartbeat -> <ip>` at startup; it should match the PS5's address.
3. The computer and PS5 are on the same network, and no firewall blocks incoming UDP port `33740`. On Windows, allow Python through the firewall when it asks.
4. Phone shows **OFFLINE**: the phone can't reach the computer. Check the address and port `7000`, and that the phone is on the same Wi-Fi, not mobile data.

### Updating later

```sh
git pull
.venv/bin/pip install -r requirements.txt
```

## Tracks

GT7 doesn't send a track ID, so the dashboard works out the track from where you're driving. It compares your position and direction against every saved track line in `tracks/`. Once one layout has matched for about 200 m with no rival, the map, lap distance and turn callouts appear.

**Included (19 layouts):**

| Track | Layouts |
|---|---|
| Nürburgring Nordschleife | Nordschleife, with every corner named |
| Red Bull Ring | Full, Short |
| Brands Hatch | Grand Prix, Indy |
| Goodwood | Motor Circuit |
| Alsace | Village and Test Course, each forward and reverse |
| Autodrome Lago Maggiore | Full Course, East, West, East End and West End, each forward and reverse except East End (reverse only) |

These come from recorded laps in [vthinsel/GT7Tracks](https://github.com/vthinsel/GT7Tracks) (MIT, see `tracks/LICENSE-GT7Tracks.txt`), imported with `tools/import_gt7tracks.py`. Turns are detected automatically from the track's shape.

- **Nordschleife:** all 49 detected turns carry their real corner names, from Hatzenbach to Galgenkopf. Corner positions came from [nring.info's corner map](https://nring.info/nurburgring-nordschleife-corners/), placed on the GT7 track by fitting the real track shape from [OpenStreetMap](https://www.openstreetmap.org/relation/38566) (© OpenStreetMap contributors) onto the GT7 line. The two agree to within about 6 m on average. Long sections such as Hatzenbach, Hohe Acht and Brünnchen cover several bends, so those names repeat, as drivers use them.
- **Other tracks:** turns show as "Turn 1", "Turn 2" and so on. To name one, fill in `name` in `tracks/<track>_turns.csv`.

**Shared road:** some layouts are built from the same pieces of tarmac, such as Brands Hatch Indy inside the GP circuit, or Lago Maggiore's variants. While you're on a shared stretch the dashboard can briefly show the wrong layout. It corrects itself as soon as you drive a piece only one of them has. From your second lap on it's correct straight away.

**Start/finish line:** imported tracks don't know where the line is. The first time you cross it, it's learned and saved to `tracks/<track>.json`, so lap distance counts from the line after that.

### Recording new tracks

There's nothing to set up. Drive any track that isn't in `tracks/`:

1. Cross the start/finish line, so recording starts.
2. Drive one full lap. A reset or teleport mid-lap throws that lap away, and the next one is recorded instead.
3. When you cross the line again, the lap is saved as `tracks/recorded-<date>-<time>.*` and is recognised from then on.
4. The dashboard shows a **New track recorded** card. It suggests the GT7 layouts whose official length is closest to what you drove. Pick one or type a name, then tap Save.

### Saving what you've driven to the repo

Everything the dashboard learns about tracks is a plain file in `tracks/`. After a session, `git status` shows what's new:

| You do | Files that change |
|---|---|
| Lap a track it doesn't know | 3 new files: `recorded-<date>-<time>.csv` (the line), `_turns.csv` (detected turns), `.json` (name, length) |
| Name it on the dashboard | that track's `.json` |
| Cross the start/finish line on an imported track for the first time | that track's `.json` (the learned line) |
| Name a turn yourself | `<track>_turns.csv` (fill in the `name` column) |

```sh
git add tracks/
git commit -m "Add recorded tracks"
git push
```

- Recorded files keep their date-stamped filenames after you name them; the name lives in the `.json`.
- The recorded line is your driving line, not the exact centre of the track. That's fine: matching allows 25 m either side. If a recording came out messy (lots of off-track moments), delete its 3 files and drive another lap.
- If you play on more than one computer, `git pull` first so it doesn't record tracks you already have.
- Recorded tracks contain only in-game coordinates. Nothing about you or your network is in them.

## Voice coach

`gt7_web.py` runs a race-engineer coach unless you pass `--no-coach`. It never sees the screen; it works from telemetry alone.

1. **Rules spot what happened** (`coach.py`): front lock-ups, wheelspin or TCS on exit, coasting, the rev limiter, brake/throttle overlap, and corners taken slower than your best lap. At the end of each lap there's a debrief covering your time against your best, fuel per lap, and your biggest corner losses.
2. **The LLM decides how to say it.** It gets the facts as JSON, a plain-English description of the event, and its last 8 lines so it doesn't repeat itself. It's told to use only the facts it's given, so it can't invent pit calls or problems. Thinking is turned off (`chat_template_kwargs: {"enable_thinking": false}`), so replies take about 0.2 s.
3. **TTS speaks it.** The mp3 goes to the dashboard over the WebSocket and plays on your phone, with a caption.
4. **It waits for a straight.** It won't talk mid-corner, keeps at least 7 s between lines, and drops a callout if no straight comes within 10 s. Lap debriefs can wait 60 s.

**Corner names:** to help you learn a track, the coach says each corner's name about 2.5 s before you reach it ("Hatzenbach", "Flugplatz", "Aremberg"). Bends that share a name are announced once. Names cut off any other coaching line, and other lines wait until no name is due, so names always come first. The audio for every name is made when the server starts, so names play without delay. Only named turns are announced, so on tracks with plain "Turn 3" names it stays quiet. It's on by default. Tap **CORNERS** on the dashboard to switch it off or on while you drive, or set `GT7_CORNER_NAMES=0` in `.env` to start with it off.

Your personal-best lap for each car and track is saved to `coach_refs.json`. That's what "slower than your best" compares against, so corner comparisons start after you've set a best lap. It's git-ignored, as personal data that stays on your machine.

**Pronunciation:** Kokoro is an English voice and mangles German corner names ("Hocheichen" comes out as "Hot Chicken"). `tracks/pronunciations.csv` gives a phoneme spelling for each Nordschleife corner. Before a line is spoken, those names are swapped for Kokoro's inline form, `[Schwedenkreuz](/ʃvˈAdᵊnkɹˌYts/)`. Captions keep the normal spelling. To add a name, add a row: the name exactly as it appears in a `_turns.csv`, then its phonemes in [Kokoro's phoneme set](https://github.com/hexgrad/misaki/blob/main/EN_PHONES.md). Other TTS servers would read the brackets out loud, so empty that file if you use one.

If `GT7_LLM_URL` is blank or the LLM is down, the coach speaks fixed fallback lines. If `GT7_TTS_URL` is blank or TTS is down, callouts show as captions only.

## Files

| Path | What it is |
|---|---|
| `gt7_telemetry.py` | Packet decryption and parsing, terminal dashboard, `--ps5-ip` option |
| `gt7_web.py` | Web server: UDP listener, WebSocket, track and naming endpoints |
| `static/index.html` | The phone dashboard (one file, no build step) |
| `track.py` | Track detection, lap position, turn lookup, lap recorder |
| `trackbuild.py` | Lap cleaning, resampling and turn detection, shared by the importer and the recorder |
| `coach.py` | Voice coach: event rules, LLM prompt, TTS |
| `env.py` | Loads `.env` |
| `tracks/` | Track lines, turns and metadata, plus `pronunciations.csv` for the voice |
| `tools/import_gt7tracks.py` | Re-imports the GT7Tracks laps |
| `.env.example` | Settings template; copy to `.env` |

## Notes

- **Heartbeat:** `~` is sent to the PS5 on port `33739` every 10 seconds. GT7 stops sending if it doesn't hear one within about 16 seconds. `~` asks for GT7's longest packet format (`0x158` bytes), which adds steering angle, sway/heave/surge, filtered throttle/brake and energy recovery. Change `HEARTBEAT_PACKET` in `gt7_telemetry.py` to `b"A"` if your firmware doesn't respond to it.
- **Screen staying on:** browsers only offer the screen Wake Lock API on `https://` pages or `localhost`, and the dashboard is plain `http://` on your network. So after your first tap the dashboard also plays a tiny silent looping video (`static/keepawake.mp4`, bottom-right corner, invisible), which phones treat as "watching something" and keep the screen on. If you switch apps or lock the phone, tap the dashboard once when you come back.
- **Wheel slip** is tyre surface speed ÷ car speed: about 1.00 when rolling, above 1 for wheelspin, below 1 for lock-up.
- **Car names** are fetched once at startup from [ddm999/gt7info](https://github.com/ddm999/gt7info). If the fetch fails you'll see numeric car codes.
