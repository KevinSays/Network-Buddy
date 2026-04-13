"""
MHS: Mikrotik Homelab Scanner — FastAPI backend.

Data source priority:
  1. MikroTik RouterOS REST API  (if reachable — authoritative)
  2. Local ARP / nmap scan + psutil bandwidth  (fallback)

Endpoints:
  GET    /api/health              — uptime, connection status, db size
  GET    /api/devices             — device list with per-device bandwidth
  GET    /api/device/{query}      — look up a single device by IP or MAC
  GET    /api/history/wan         — WAN bandwidth history (SQLite)
  GET    /api/history/wan/export  — WAN history as CSV download
  GET    /api/history/{ip}        — per-device bandwidth history (SQLite)
  GET    /api/history/{ip}/export — device history as CSV download
  GET    /api/transient           — devices that joined then quickly disconnected
  GET    /api/aliases             — all user-set device aliases
  PUT    /api/alias/{ip}          — set a friendly name for a device
  DELETE /api/alias/{ip}          — remove alias (revert to router hostname)
  GET    /api/ports               — router + switch port stats
  GET    /api/stats               — interface totals + meta
  POST   /api/scan                — trigger an immediate re-scan / refresh
  WS     /ws                      — real-time push every 2 s
  GET    /                        — dashboard SPA
"""

import asyncio
import base64
import csv
import io
import ipaddress
import json
import logging
import re
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response as StarletteResponse

from .scanner import scan_network
from .bandwidth import BandwidthMonitor
from . import db, alerts

sys.path.insert(0, str(Path(__file__).parent.parent))
from settings import settings as _settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

SCAN_INTERVAL        = 30      # seconds between local re-scans (fallback mode)
PUSH_INTERVAL        = 2       # seconds between WebSocket pushes
LOG_INTERVAL         = 30      # seconds between SQLite traffic log writes
RECONNECT_INTERVAL   = 60      # seconds between MikroTik reconnection attempts
MAX_HISTORY_MINUTES  = 10_080  # 7 days — upper bound for ?minutes= param
SCAN_COOLDOWN        = 10      # minimum seconds between manual /api/scan calls

# Regex for safe filename component (alphanumeric + dots + underscores only)
_SAFE_FILENAME_RE = re.compile(r"[^\w.]")


def _validate_ip(ip: str) -> str:
    """Raise HTTP 422 if *ip* is not a valid IPv4 address. Returns the ip."""
    try:
        ipaddress.IPv4Address(ip)
        return ip
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid IPv4 address: {ip!r}")


def _clamp_minutes(minutes: int) -> int:
    """Clamp history window to [1, MAX_HISTORY_MINUTES]."""
    return max(1, min(minutes, MAX_HISTORY_MINUTES))

# ---------------------------------------------------------------------------
# Optional HTTP Basic Auth middleware
# ---------------------------------------------------------------------------

class _BasicAuth(BaseHTTPMiddleware):
    def __init__(self, app, username: str, password: str):
        super().__init__(app)
        self._token = base64.b64encode(f"{username}:{password}".encode()).decode()

    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic ") and secrets.compare_digest(auth[6:], self._token):
            return await call_next(request)
        return StarletteResponse(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="MHS"'},
            content="Unauthorized",
        )


app = FastAPI(title="MHS: Mikrotik Homelab Scanner", version="1.2.0")

if _settings.dashboard_user and _settings.dashboard_pass:
    app.add_middleware(
        _BasicAuth,
        username=_settings.dashboard_user,
        password=_settings.dashboard_pass,
    )
    # Pre-compute token for WebSocket auth check (middleware doesn't cover WS)
    _ws_auth_token = base64.b64encode(
        f"{_settings.dashboard_user}:{_settings.dashboard_pass}".encode()
    ).decode()
    logger.info("Dashboard basic auth enabled (user: %s)", _settings.dashboard_user)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_devices: List[dict]        = []
_last_scan: float           = 0.0
_last_scan_trigger: float   = 0.0   # for /api/scan rate-limiting
_scan_lock                  = asyncio.Lock()
_startup_time: float        = 0.0
_ws_auth_token: str         = ""    # pre-computed Basic Auth token for WS check

_bw       = BandwidthMonitor()
_mikrotik = None
_clients: List[WebSocket] = []

