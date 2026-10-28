#!/usr/bin/env python3
"""
GT7 UDP Telemetry Listener
Decrypts Salsa20-encrypted packets from PS5 and displays a live dashboard.
"""

import argparse
import csv
import io
import socket
import struct
import time
import threading
import os
import sys
import urllib.request
from Crypto.Cipher import Salsa20

import env  # noqa: F401  (loads .env into os.environ)
from track import TrackMatcher

PS5_IP = os.environ.get("GT7_PS5_IP", "").strip()
PS5_PORT = 33739       # we send heartbeat here
LISTEN_PORT = 33740    # PS5 sends telemetry here
HEARTBEAT_INTERVAL = 10  # seconds (safely under 16s threshold)

# The heartbeat byte picks the packet format GT7 sends back:
#   b"A" -> 0x128 bytes (original)
#   b"B" -> 0x13C bytes (+ wheel rotation, sway/heave/surge)
#   b"~" -> 0x158 bytes (+ filtered throttle/brake, energy recovery)
# Each format XORs the IV with its own constant before decrypting.
HEARTBEAT_PACKET = b"~"
IV_XOR = {b"~": 0x55FABB4F, b"B": 0xDEADBEEF, b"A": 0xDEADBEAF}

SALSA_KEY = b"Simulator Interface Packet GT7 ver 0.0"[:32]

CAR_DB_URL = "https://raw.githubusercontent.com/ddm999/gt7info/web-new/_data/db/cars.csv"
COURSE_DB_URL = "https://raw.githubusercontent.com/ddm999/gt7info/web-new/_data/db/course.csv"

def load_car_names() -> dict:
    try:
        with urllib.request.urlopen(CAR_DB_URL, timeout=5) as resp:
            text = resp.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))
        return {int(row["ID"]): row["ShortName"].strip() for row in reader if row["ID"].strip().lstrip("-").isdigit()}
    except Exception:
        return {}

def load_courses() -> list[dict]:
    """GT7's track layouts with official lengths, for naming recorded tracks."""
    try:
        with urllib.request.urlopen(COURSE_DB_URL, timeout=5) as resp:
            text = resp.read().decode("utf-8")
        return [
            {"name": row["Name"].strip(), "length_m": float(row["Length"])}
            for row in csv.DictReader(io.StringIO(text))
            if row["Length"].strip()
        ]
    except Exception:
        return []

PACKET_A_MAGIC = 0x47375330  # 'G7S0'

def decrypt_packet(data: bytes) -> bytes | None:
    iv1 = struct.unpack_from("<I", data, 0x40)[0]
    # Try the format we asked for first; fall back to the others in case the
    # game ignored our heartbeat type (older firmware only knows "A").
    for kind in (HEARTBEAT_PACKET, *(k for k in IV_XOR if k != HEARTBEAT_PACKET)):
        nonce = struct.pack("<II", iv1 ^ IV_XOR[kind], iv1)
        decrypted = Salsa20.new(key=SALSA_KEY, nonce=nonce).encrypt(data)
        if struct.unpack_from("<I", decrypted, 0x00)[0] == PACKET_A_MAGIC:
            return decrypted
    return None

