#!/usr/bin/env python3
"""
GT7 UDP Telemetry Listener
Decrypts Salsa20-encrypted packets from PS5 and displays a live dashboard.
"""

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

PS5_IP = "192.168.64.4"
PS5_PORT = 33739       # we send heartbeat here
LISTEN_PORT = 33740    # PS5 sends telemetry here
HEARTBEAT_INTERVAL = 10  # seconds (safely under 16s threshold)

SALSA_KEY = b"Simulator Interface Packet GT7 ver 0.0"[:32]

CAR_DB_URL = "https://raw.githubusercontent.com/ddm999/gt7info/web-new/_data/db/cars.csv"

def load_car_names() -> dict:
    try:
        with urllib.request.urlopen(CAR_DB_URL, timeout=5) as resp:
            text = resp.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))
        return {int(row["ID"]): row["ShortName"].strip() for row in reader if row["ID"].strip().lstrip("-").isdigit()}
    except Exception:
        return {}

PACKET_A_MAGIC = 0x47375330  # 'G7S0'

def decrypt_packet(data: bytes) -> bytes:
    iv1 = struct.unpack_from("<I", data, 0x40)[0]
    iv2 = iv1 ^ 0xDEADBEAF
    nonce = struct.pack("<II", iv2, iv1)
    cipher = Salsa20.new(key=SALSA_KEY, nonce=nonce)
    return cipher.encrypt(data)

def parse_packet(data: bytes) -> dict | None:
    if len(data) < 0x100:
        return None

    decrypted = decrypt_packet(data)

    magic = struct.unpack_from("<I", decrypted, 0x00)[0]
    if magic != PACKET_A_MAGIC:
        return None

    def f(offset):
        return struct.unpack_from("<f", decrypted, offset)[0]
    def i32(offset):
        return struct.unpack_from("<i", decrypted, offset)[0]
    def u8(offset):
        return decrypted[offset]

    pos_x, pos_y, pos_z = f(0x04), f(0x08), f(0x0C)
    vel_x, vel_y, vel_z = f(0x10), f(0x14), f(0x18)
    speed_ms = f(0x4C)
    rpm       = f(0x3C)
    boost     = f(0x50) - 1.0   # offset by +1 in packet
    fuel      = f(0x44)
    fuel_cap  = f(0x48)
    oil_temp  = f(0x5C)
    water_temp= f(0x58)
    body_h    = f(0x38)

    tyre_temp = [f(0x60 + i*4) for i in range(4)]   # FL FR RL RR
    susp_h    = [f(0xC4 + i*4) for i in range(4)]   # FL FR RL RR
    wheel_rps = [f(0xA4 + i*4) for i in range(4)]   # FL FR RL RR

    gear      = u8(0x90) & 0x0F
    sugg_gear = (u8(0x90) >> 4) & 0x0F
    throttle  = u8(0x91)
    brake     = u8(0x92)

    def u16(offset):
        return struct.unpack_from("<H", decrypted, offset)[0]
    def i16(offset):
        return struct.unpack_from("<h", decrypted, offset)[0]

    # flags at 0x8E (uint16)
    flags     = u16(0x8E)
    in_race   = bool(flags & 0x0001)
    paused    = bool(flags & 0x0002)
    loading   = bool(flags & 0x0004)

    best_lap_ms = i32(0x78)
    last_lap_ms = i32(0x7C)
    lap_count   = i16(0x74)
    total_laps  = i16(0x76)
    car_code    = i32(0x124)
    max_rpm     = u16(0x8A)   # rpm_rev_limiter as ushort

    return {
        "speed_kmh": speed_ms * 3.6,
        "rpm": rpm,
        "max_rpm": max_rpm,
        "gear": gear,
        "sugg_gear": sugg_gear,
        "throttle_pct": throttle / 2.55,
        "brake_pct": brake / 2.55,
        "boost_bar": boost,
        "fuel_l": fuel,
        "fuel_cap_l": fuel_cap,
        "oil_temp_c": oil_temp,
        "water_temp_c": water_temp,
        "tyre_temp": tyre_temp,
        "susp_height": susp_h,
        "wheel_rps": wheel_rps,
        "pos": (pos_x, pos_y, pos_z),
        "vel": (vel_x, vel_y, vel_z),
        "body_height": body_h,
        "in_race": in_race,
        "paused": paused,
        "loading": loading,
        "lap_count": lap_count,
        "best_lap_ms": best_lap_ms,
        "last_lap_ms": last_lap_ms,
        "total_laps": total_laps,
        "car_code": car_code,
    }

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

