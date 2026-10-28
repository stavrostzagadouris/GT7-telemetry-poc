#!/usr/bin/env python3
"""
GT7 Telemetry Web Dashboard.
One process: UDP listener + heartbeat + WebSocket broadcast + mobile UI.
Run:   python gt7_web.py [--ps5-ip 192.168.x.x] [--no-coach]
Open:  http://<this-host>:7000  (on your phone, same Wi-Fi)
"""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
import uvicorn

from gt7_telemetry import (
    PS5_IP,
    PS5_PORT,
    LISTEN_PORT,
    HEARTBEAT_INTERVAL,
    HEARTBEAT_PACKET,
    PacketStats,
    add_track_info,
    load_car_names,
    parse_args,
    parse_packet,
)
from coach import LLM_URL, TTS_URL, Coach
from track import TrackMatcher

HOST = "0.0.0.0"
WEB_PORT = 7000

STATIC_DIR = Path(__file__).parent / "static"


class Client:
    """One WebSocket viewer. Holds only the newest telemetry frame, so a slow
    phone skips frames instead of building up a backlog. Messages queued with
    send() (coach callouts) are always delivered, ahead of telemetry."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.latest: str | None = None
        self.outbox: list[str] = []
        self.ready = asyncio.Event()

    def push(self, payload: str) -> None:
        self.latest = payload
        self.ready.set()

    def send(self, payload: str) -> None:
        self.outbox.append(payload)
        self.ready.set()

    async def send_loop(self) -> None:
        while True:
            await self.ready.wait()
            self.ready.clear()
            pending, self.outbox = self.outbox, []
            if self.latest is not None:
                pending.append(self.latest)
                self.latest = None
            try:
                for payload in pending:
                    await self.ws.send_text(payload)
            except Exception:
                return   # socket is gone; ws_endpoint cleans up


class AppState:
    def __init__(self) -> None:
        self.ps5_ip: str = PS5_IP
        self.car_names: dict[int, str] = {}
        self.stats = PacketStats()
        self.matcher = TrackMatcher()
        self.clients: set[Client] = set()
        self.coach_enabled = True
        self.coach: Coach | None = None


S = AppState()


class UDPProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data: bytes, addr) -> None:
        t = parse_packet(data)
        if t is None:
            return
        S.stats.update(t["packet_id"])
        add_track_info(t, S.matcher)
        t["car_name"] = S.car_names.get(t["car_code"], f"Unknown ({t['car_code']})")
        if S.coach:
            S.coach.feed(t)
        if not S.clients:
            return
        t["pkt_count"] = S.stats.received
        t["pkt_dropped"] = S.stats.dropped
        payload = json.dumps(t)
        for c in S.clients:
            c.push(payload)


def send_to_all(payload: str) -> None:
    for c in S.clients:
        c.send(payload)


async def heartbeat_loop(transport: asyncio.DatagramTransport) -> None:
    while True:
        try:
            transport.sendto(HEARTBEAT_PACKET, (S.ps5_ip, PS5_PORT))
        except Exception:
            pass
        await asyncio.sleep(HEARTBEAT_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Fetching car database...")
    S.car_names = load_car_names()
    print(
        f"Loaded {len(S.car_names)} cars."
        if S.car_names
        else "Car database unavailable, will show codes."
    )

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        UDPProtocol, local_addr=("0.0.0.0", LISTEN_PORT)
    )
    hb = asyncio.create_task(heartbeat_loop(transport))
    coach_task = None
    if S.coach_enabled:
        S.coach = Coach(send_to_all)
        coach_task = asyncio.create_task(S.coach.run())
        print(
            "Coach on (disable with --no-coach): "
            f"LLM {LLM_URL or 'not set, using fixed lines'}, "
            f"TTS {TTS_URL or 'not set, captions only'}"
        )
    print(
        f"Listening on UDP :{LISTEN_PORT}, "
        f"heartbeat -> {S.ps5_ip}:{PS5_PORT} every {HEARTBEAT_INTERVAL}s"
    )
    print(f"Web dashboard: http://{HOST}:{WEB_PORT}")

    try:
        yield
    finally:
        hb.cancel()
        if coach_task:
            coach_task.cancel()
            await S.coach.http.aclose()
        transport.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/tracks")
async def tracks():
    """Centerlines for the track map, as [x, z] pairs."""
    return {
        t.name: {"length_m": t.length, "points": t.map_points()}
        for t in S.matcher.tracks
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    client = Client(ws)
    S.clients.add(client)
    sender = asyncio.create_task(client.send_loop())
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        S.clients.discard(client)
        sender.cancel()


if __name__ == "__main__":
    args = parse_args("GT7 telemetry web dashboard", web=True)
    S.ps5_ip = args.ps5_ip
    S.coach_enabled = not args.no_coach
    uvicorn.run(app, host=HOST, port=WEB_PORT, log_level="info")