def parse_packet(data: bytes) -> dict | None:
    if len(data) < 0x128:
        return None

    decrypted = decrypt_packet(data)
    if decrypted is None:
        return None

    def f(offset):
        return struct.unpack_from("<f", decrypted, offset)[0]
    def i32(offset):
        return struct.unpack_from("<i", decrypted, offset)[0]
    def u8(offset):
        return decrypted[offset]
    def u16(offset):
        return struct.unpack_from("<H", decrypted, offset)[0]
    def i16(offset):
        return struct.unpack_from("<h", decrypted, offset)[0]

    pos_x, pos_y, pos_z = f(0x04), f(0x08), f(0x0C)
    vel_x, vel_y, vel_z = f(0x10), f(0x14), f(0x18)
    rotation  = (f(0x1C), f(0x20), f(0x24))   # pitch, yaw, roll
    ang_vel   = (f(0x2C), f(0x30), f(0x34))
    speed_ms = f(0x4C)
    rpm       = f(0x3C)
    boost     = f(0x50) - 1.0   # offset by +1 in packet
    fuel      = f(0x44)
    fuel_cap  = f(0x48)
    oil_temp  = f(0x5C)
    water_temp= f(0x58)
    body_h    = f(0x38)

    tyre_temp = [f(0x60 + i*4) for i in range(4)]   # FL FR RL RR
    wheel_rps = [f(0xA4 + i*4) for i in range(4)]   # FL FR RL RR (actually rad/s)
    tyre_rad  = [f(0xB4 + i*4) for i in range(4)]   # FL FR RL RR, metres
    susp_h    = [f(0xC4 + i*4) for i in range(4)]   # FL FR RL RR

    # Wheel surface speed / car speed: ~1.0 rolling, >1 wheelspin, <1 lockup.
    # Meaningless at a crawl, so only computed above ~10 km/h.
    if speed_ms > 3:
        wheel_slip = [abs(wheel_rps[i]) * tyre_rad[i] / speed_ms for i in range(4)]
    else:
        wheel_slip = None

    gear      = u8(0x90) & 0x0F
    sugg_gear = (u8(0x90) >> 4) & 0x0F
    throttle  = u8(0x91)
    brake     = u8(0x92)

    # flags at 0x8E (uint16)
    flags     = u16(0x8E)

    best_lap_ms = i32(0x78)
    last_lap_ms = i32(0x7C)
    lap_count   = i16(0x74)
    total_laps  = i16(0x76)
    car_code    = i32(0x124)
    shift_rpm   = u16(0x88)   # rpm where the shift lights start
    max_rpm     = u16(0x8A)   # rpm_rev_limiter as ushort

    t = {
        "packet_id": i32(0x70),
        "speed_kmh": speed_ms * 3.6,
        "rpm": rpm,
        "shift_rpm": shift_rpm,
        "max_rpm": max_rpm,
        "gear": gear,
        "sugg_gear": sugg_gear,
        "throttle_pct": throttle / 2.55,
        "brake_pct": brake / 2.55,
        "clutch": f(0xF4),
        "boost_bar": boost,
        "fuel_l": fuel,
        "fuel_cap_l": fuel_cap,
        "oil_temp_c": oil_temp,
        "water_temp_c": water_temp,
        "tyre_temp": tyre_temp,
        "susp_height": susp_h,
        "wheel_rps": wheel_rps,
        "tyre_radius": tyre_rad,
        "wheel_slip": wheel_slip,
        "pos": (pos_x, pos_y, pos_z),
        "vel": (vel_x, vel_y, vel_z),
        "rotation": rotation,
        "ang_vel": ang_vel,
        "body_height": body_h,
        "on_track": bool(flags & 0x0001),
        "paused": bool(flags & 0x0002),
        "loading": bool(flags & 0x0004),
        "rev_limiter": bool(flags & 0x0020),
        "handbrake": bool(flags & 0x0040),
        "asm_active": bool(flags & 0x0400),
        "tcs_active": bool(flags & 0x0800),
        "lap_count": lap_count,
        "best_lap_ms": best_lap_ms,
        "last_lap_ms": last_lap_ms,
        "total_laps": total_laps,
        # Grid / qualifying position; only valid before the race starts (-1 after)
        "grid_pos": i16(0x84),
        "num_cars": i16(0x86),
        "car_code": car_code,
    }

    if len(decrypted) >= 0x13C:   # packet "B" and up
        t["steering_rad"] = f(0x128)
        t["sway"], t["heave"], t["surge"] = f(0x130), f(0x134), f(0x138)
    if len(decrypted) >= 0x158:   # packet "~"
        t["throttle_filtered_pct"] = u8(0x13C) / 2.55
        t["brake_filtered_pct"] = u8(0x13D) / 2.55
        t["energy_recovery"] = f(0x150)

    return t


class PacketStats:
    """Counts packets and spots gaps in the packet ID sequence."""

    def __init__(self) -> None:
        self.received = 0
        self.dropped = 0
        self._last_id: int | None = None

    def update(self, packet_id: int) -> None:
        self.received += 1
        if self._last_id is not None:
            gap = packet_id - self._last_id - 1
            if 0 < gap < 1000:   # bigger jumps are a session restart, not loss
                self.dropped += gap
        self._last_id = packet_id


def add_track_info(t: dict, matcher: TrackMatcher) -> None:
    """Attach lap position / turn fields from the matcher (None when unknown)."""
    t["track_pos"] = matcher.update(t)