# Caches refreshed by _logging_loop every LOG_INTERVAL seconds
_transient_cache:  List[dict]       = []
_aliases_cache:    Dict[str, str]   = {}
_seen_times_cache: Dict[str, Dict]  = {}
_prev_ips:         set              = set()

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup():
    global _mikrotik, _startup_time, _aliases_cache, _seen_times_cache
    _startup_time = time.time()
    db.init_db()
    _aliases_cache    = db.get_aliases()
    _seen_times_cache = db.get_device_seen_times()

    _bw.start()

    try:
        from .mikrotik import MikroTikMonitor
        monitor   = MikroTikMonitor(_settings.router, _settings.switch)
        reachable = await monitor.router.ping()
        if reachable:
            await monitor.start()
            _mikrotik = monitor
            logger.info("MikroTik connected at %s", _settings.router.host)
        else:
            await monitor.close()
            logger.warning("MikroTik at %s unreachable — local scan fallback", _settings.router.host)
    except Exception as exc:
        logger.warning("MikroTik init failed (%s) — local scan fallback", exc)

    asyncio.create_task(_local_scan_loop(),  name="local-scan")
    asyncio.create_task(_push_loop(),        name="ws-push")
    asyncio.create_task(_logging_loop(),     name="db-log")
    asyncio.create_task(_reconnect_loop(),   name="mt-reconnect")


@app.on_event("shutdown")
async def on_shutdown():
    _bw.stop()
    if _mikrotik:
        await _mikrotik.close()


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _local_scan_loop():
    while True:
        if _mikrotik is None:
            await _do_local_scan()
        await asyncio.sleep(SCAN_INTERVAL)


async def _do_local_scan():
    global _devices, _last_scan
    async with _scan_lock:
        logger.info("Starting local network scan…")
        loop = asyncio.get_running_loop()
        _devices   = await loop.run_in_executor(None, scan_network)
        _last_scan = time.time()
        logger.info("Local scan complete — %d device(s)", len(_devices))


async def _reconnect_loop():
    """Periodically check MikroTik reachability and reconnect/fallback as needed."""
    global _mikrotik
    while True:
        await asyncio.sleep(RECONNECT_INTERVAL)
        try:
            if _mikrotik is None:
                from .mikrotik import MikroTikMonitor
                monitor   = MikroTikMonitor(_settings.router, _settings.switch)
                reachable = await monitor.router.ping()
                if reachable:
                    await monitor.start()
                    _mikrotik = monitor
                    logger.info("MikroTik reconnected at %s", _settings.router.host)
                else:
                    await monitor.close()
            else:
                alive = await _mikrotik.router.ping()
                if not alive:
                    logger.warning("MikroTik went offline — switching to local scan")
                    await _mikrotik.close()
                    _mikrotik = None
        except Exception as exc:
            logger.warning("Reconnect loop error: %s", exc)


async def _logging_loop():
    """Write traffic + WAN + session snapshots to SQLite every LOG_INTERVAL s."""
    global _transient_cache, _aliases_cache, _seen_times_cache, _prev_ips
    while True:
        await asyncio.sleep(LOG_INTERVAL)
        try:
            if _mikrotik:
                devices  = _mikrotik.get_devices()
                wan      = _mikrotik.get_wan_stats()
                wan_dl   = wan["download_bps"]
                wan_ul   = wan["upload_bps"]
            else:
                rates  = _bw.get_device_rates()
                total  = _bw.get_total_rates()
                devices = [
                    {**dev,
                     "upload_bps":   rates.get(dev["ip"], {}).get("upload_bps",   0.0),
                     "download_bps": rates.get(dev["ip"], {}).get("download_bps", 0.0)}
                    for dev in _devices
                ]
                wan_dl = total["download_bps"]
                wan_ul = total["upload_bps"]

            device_map  = {d["ip"]: d for d in devices}
            current_ips = set(device_map)
            loop        = asyncio.get_running_loop()

            await loop.run_in_executor(None, db.log_traffic, devices)
            await loop.run_in_executor(None, db.log_wan, wan_dl, wan_ul)

            new_devices = await loop.run_in_executor(
                None, db.update_sessions, current_ips, device_map
            )

            _transient_cache  = await loop.run_in_executor(None, db.get_transient_devices, 24)
            _aliases_cache    = await loop.run_in_executor(None, db.get_aliases)
            _seen_times_cache = await loop.run_in_executor(None, db.get_device_seen_times)

            # ── Alerts ──────────────────────────────────────────────────────
            wh  = _settings.alert_webhook_url
            ntfy = _settings.alert_ntfy_topic

            for dev in new_devices:
                label = dev.get("hostname") or dev.get("ip", "?")
                asyncio.create_task(alerts.send(
                    "New Device", f"{label} ({dev.get('mac','?')}) joined the network",
                    webhook_url=wh, ntfy_topic=ntfy,
                ))

            # Offline alert for aliased devices that just disappeared
            if _prev_ips:
                newly_gone = _prev_ips - current_ips
                for ip in newly_gone:
                    if ip in _aliases_cache:
                        alias = _aliases_cache[ip]
                        asyncio.create_task(alerts.send(
                            "Device Offline", f"{alias} ({ip}) went offline",
                            webhook_url=wh, ntfy_topic=ntfy,
                        ))

            _prev_ips = current_ips

        except Exception as exc:
            logger.warning("Logging loop error: %s", exc)


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
        devices  = _mikrotik.get_devices()
        wan      = _mikrotik.get_wan_stats()
        ports    = {"router": _mikrotik.get_router_ports(), "switch": _mikrotik.get_switch_ports()}
        total_dl = wan["download_bps"]
        total_ul = wan["upload_bps"]
        source   = "mikrotik"
    else:
        device_rates = _bw.get_device_rates()
        total        = _bw.get_total_rates()
        devices = [
            {**dev,
             "upload_bps":   device_rates.get(dev["ip"], {}).get("upload_bps",   0.0),
             "download_bps": device_rates.get(dev["ip"], {}).get("download_bps", 0.0)}
            for dev in _devices
        ]
        ports    = {"router": [], "switch": []}
        total_dl = total["download_bps"]
        total_ul = total["upload_bps"]
        source   = "local"

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
        "transient_devices":  _transient_cache,
        "aliases":            _aliases_cache,
        "seen_times":         _seen_times_cache,
    }


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    db_size = db.DB_PATH.stat().st_size if db.DB_PATH.exists() else 0
    return {
        "ok":                True,
        "version":           app.version,
        "uptime_seconds":    round(time.time() - _startup_time),
        "mikrotik_connected": _mikrotik is not None,
        "device_count":      len(_mikrotik.get_devices()) if _mikrotik else len(_devices),
        "last_scan":         _last_scan,
        "db_size_bytes":     db_size,
    }


