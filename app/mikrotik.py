"""
MikroTik RouterOS REST API client and network monitor.

Provides:
  • Device discovery via DHCP leases + ARP table (authoritative device names)
  • Per-device bandwidth via RouterOS IP Accounting (exact byte counts)
  • Per-interface traffic stats for router and switch ports

Requires RouterOS v7.1+ with REST API enabled.
REST API docs: https://help.mikrotik.com/docs/display/ROS/REST+API
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Low-level REST client
# ---------------------------------------------------------------------------


class RouterOSClient:
    """Thin async wrapper around the RouterOS REST API."""

    def __init__(
        self,
        host: str,
        username: str = "admin",
        password: str = "",
        verify_ssl: bool = False,
    ):
        self.host = host
        self._base = f"https://{host}/rest"
        self._auth = (username, password)
        self._verify = verify_ssl
        self._http: Optional[httpx.AsyncClient] = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                auth=self._auth,
                verify=self._verify,
                timeout=httpx.Timeout(10.0),
            )
        return self._http

    async def get(self, path: str, params: Optional[dict] = None) -> list:
        client = await self._client()
        url = f"{self._base}/{path.lstrip('/')}"
        try:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else [data]
        except httpx.HTTPStatusError as exc:
            logger.warning("RouterOS GET %s → %s", path, exc.response.status_code)
        except httpx.ConnectError:
            logger.warning("Cannot connect to RouterOS at %s", self.host)
        except Exception as exc:
            logger.warning("RouterOS GET %s failed: %s", path, exc)
        return []

    async def patch(self, path: str, body: dict) -> bool:
        client = await self._client()
        url = f"{self._base}/{path.lstrip('/')}"
        try:
            r = await client.patch(url, json=body)
            return r.status_code in (200, 204)
        except Exception as exc:
            logger.warning("RouterOS PATCH %s failed: %s", path, exc)
            return False

    async def ping(self) -> bool:
        result = await self.get("/system/identity")
        return bool(result)

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()


# ---------------------------------------------------------------------------
# High-level monitor
# ---------------------------------------------------------------------------


class MikroTikMonitor:
    """
    Polls both the RB5009 router and (optionally) the CRS310 switch.

    Internal polling loops run as asyncio Tasks:
      • Device list  — every 30 s  (DHCP leases + ARP)
      • Traffic/accounting — every 5 s  (per-device + per-port rates)
    """

    DEVICE_POLL  = 30   # seconds
    TRAFFIC_POLL = 5    # seconds

    def __init__(self, router_creds, switch_creds=None):
        self.router = RouterOSClient(
            router_creds.host,
            router_creds.username,
            router_creds.password,
            router_creds.verify_ssl,
        )
        self.switch: Optional[RouterOSClient] = None
        if switch_creds and switch_creds.host:
            self.switch = RouterOSClient(
                switch_creds.host,
                switch_creds.username,
                switch_creds.password,
                switch_creds.verify_ssl,
            )

        # ── Shared state (read by main.py) ──────────────────────────────
        self._devices: List[Dict] = []
        self._router_ports: List[Dict] = []
        self._switch_ports: List[Dict] = []
        self._device_rates: Dict[str, Dict[str, float]] = {}  # ip → {up, dl}

        # ── Accounting state ────────────────────────────────────────────
        self._accounting_ok = False
        self._acct_time: float = 0.0

        # ── Interface rate-calculation state ────────────────────────────
        self._rtr_prev: Dict[str, Dict] = {}   # name → {rx, tx, t}
        self._sw_prev:  Dict[str, Dict] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        self._accounting_ok = await self.router.patch(
            "/ip/accounting",
            {"enabled": "yes", "account-local-traffic": "no"},
        )
        if self._accounting_ok:
            logger.info("RouterOS IP accounting enabled → per-device rates active")
        else:
            logger.warning(
                "IP accounting unavailable — per-device rates will read 0. "
                "Check that the API user has 'write' permission."
            )

        # Kick off background tasks
        asyncio.create_task(self._device_loop(), name="mt-devices")
        asyncio.create_task(self._traffic_loop(), name="mt-traffic")

    async def close(self):
        await self.router.close()
        if self.switch:
            await self.switch.close()

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------

    async def _device_loop(self):
        while True:
            try:
                await self._refresh_devices()
            except Exception as exc:
                logger.error("Device loop error: %s", exc)
            await asyncio.sleep(self.DEVICE_POLL)

    async def _traffic_loop(self):
        while True:
            try:
                await asyncio.gather(
                    self._refresh_router_ports(),
                    self._refresh_switch_ports() if self.switch else asyncio.sleep(0),
                    self._refresh_accounting() if self._accounting_ok else asyncio.sleep(0),
                )
            except Exception as exc:
                logger.error("Traffic loop error: %s", exc)
            await asyncio.sleep(self.TRAFFIC_POLL)

    # ------------------------------------------------------------------
    # Device discovery
    # ------------------------------------------------------------------

    async def _refresh_devices(self):
        leases_raw, arp_raw = await asyncio.gather(
            self.router.get("/ip/dhcp-server/lease"),
            self.router.get("/ip/arp"),
        )

        # MAC → DHCP lease info (hostname, comment, status)
        leases: Dict[str, Dict] = {}
        for lease in leases_raw:
            mac = lease.get("mac-address", "").lower()
            if mac:
                leases[mac] = lease

        devices = []
        seen: set = set()

        for entry in arp_raw:
            ip = entry.get("address", "")
            mac = entry.get("mac-address", "").lower()
            if not ip or ip in seen:
                continue
            if entry.get("invalid") == "true":
                continue
            seen.add(ip)

            lease = leases.get(mac, {})
            hostname = (
                lease.get("host-name")
                or lease.get("comment")
                or entry.get("comment")
                or ""
            )

            devices.append({
                "ip": ip,
                "mac": mac,
                "hostname": hostname,
                "vendor": "",
                "interface": entry.get("interface", ""),
                "status": "online",
                "scan_method": "mikrotik_api",
                "lease_status": lease.get("status", ""),
                "comment": lease.get("comment", ""),
            })

        self._devices = devices
        logger.debug("RouterOS: %d devices", len(devices))

    # ------------------------------------------------------------------
    # IP Accounting  →  per-device rates
    # ------------------------------------------------------------------

    async def _refresh_accounting(self):
        snapshot = await self.router.get("/ip/accounting/snapshot")
        if not snapshot:
            return

        now = time.time()
        # Snapshot resets counters each call; bytes counted = bytes in last interval
        elapsed = now - self._acct_time if self._acct_time else self.TRAFFIC_POLL
        self._acct_time = now

        # Aggregate by IP — src = sender (upload), dst = receiver (download)
        ip_up:  Dict[str, int] = {}
        ip_dl:  Dict[str, int] = {}

        for row in snapshot:
            src = row.get("src-address", "")
            dst = row.get("dst-address", "")
            try:
                b = int(row.get("bytes", 0))
            except (ValueError, TypeError):
                continue
            if src:
                ip_up[src] = ip_up.get(src, 0) + b
            if dst:
                ip_dl[dst] = ip_dl.get(dst, 0) + b

        rates: Dict[str, Dict[str, float]] = {}
        for ip in set(ip_up) | set(ip_dl):
            rates[ip] = {
                "upload_bps":   (ip_up.get(ip, 0) * 8) / elapsed,
                "download_bps": (ip_dl.get(ip, 0) * 8) / elapsed,
            }

        self._device_rates = rates

    # ------------------------------------------------------------------
    # Interface / port stats
    # ------------------------------------------------------------------

    async def _refresh_router_ports(self):
        self._router_ports = await self._poll_interfaces(
            self.router, self._rtr_prev, device_label="router"
        )

    async def _refresh_switch_ports(self):
        if not self.switch:
            return
        self._switch_ports = await self._poll_interfaces(
            self.switch, self._sw_prev, device_label="switch"
        )

    async def _poll_interfaces(
        self,
        client: RouterOSClient,
        prev_state: Dict[str, Dict],
        device_label: str,
    ) -> List[Dict]:
        raw = await client.get(
            "/interface",
            params={
                ".proplist": (
                    "name,type,running,disabled,"
                    "rx-byte,tx-byte,"
                    "rx-bits-per-second,tx-bits-per-second,"
                    "mac-address,comment,last-link-up-time"
                )
            },
        )

        now = time.time()
        ports = []

        for iface in raw:
            if iface.get("disabled") == "true":
                continue

            name = iface.get("name", "")
            rx_bytes = _int(iface.get("rx-byte"))
            tx_bytes = _int(iface.get("tx-byte"))

            # RouterOS sometimes exposes live bps directly
            rx_bps = _int(iface.get("rx-bits-per-second"))
            tx_bps = _int(iface.get("tx-bits-per-second"))

            # Compute from byte deltas when live rates unavailable / zero
            prev = prev_state.get(name)
            if prev and (rx_bps == 0 and tx_bps == 0):
                elapsed = now - prev["t"]
                if elapsed > 0:
                    rx_bps = max(0, int((rx_bytes - prev["rx"]) * 8 / elapsed))
                    tx_bps = max(0, int((tx_bytes - prev["tx"]) * 8 / elapsed))

            prev_state[name] = {"rx": rx_bytes, "tx": tx_bytes, "t": now}

            ports.append({
                "name": name,
                "type": iface.get("type", "ether"),
                "running": iface.get("running", "false") == "true",
                "rx_bps": rx_bps,
                "tx_bps": tx_bps,
                "rx_bytes": rx_bytes,
                "tx_bytes": tx_bytes,
                "mac": iface.get("mac-address", ""),
                "comment": iface.get("comment", ""),
                "device": device_label,
            })

        return ports

    # ------------------------------------------------------------------
    # Public getters (called by main.py)
    # ------------------------------------------------------------------

    def get_devices(self) -> List[Dict]:
        rates = self._device_rates
        return [
            {
                **dev,
                "upload_bps":   rates.get(dev["ip"], {}).get("upload_bps",   0.0),
                "download_bps": rates.get(dev["ip"], {}).get("download_bps", 0.0),
            }
            for dev in self._devices
        ]

    def get_router_ports(self) -> List[Dict]:
        return list(self._router_ports)

    def get_switch_ports(self) -> List[Dict]:
        return list(self._switch_ports)

    def get_wan_stats(self) -> Dict[str, float]:
        """Best-effort WAN interface stats from the router."""
        # RB5009: ether1 is typically the WAN port
        for port in self._router_ports:
            if port["name"] in ("ether1", "wan", "WAN", "ether-wan"):
                return {
                    "download_bps": port["rx_bps"],
                    "upload_bps":   port["tx_bps"],
                }
        # Fallback: busiest port
        active = [p for p in self._router_ports if p["running"]]
        if active:
            best = max(active, key=lambda p: p["rx_bps"] + p["tx_bps"])
            return {"download_bps": best["rx_bps"], "upload_bps": best["tx_bps"]}
        return {"download_bps": 0.0, "upload_bps": 0.0}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _int(val) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0