def render(t: dict, pkt_count: int, car_names: dict):
    CLEAR = "\033[2J\033[H"
    BOLD  = "\033[1m"
    RST   = "\033[0m"
    RED   = "\033[91m"
    YEL   = "\033[93m"
    GRN   = "\033[92m"
    CYN   = "\033[96m"
    MAG   = "\033[95m"

    def rpm_bar(rpm, max_rpm, width=30):
        ratio = min(rpm / max_rpm, 1.0) if max_rpm > 0 else 0
        filled = int(ratio * width)
        color = GRN if ratio < 0.7 else (YEL if ratio < 0.9 else RED)
        return color + "█" * filled + "░" * (width - filled) + RST

    def pct_bar(pct, width=20, color=GRN):
        filled = int(min(pct, 100) / 100 * width)
        return color + "█" * filled + "░" * (width - filled) + RST

    def temp_color(t_val, warn=80, hot=100):
        if t_val >= hot:  return RED
        if t_val >= warn: return YEL
        return GRN

    tyre = t["tyre_temp"]
    susp = t["susp_height"]
    wrps = t["wheel_rps"]

    status = ""
    if t["loading"]:  status = f"{YEL}[LOADING]{RST}"
    elif t["paused"]: status = f"{YEL}[PAUSED]{RST}"
    elif t["in_race"]:status = f"{GRN}[RACING]{RST}"
    else:             status = f"{CYN}[MENU/REPLAY]{RST}"

    output = f"""{CLEAR}{BOLD}{'═'*60}
  GT7 Telemetry                                  {status}
{'═'*60}{RST}

  {BOLD}SPEED{RST}  {CYN}{t['speed_kmh']:6.1f} km/h{RST}    {BOLD}GEAR{RST}  {MAG}{fmt_gear(t['gear'])}{RST}  (suggest {fmt_gear(t['sugg_gear'])})

  {BOLD}RPM{RST}   {t['rpm']:7.0f} / {t['max_rpm']:.0f}
         {rpm_bar(t['rpm'], t['max_rpm'])}

  {BOLD}THROTTLE{RST}  {pct_bar(t['throttle_pct'], color=GRN)}  {t['throttle_pct']:5.1f}%
  {BOLD}BRAKE   {RST}  {pct_bar(t['brake_pct'],    color=RED)}  {t['brake_pct']:5.1f}%

  {BOLD}FUEL{RST}  {t['fuel_l']:.1f} L / {t['fuel_cap_l']:.1f} L    {BOLD}BOOST{RST}  {t['boost_bar']:+.2f} bar

  {BOLD}TEMPS{RST}  Water {temp_color(t['water_temp_c'])}{t['water_temp_c']:.1f}°C{RST}   Oil {temp_color(t['oil_temp_c'])}{t['oil_temp_c']:.1f}°C{RST}

  {BOLD}TYRES (°C)        FL          FR          RL          RR{RST}
    Temp   {temp_color(tyre[0])}{tyre[0]:8.1f}{RST}    {temp_color(tyre[1])}{tyre[1]:8.1f}{RST}    {temp_color(tyre[2])}{tyre[2]:8.1f}{RST}    {temp_color(tyre[3])}{tyre[3]:8.1f}{RST}
    Susp   {susp[0]:8.3f}    {susp[1]:8.3f}    {susp[2]:8.3f}    {susp[3]:8.3f} m
    RPS    {wrps[0]:8.2f}    {wrps[1]:8.2f}    {wrps[2]:8.2f}    {wrps[3]:8.2f}

  {BOLD}RACE{RST}   Lap {t['lap_count']}/{t['total_laps']}
  {BOLD}TIMES{RST}
         Best {GRN}{fmt_time(t['best_lap_ms'])}{RST}
         Last {fmt_time(t['last_lap_ms'])}

  {BOLD}POS{RST}  X:{t['pos'][0]:9.1f}  Y:{t['pos'][1]:8.1f}  Z:{t['pos'][2]:9.1f}
  {BOLD}VEL{RST}  X:{t['vel'][0]:9.2f}  Y:{t['vel'][1]:8.2f}  Z:{t['vel'][2]:9.2f}

  {BOLD}CAR{RST}  {car_names.get(t['car_code'], f"Unknown ({t['car_code']})")}    {BOLD}Packets{RST} {pkt_count}
{'─'*60}
  Ctrl+C to quit
"""
    sys.stdout.write(output)
    sys.stdout.flush()

def heartbeat_loop(sock: socket.socket):
    while True:
        try:
            sock.sendto(b"A", (PS5_IP, PS5_PORT))
        except Exception:
            pass
        time.sleep(HEARTBEAT_INTERVAL)

def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", LISTEN_PORT))
    sock.settimeout(1.0)

    print("Fetching car database...")
    car_names = load_car_names()
    print(f"Loaded {len(car_names)} cars." if car_names else "Car database unavailable, will show codes.")
    print(f"Listening on UDP port {LISTEN_PORT} ...")
    print(f"Sending heartbeat to {PS5_IP}:{PS5_PORT} every {HEARTBEAT_INTERVAL}s")
    print("Waiting for GT7 packets (make sure GT7 is running on the PS5)...\n")

    hb = threading.Thread(target=heartbeat_loop, args=(sock,), daemon=True)
    hb.start()

    pkt_count = 0
    last_render = 0

    try:
        while True:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue

            pkt_count += 1
            t = parse_packet(data)
            if t is None:
                continue

            now = time.monotonic()
            if now - last_render >= 0.1:   # render at ~10 Hz to avoid flicker
                render(t, pkt_count, car_names)
                last_render = now

    except KeyboardInterrupt:
        print("\n\nStopped.")
    finally:
        sock.close()

if __name__ == "__main__":
    main()