@app.get("/api/devices")
async def get_devices():
    return _build_payload()


# ── WAN history (must be defined before /api/history/{ip} to avoid clash) ──

@app.get("/api/history/wan")
async def get_wan_history(minutes: int = 60):
    minutes = _clamp_minutes(minutes)
    loop    = asyncio.get_running_loop()
    samples = await loop.run_in_executor(None, db.get_wan_history, minutes)
    return {"minutes": minutes, "samples": samples}


@app.get("/api/history/wan/export")
async def export_wan_csv(minutes: int = 10_080):   # default: 7 days
    minutes = _clamp_minutes(minutes)
    loop    = asyncio.get_running_loop()
    samples = await loop.run_in_executor(None, db.get_wan_history, minutes)
    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(["timestamp_iso", "timestamp_unix", "download_bps", "upload_bps"])
    for s in samples:
        w.writerow([datetime.fromtimestamp(s["ts"]).isoformat(),
                    round(s["ts"], 3), round(s["download_bps"]), round(s["upload_bps"])])
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="mhs-wan-history.csv"'},
    )


# ── Per-device history ──────────────────────────────────────────────────────

@app.get("/api/history/{ip}")
async def get_device_history(ip: str, minutes: int = 60):
    _validate_ip(ip)
    minutes = _clamp_minutes(minutes)
    loop    = asyncio.get_running_loop()
    samples = await loop.run_in_executor(None, db.get_history, ip, minutes)
    return {"ip": ip, "minutes": minutes, "samples": samples}


@app.get("/api/history/{ip}/export")
async def export_device_csv(ip: str, minutes: int = 10_080):   # default: 7 days
    _validate_ip(ip)
    minutes = _clamp_minutes(minutes)
    loop    = asyncio.get_running_loop()
    samples = await loop.run_in_executor(None, db.get_history, ip, minutes)
    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(["timestamp_iso", "timestamp_unix", "upload_bps", "download_bps"])
    for s in samples:
        w.writerow([datetime.fromtimestamp(s["ts"]).isoformat(),
                    round(s["ts"], 3), round(s["upload_bps"]), round(s["download_bps"])])
    # Sanitize IP for use in filename — dots and digits only after validation
    safe_ip = _SAFE_FILENAME_RE.sub("_", ip)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="mhs-{safe_ip}-history.csv"'},
    )


# ── Transient devices ───────────────────────────────────────────────────────

@app.get("/api/transient")
async def get_transient(hours: int = 24):
    loop    = asyncio.get_running_loop()
    devices = await loop.run_in_executor(None, db.get_transient_devices, hours)
    return {"hours": hours, "devices": devices}


# ── Device lookup ───────────────────────────────────────────────────────────

