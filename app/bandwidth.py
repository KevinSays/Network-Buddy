"""
Bandwidth / throughput monitor.

Two layers:
  1. Interface-level stats via psutil  — always available, no root needed.
  2. Per-device packet accounting via scapy — optional, requires root.

On a typical home switch only traffic visible to the scanner machine is
counted for per-device stats (i.e. traffic to/from the scanner itself,
plus broadcast/multicast).  The interface totals are always accurate.
"""

import time
import threading
import logging
from collections import defaultdict
from typing import Dict

import psutil

logger = logging.getLogger(__name__)

# How long (seconds) each sample bucket covers for rate calculation
SAMPLE_INTERVAL = 1.0


class BandwidthMonitor:
    def __init__(self):
        # Per-device cumulative byte counters  {ip: {"sent": int, "recv": int}}
        self._device_bytes: Dict[str, Dict[str, int]] = defaultdict(lambda: {"sent": 0, "recv": 0})
        # Calculated rates  {ip: {"upload_bps": float, "download_bps": float}}
        self._device_rates: Dict[str, Dict[str, float]] = {}

        # Interface-level snapshots for rate calculation
        self._iface_prev = self._get_iface_counters()
        self._iface_rates: Dict[str, Dict[str, float]] = {}

        # Per-device snapshot for rate calculation
        self._device_prev: Dict[str, Dict[str, int]] = {}

        self._lock = threading.Lock()
        self._running = False
        self._sniffer = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def start(self):
        self._running = True

        # Always start the psutil poller
        t = threading.Thread(target=self._psutil_loop, daemon=True, name="bw-psutil")
        t.start()

        # Optionally start scapy sniffer
        self._start_sniffer()

    def stop(self):
        self._running = False
        if self._sniffer:
            try:
                self._sniffer.stop()
            except Exception:
                pass

    def get_device_rates(self) -> Dict[str, Dict[str, float]]:
        with self._lock:
            return dict(self._device_rates)

    def get_interface_rates(self) -> Dict[str, Dict[str, float]]:
        with self._lock:
            return dict(self._iface_rates)

    def get_total_rates(self) -> Dict[str, float]:
        """Aggregate upload/download across all interfaces (excluding loopback)."""
        rates = self.get_interface_rates()
        upload = sum(v["upload_bps"] for v in rates.values())
        download = sum(v["download_bps"] for v in rates.values())
        return {"upload_bps": upload, "download_bps": download}

    # ------------------------------------------------------------------
    # psutil interface poller
    # ------------------------------------------------------------------

    def _get_iface_counters(self) -> Dict[str, Dict[str, int]]:
        stats = psutil.net_io_counters(pernic=True)
        return {
            iface: {"sent": c.bytes_sent, "recv": c.bytes_recv}
            for iface, c in stats.items()
            if iface != "lo"
        }

    def _psutil_loop(self):
        while self._running:
            time.sleep(SAMPLE_INTERVAL)
            current = self._get_iface_counters()
            rates = {}
            with self._lock:
                for iface, counters in current.items():
                    prev = self._iface_prev.get(iface, counters)
                    delta_sent = max(0, counters["sent"] - prev["sent"])
                    delta_recv = max(0, counters["recv"] - prev["recv"])
                    rates[iface] = {
                        "upload_bps": delta_sent / SAMPLE_INTERVAL,
                        "download_bps": delta_recv / SAMPLE_INTERVAL,
                    }
                self._iface_prev = current
                self._iface_rates = rates

            # Also recalculate per-device rates from cumulative counters
            with self._lock:
                new_rates = {}
                for ip, counters in self._device_bytes.items():
                    prev = self._device_prev.get(ip, {"sent": counters["sent"], "recv": counters["recv"]})
                    delta_sent = max(0, counters["sent"] - prev["sent"])
                    delta_recv = max(0, counters["recv"] - prev["recv"])
                    new_rates[ip] = {
                        "upload_bps": delta_sent / SAMPLE_INTERVAL,
                        "download_bps": delta_recv / SAMPLE_INTERVAL,
                    }
                    self._device_prev[ip] = dict(counters)
                self._device_rates = new_rates

    # ------------------------------------------------------------------
    # scapy per-device packet sniffer
    # ------------------------------------------------------------------

    def _start_sniffer(self):
        try:
            from scapy.all import AsyncSniffer, IP  # type: ignore

            def handle_packet(pkt):
                if IP in pkt:
                    size = len(pkt)
                    src = pkt[IP].src
                    dst = pkt[IP].dst
                    with self._lock:
                        self._device_bytes[src]["sent"] += size
                        self._device_bytes[dst]["recv"] += size

            self._sniffer = AsyncSniffer(
                prn=handle_packet,
                filter="ip",
                store=False,
            )
            self._sniffer.start()
            logger.info("scapy packet sniffer started (per-device traffic tracking active)")

        except PermissionError:
            logger.warning(
                "Packet sniffing requires root. Per-device bandwidth will show 0. "
                "Re-run with sudo for per-device stats."
            )
        except ImportError:
            logger.warning("scapy not installed — per-device bandwidth unavailable")
        except Exception as exc:
            logger.warning("Could not start packet sniffer: %s", exc)