def fmt_time(ms: int) -> str:
    if ms < 0:
        return "--:--.---"
    m, rem = divmod(ms, 60000)
    s, ms_ = divmod(rem, 1000)
    return f"{m}:{s:02d}.{ms_:03d}"

def fmt_gear(g: int) -> str:
    if g == 0: return "R"
    if g == 15: return "N"
    return str(g)

def fmt_track(tp: dict | None) -> str:
    if tp is None:
        return "unknown"
    here = tp["turn"]["label"] if tp["turn"] else "straight"
    out = f"{tp['track']}  {tp['lap_dist_m']/1000:5.2f} km  {here}"
    nxt = tp["next_turn"]
    if nxt:
        out += f"  (next {nxt['label']} {nxt['dir']} in {tp['next_turn_in_m']:.0f} m)"
    return out

def render(t: dict, stats: PacketStats, car_names: dict):
    HOME  = "\033[H"
    EOL   = "\033[K"   # clear to end of line
    EOS   = "\033[J"   # clear to end of screen
    BOLD  = "\033[1m"
    RST   = "\033[0m"
    RED   = "\033[91m"
    YEL   = "\033[93m"
    GRN   = "\033[92m"
    CYN   = "\033[96m"
    MAG   = "\033[95m"

    def rpm_bar(rpm, shift_rpm, max_rpm, width=30):
        ratio = min(rpm / max_rpm, 1.0) if max_rpm > 0 else 0
        filled = int(ratio * width)
        if rpm >= max_rpm > 0: color = RED
        elif rpm >= shift_rpm > 0: color = YEL
        else: color = GRN
        return color + "█" * filled + "░" * (width - filled) + RST

    def pct_bar(pct, width=20, color=GRN):
        filled = int(min(pct, 100) / 100 * width)
        return color + "█" * filled + "░" * (width - filled) + RST

    def temp_color(t_val, warn=80, hot=100):
        if t_val >= hot:  return RED
        if t_val >= warn: return YEL
        return GRN

    def slip_cell(s):
        if s is None: return f"{'—':>8}"
        color = RED if abs(s - 1) > 0.10 else (YEL if abs(s - 1) > 0.04 else GRN)
        return f"{color}{s:8.2f}{RST}"

    tyre = t["tyre_temp"]
    susp = t["susp_height"]
    wrps = t["wheel_rps"]
    slip = t["wheel_slip"] or [None] * 4

    status = ""
    if t["loading"]:  status = f"{YEL}[LOADING]{RST}"
    elif t["paused"]: status = f"{YEL}[PAUSED]{RST}"
    elif t["on_track"]:status = f"{GRN}[ON TRACK]{RST}"
    else:             status = f"{CYN}[MENU/REPLAY]{RST}"

    aids = " ".join(name for name, on in (("TCS", t["tcs_active"]), ("ASM", t["asm_active"])) if on)

    output = f"""{BOLD}{'═'*60}
  GT7 Telemetry                                  {status}
{'═'*60}{RST}

  {BOLD}SPEED{RST}  {CYN}{t['speed_kmh']:6.1f} km/h{RST}    {BOLD}GEAR{RST}  {MAG}{fmt_gear(t['gear'])}{RST}  (suggest {fmt_gear(t['sugg_gear'])})

  {BOLD}RPM{RST}   {t['rpm']:7.0f} / {t['max_rpm']:.0f}   (shift {t['shift_rpm']})
         {rpm_bar(t['rpm'], t['shift_rpm'], t['max_rpm'])}

  {BOLD}THROTTLE{RST}  {pct_bar(t['throttle_pct'], color=GRN)}  {t['throttle_pct']:5.1f}%
  {BOLD}BRAKE   {RST}  {pct_bar(t['brake_pct'],    color=RED)}  {t['brake_pct']:5.1f}%   {YEL}{aids}{RST}

  {BOLD}FUEL{RST}  {t['fuel_l']:.1f} L / {t['fuel_cap_l']:.1f} L    {BOLD}BOOST{RST}  {t['boost_bar']:+.2f} bar

  {BOLD}TEMPS{RST}  Water {temp_color(t['water_temp_c'])}{t['water_temp_c']:.1f}°C{RST}   Oil {temp_color(t['oil_temp_c'])}{t['oil_temp_c']:.1f}°C{RST}

  {BOLD}TYRES (°C)        FL          FR          RL          RR{RST}
    Temp   {temp_color(tyre[0])}{tyre[0]:8.1f}{RST}    {temp_color(tyre[1])}{tyre[1]:8.1f}{RST}    {temp_color(tyre[2])}{tyre[2]:8.1f}{RST}    {temp_color(tyre[3])}{tyre[3]:8.1f}{RST}
    Susp   {susp[0]:8.3f}    {susp[1]:8.3f}    {susp[2]:8.3f}    {susp[3]:8.3f} m
    RPS    {wrps[0]:8.2f}    {wrps[1]:8.2f}    {wrps[2]:8.2f}    {wrps[3]:8.2f}
    Slip   {slip_cell(slip[0])}    {slip_cell(slip[1])}    {slip_cell(slip[2])}    {slip_cell(slip[3])}

  {BOLD}RACE{RST}   Lap {t['lap_count']}/{t['total_laps']}
  {BOLD}TIMES{RST}
         Best {GRN}{fmt_time(t['best_lap_ms'])}{RST}
         Last {fmt_time(t['last_lap_ms'])}

  {BOLD}TRACK{RST} {fmt_track(t['track_pos'])}
  {BOLD}POS{RST}  X:{t['pos'][0]:9.1f}  Y:{t['pos'][1]:8.1f}  Z:{t['pos'][2]:9.1f}
  {BOLD}VEL{RST}  X:{t['vel'][0]:9.2f}  Y:{t['vel'][1]:8.2f}  Z:{t['vel'][2]:9.2f}

  {BOLD}CAR{RST}  {car_names.get(t['car_code'], f"Unknown ({t['car_code']})")}    {BOLD}Packets{RST} {stats.received} ({stats.dropped} dropped)
{'─'*60}
  Ctrl+C to quit"""
    # Overwrite in place instead of clearing the screen every frame (flicker).
    sys.stdout.write(HOME + output.replace("\n", EOL + "\n") + EOL + "\n" + EOS)
    sys.stdout.flush()