@app.get("/api/device/{query}")
async def lookup_device(query: str):
    devices = _mikrotik.get_devices() if _mikrotik else _devices
    q = query.lower().strip()
    for dev in devices:
        if dev.get("ip", "").lower() == q or dev.get("mac", "").lower() == q:
            limits = _mikrotik.get_device_limits() if _mikrotik else {}
            return {**dev, "limit_mbps": limits.get(dev.get("ip", ""), 0)}
    raise HTTPException(status_code=404, detail=f"No device found for '{query}'")


# ── Aliases ─────────────────────────────────────────────────────────────────

@app.get("/api/aliases")
async def get_aliases():
    return {"aliases": _aliases_cache}


class AliasRequest(BaseModel):
    alias: str = Field(..., min_length=1, max_length=64)


@app.put("/api/alias/{ip}")
async def set_alias(ip: str, body: AliasRequest):
    global _aliases_cache
    _validate_ip(ip)
    alias = body.alias.strip()
    if not alias:
        raise HTTPException(status_code=400, detail="alias cannot be empty")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, db.set_alias, ip, alias)
    _aliases_cache[ip] = alias          # update cache immediately
    return {"ok": True, "ip": ip, "alias": alias}


@app.delete("/api/alias/{ip}")
async def delete_alias(ip: str):
    global _aliases_cache
    _validate_ip(ip)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, db.delete_alias, ip)
    _aliases_cache.pop(ip, None)        # update cache immediately
    return {"ok": True, "ip": ip}


# ── Ports / stats ───────────────────────────────────────────────────────────

@app.get("/api/ports")
async def get_ports():
    if _mikrotik:
        return {"router": _mikrotik.get_router_ports(), "switch": _mikrotik.get_switch_ports()}
    return {"router": [], "switch": [], "note": "MikroTik not connected"}


@app.get("/api/stats")
async def get_stats():
    return {
        "source":       "mikrotik" if _mikrotik else "local",
        "interface_rates": _bw.get_interface_rates(),
        "total":        _bw.get_total_rates(),
        "last_scan":    _last_scan,
        "device_count": len(_mikrotik.get_devices()) if _mikrotik else len(_devices),
    }


# ── Throttle limits ─────────────────────────────────────────────────────────

class LimitRequest(BaseModel):
    limit_mbps: int


@app.get("/api/limits")
async def get_limits():
    if not _mikrotik:
        return {"limits": {}, "presets": [0, 5, 30, 500, 1000]}
    from .mikrotik import LIMIT_PRESETS
    return {"limits": _mikrotik.get_device_limits(), "presets": LIMIT_PRESETS}


@app.post("/api/limits/{ip}")
async def set_limit(ip: str, body: LimitRequest):
    _validate_ip(ip)
    if not _mikrotik:
        raise HTTPException(status_code=503, detail="MikroTik not connected")
    from .mikrotik import LIMIT_PRESETS
    if body.limit_mbps != 0 and body.limit_mbps not in LIMIT_PRESETS:
        raise HTTPException(status_code=400, detail=f"limit_mbps must be one of {LIMIT_PRESETS}")
    if not await _mikrotik.set_device_limit(ip, body.limit_mbps):
        raise HTTPException(status_code=500, detail="Failed to apply queue on router")
    return {"ok": True, "ip": ip, "limit_mbps": body.limit_mbps}


@app.delete("/api/limits/{ip}")
async def remove_limit(ip: str):
    _validate_ip(ip)
    if not _mikrotik:
        raise HTTPException(status_code=503, detail="MikroTik not connected")
    if not await _mikrotik.remove_device_limit(ip):
        raise HTTPException(status_code=500, detail="Failed to remove queue on router")
    return {"ok": True, "ip": ip, "limit_mbps": 0}


# ── Manual scan ─────────────────────────────────────────────────────────────

@app.post("/api/scan")
async def trigger_scan():
    global _last_scan_trigger
    now = time.time()
    if now - _last_scan_trigger < SCAN_COOLDOWN:
        raise HTTPException(
            status_code=429,
            detail=f"Scan cooldown active — wait {SCAN_COOLDOWN} s between manual scans",
        )
    _last_scan_trigger = now

    if _mikrotik:
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
    # Enforce Basic Auth on WebSocket if enabled.
    # Browsers include the cached Authorization header on WS upgrade requests,
    # but we check explicitly here because BaseHTTPMiddleware only runs for HTTP.
    if _ws_auth_token:
        auth = ws.headers.get("Authorization", "")
        if not (auth.startswith("Basic ") and secrets.compare_digest(auth[6:], _ws_auth_token)):
            await ws.close(code=1008)   # 1008 = Policy Violation
            return
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
