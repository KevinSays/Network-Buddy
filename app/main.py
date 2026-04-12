"""
MHS: Mikrotik Homelab Scanner — FastAPI backend.

Data source priority:
  1. MikroTik RouterOS REST API  (if reachable — authoritative)
  2. Local ARP / nmap scan + psutil bandwidth  (fallback)

Endpoints:
  GET  /api/devices         — device list with per-device bandwidth
  GET  /api/device/{query}  — look up a single device by IP or MAC address
  GET  /api/ports           — router + switch port stats
  GET  /api/stats           — interface totals + meta
  POST /api/scan            — trigger an immediate re-scan / refresh
  WS   /ws                  — real-time push every 2 s
  GET  /                    — dashboard SPA
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .scanner import scan_network
from .bandwidth import BandwidthMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

SCAN_INTERVAL = 30   # seconds between local re-scans (fallback mode)
PUSH_INTERVAL = 2    # seconds between WebSocket pushes

app = FastAPI(title="MHS: Mikrotik Homelab Scanner", version="1.1.0")

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_devices: List[dict] = []        # used only when MikroTik is unavailable
_last_scan: float = 0.0
_scan_lock = asyncio.Lock()

_bw = BandwidthMonitor()          # always running (fallback + local iface totals)

_mikrotik = None                  # MikroTikMonitor instance, or None
_clients: List[WebSocket] = []

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup():
    global _mikrotik

    # Always start local bandwidth monitor (used as fallback)
    _bw.start()

    # Attempt to connect to MikroTik
    try:
        import sys, os
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from settings import settings
        from .mikrotik import MikroTikMonitor

        monitor = MikroTikMonitor(settings.router, settings.switch)
        reachable = await monitor.router.ping()

        if reachable:
            await monitor.start()
            _mikrotik = monitor
            logger.info(
                "MikroTik connected at %s — using RouterOS API as primary source",
                settings.router.host,
            )
        else:
            await monitor.close()
            logger.warning(
                "MikroTik at %s is unreachable — falling back to local scan",
                settings.router.host,
            )
    except Exception as exc:
        logger.warning("MikroTik init failed (%s) — using local scan", exc)

    asyncio.create_task(_local_scan_loop())
    asyncio.create_task(_push_loop())


@app.on_event("shutdown")
async def on_shutdown():
    _bw.stop()
    if _mikrotik:
        await _mikrotik.close()


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _local_scan_loop():
    """Runs the local ARP/nmap scan.  In MikroTik mode, runs once at startup
    then sits idle (MikroTik provides device data instead)."""
    while True:
        if _mikrotik is None:
            await _do_local_scan()
        await asyncio.sleep(SCAN_INTERVAL)


async def _do_local_scan():
    global _devices, _last_scan
    async with _scan_lock:
        logger.info("Starting local network scan…")
        loop = asyncio.get_event_loop()
        _devices = await loop.run_in_executor(None, scan_network)
        _last_scan = time.time()
        logger.info("Local scan complete — %d device(s)", len(_devices))


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
            if ws in _clients:
                _clients.remove(ws)


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------

def _build_payload() -> dict:
    if _mikrotik:
        devices = _mikrotik.get_devices()
        wan = _mikrotik.get_wan_stats()
        ports = {
            "router": _mikrotik.get_router_ports(),
            "switch": _mikrotik.get_switch_ports(),
        }
        total_dl = wan["download_bps"]
        total_ul = wan["upload_bps"]
        source = "mikrotik"
    else:
        device_rates = _bw.get_device_rates()
        total = _bw.get_total_rates()
        devices = [
            {
                **dev,
                "upload_bps":   device_rates.get(dev["ip"], {}).get("upload_bps",   0.0),
                "download_bps": device_rates.get(dev["ip"], {}).get("download_bps", 0.0),
            }
            for dev in _devices
        ]
        ports = {"router": [], "switch": []}
        total_dl = total["download_bps"]
        total_ul = total["upload_bps"]
        source = "local"

    limits = _mikrotik.get_device_limits() if _mikrotik else {}

    return {
        "type":               "update",
        "source":             source,
        "devices":            devices,
        "ports":              ports,
        "limits":             limits,
        "last_scan":          _last_scan,
        "total_download_bps": total_dl,
        "total_upload_bps":   total_ul,
    }


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/devices")
async def get_devices():
    return _build_payload()


@app.get("/api/ports")
async def get_ports():
    if _mikrotik:
        return {
            "router": _mikrotik.get_router_ports(),
            "switch": _mikrotik.get_switch_ports(),
        }
    return {"router": [], "switch": [], "note": "MikroTik not connected"}


@app.get("/api/stats")
async def get_stats():
    iface_rates = _bw.get_interface_rates()
    total = _bw.get_total_rates()
    return {
        "source":          "mikrotik" if _mikrotik else "local",
        "interface_rates": iface_rates,
        "total":           total,
        "last_scan":       _last_scan,
        "device_count":    len(_mikrotik.get_devices()) if _mikrotik else len(_devices),
    }


class LimitRequest(BaseModel):
    limit_mbps: int   # 0 = remove limit (unlimited)


@app.get("/api/limits")
async def get_limits():
    if not _mikrotik:
        return {"limits": {}, "presets": [0, 5, 30, 500, 1000]}
    from .mikrotik import LIMIT_PRESETS
    return {"limits": _mikrotik.get_device_limits(), "presets": LIMIT_PRESETS}


@app.post("/api/limits/{ip}")
async def set_limit(ip: str, body: LimitRequest):
    if not _mikrotik:
        raise HTTPException(status_code=503, detail="MikroTik not connected")
    from .mikrotik import LIMIT_PRESETS
    if body.limit_mbps != 0 and body.limit_mbps not in LIMIT_PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"limit_mbps must be one of {LIMIT_PRESETS}",
        )
    ok = await _mikrotik.set_device_limit(ip, body.limit_mbps)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to apply queue on router")
    return {"ok": True, "ip": ip, "limit_mbps": body.limit_mbps}


@app.delete("/api/limits/{ip}")
async def remove_limit(ip: str):
    if not _mikrotik:
        raise HTTPException(status_code=503, detail="MikroTik not connected")
    ok = await _mikrotik.remove_device_limit(ip)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to remove queue on router")
    return {"ok": True, "ip": ip, "limit_mbps": 0}


@app.get("/api/device/{query}")
async def lookup_device(query: str):
    """Look up a single device by IP address or MAC address (case-insensitive)."""
    devices = _mikrotik.get_devices() if _mikrotik else _devices
    q = query.lower().strip()
    for dev in devices:
        if dev.get("ip", "").lower() == q or dev.get("mac", "").lower() == q:
            limits = _mikrotik.get_device_limits() if _mikrotik else {}
            return {**dev, "limit_mbps": limits.get(dev.get("ip", ""), 0)}
    raise HTTPException(status_code=404, detail=f"No device found for '{query}'")


@app.post("/api/scan")
async def trigger_scan():
    if _mikrotik:
        # Force an immediate device refresh on the MikroTik monitor
        await _mikrotik._refresh_devices()
        await _mikrotik._refresh_router_ports()
        if _mikrotik.switch:
            await _mikrotik._refresh_switch_ports()
        count = len(_mikrotik.get_devices())
    else:
        await _do_local_scan()
        count = len(_devices)
    return {"ok": True, "device_count": count}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.append(ws)
    logger.info("WS client connected (total: %d)", len(_clients))
    try:
        await ws.send_text(json.dumps(_build_payload()))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if ws in _clients:
            _clients.remove(ws)
        logger.info("WS client disconnected (total: %d)", len(_clients))


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

_static = Path(__file__).parent.parent / "static"
app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")