def heartbeat_loop(sock: socket.socket, ps5_ip: str):
    while True:
        try:
            sock.sendto(HEARTBEAT_PACKET, (ps5_ip, PS5_PORT))
        except Exception:
            pass
        time.sleep(HEARTBEAT_INTERVAL)

def parse_args(description: str, web: bool = False) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--ps5-ip", default=PS5_IP,
        help="PS5 LAN IP (default: GT7_PS5_IP from .env)",
    )
    if web:
        parser.add_argument(
            "--no-coach", action="store_true",
            help="disable the LLM + TTS voice coach",
        )
    args = parser.parse_args()
    if not args.ps5_ip:
        parser.error("no PS5 IP: set GT7_PS5_IP in .env or pass --ps5-ip")
    return args

def main():
    args = parse_args("GT7 telemetry terminal dashboard")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", LISTEN_PORT))
    sock.settimeout(1.0)

    print("Fetching car database...")
    car_names = load_car_names()
    print(f"Loaded {len(car_names)} cars." if car_names else "Car database unavailable, will show codes.")
    print(f"Listening on UDP port {LISTEN_PORT} ...")
    print(f"Sending heartbeat to {args.ps5_ip}:{PS5_PORT} every {HEARTBEAT_INTERVAL}s")
    print("Waiting for GT7 packets (make sure GT7 is running on the PS5)...\n")

    hb = threading.Thread(target=heartbeat_loop, args=(sock, args.ps5_ip), daemon=True)
    hb.start()

    stats = PacketStats()
    matcher = TrackMatcher()
    last_render = 0
    cleared = False

    try:
        while True:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue

            t = parse_packet(data)
            if t is None:
                continue
            stats.update(t["packet_id"])
            add_track_info(t, matcher)

            now = time.monotonic()
            if now - last_render >= 0.1:   # render at ~10 Hz
                if not cleared:
                    sys.stdout.write("\033[2J")
                    cleared = True
                render(t, stats, car_names)
                last_render = now

    except KeyboardInterrupt:
        print("\n\nStopped.")
    finally:
        sock.close()

if __name__ == "__main__":
    main()
