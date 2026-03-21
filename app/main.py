"""
Network Buddy — FastAPI backend.

Endpoints:
  GET  /api/devices        — current device list with bandwidth
  GET  /api/stats          — interface-level totals
  POST /api/scan           — trigger an immediate re-scan
  WS   /ws                 — real-time push every 2 s
  GET  /                   — serves the dashboard SPA
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .scanner import scan_network
from .bandwidth import BandwidthMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

SCAN_INTERVAL = 30   # seconds between automatic re-scans
PUSH_INTERVAL = 2    # seconds between WebSocket pushes

app = FastAPI(title="Network Buddy", version="1.0.0")

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_devices: List[dict] = []
_last_scan: float = 0.0
_scan_lock = asyncio.Lock()
_bw = BandwidthMonitor()
_clients: List[WebSocket] = []


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup():
    _bw.start()
    asyncio.create_task(_periodic_scan())
    asyncio.create_task(_push_loop())


@app.on_event("shutdown")
async def on_shutdown():
    _bw.stop()


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _do_scan():
    global _devices, _last_scan
    async with _scan_lock:
        logger.info("Starting network scan…")
        loop = asyncio.get_event_loop()
        _devices = await loop.run_in_executor(None, scan_network)
        _last_scan = time.time()
        logger.info("Scan complete — %d device(s) found", len(_devices))


async def _periodic_scan():
    while True:
        await _do_scan()
        await asyncio.sleep(SCAN_INTERVAL)


async def _push_loop():
    while True:
        await asyncio.sleep(PUSH_INTERVAL)
        if not _clients:
            continue
        payload = json.dumps(_build_payload())
        dead: List[WebSocket] = []
        for ws in list(_clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            _clients.remove(ws)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_payload() -> dict:
    device_rates = _bw.get_device_rates()
    total = _bw.get_total_rates()

    enriched = []
    for dev in _devices:
        d = dict(dev)
        ip = d.get("ip", "")
        rates = device_rates.get(ip, {"upload_bps": 0.0, "download_bps": 0.0})
        d["upload_bps"] = rates["upload_bps"]
        d["download_bps"] = rates["download_bps"]
        enriched.append(d)

    return {
        "type": "update",
        "devices": enriched,
        "last_scan": _last_scan,
        "total_upload_bps": total["upload_bps"],
        "total_download_bps": total["download_bps"],
    }


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/devices")
async def get_devices():
    return _build_payload()


@app.get("/api/stats")
async def get_stats():
    return {
        "interface_rates": _bw.get_interface_rates(),
        "total": _bw.get_total_rates(),
        "last_scan": _last_scan,
        "device_count": len(_devices),
    }


@app.post("/api/scan")
async def trigger_scan():
    await _do_scan()
    return {"ok": True, "device_count": len(_devices)}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.append(ws)
    logger.info("WebSocket client connected (total: %d)", len(_clients))
    try:
        # Send current state immediately on connect
        await ws.send_text(json.dumps(_build_payload()))
        while True:
            # Keep the connection alive; pushes are handled by _push_loop
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if ws in _clients:
            _clients.remove(ws)
        logger.info("WebSocket client disconnected (total: %d)", len(_clients))


# ---------------------------------------------------------------------------
# Static files (must be last — catches everything else)
# ---------------------------------------------------------------------------

_static_dir = Path(__file__).parent.parent / "static"
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
