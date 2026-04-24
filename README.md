# GT7 Telemetry

Decrypts Gran Turismo 7's Salsa20-encrypted UDP telemetry from a PS5 and shows it live. Two front-ends, same parser:

- **`gt7_telemetry.py`** — ANSI terminal dashboard.
- **`gt7_web.py`** — FastAPI + WebSocket backend with a mobile-friendly browser dashboard, bound to `0.0.0.0:7000`.

## Requirements

- Python 3.10+
- A PS5 on the same network, with GT7 running.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Configure your PS5 IP

Edit the top of `gt7_telemetry.py`:

```python
PS5_IP = "192.168.64.4"   # <- change to your PS5's LAN IP
```

`gt7_web.py` imports this constant, so you only set it in one place.

## Run

**Terminal dashboard:**

```sh
.venv/bin/python gt7_telemetry.py
```

**Web dashboard (recommended for mobile):**

```sh
.venv/bin/python gt7_web.py
```

Then from any device on the same Wi-Fi: `http://<your-host-ip>:7000`

## Notes

- Both scripts bind UDP `:33740`, so run **one at a time**, not both.
- Heartbeat is sent to the PS5 at port `33739` every 10 seconds — GT7 stops sending telemetry if it doesn't hear one within ~16 seconds. If the dashboard shows `NO DATA`, confirm GT7 is running and the IP is correct.
- Car names are fetched once at startup from [ddm999/gt7info](https://github.com/ddm999/gt7info). If the fetch fails you'll see numeric car codes.
