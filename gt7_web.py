#!/usr/bin/env python3
"""
GT7 Telemetry Web Dashboard.
One process: UDP listener + heartbeat + WebSocket broadcast + mobile UI.
Run:   python gt7_web.py
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
    load_car_names,
    parse_packet,
)

HOST = "0.0.0.0"
WEB_PORT = 7000

STATIC_DIR = Path(__file__).parent / "static"


class AppState:
    def __init__(self) -> None:
        self.car_names: dict[int, str] = {}
        self.pkt_count: int = 0
        self.clients: set[WebSocket] = set()


S = AppState()


async def broadcast(payload: str) -> None:
    if not S.clients:
        return
    dead = []
    for ws in list(S.clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        S.clients.discard(ws)


class UDPProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data: bytes, addr) -> None:
        S.pkt_count += 1
        t = parse_packet(data)
        if t is None:
            return
        t["car_name"] = S.car_names.get(t["car_code"], f"Unknown ({t['car_code']})")
        t["pkt_count"] = S.pkt_count
        asyncio.create_task(broadcast(json.dumps(t)))


async def heartbeat_loop(transport: asyncio.DatagramTransport) -> None:
    while True:
        try:
            transport.sendto(b"A", (PS5_IP, PS5_PORT))
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
    print(
        f"Listening on UDP :{LISTEN_PORT}, "
        f"heartbeat -> {PS5_IP}:{PS5_PORT} every {HEARTBEAT_INTERVAL}s"
    )
    print(f"Web dashboard: http://{HOST}:{WEB_PORT}")

    try:
        yield
    finally:
        hb.cancel()
        transport.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    S.clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        S.clients.discard(ws)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=WEB_PORT, log_level="info")
