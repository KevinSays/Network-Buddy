"""
Network device scanner.
Tries nmap first, falls back to scapy ARP scan, then /proc/net/arp as last resort.
"""

import socket
import ipaddress
import logging
import subprocess
from typing import List, Dict

logger = logging.getLogger(__name__)


def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def get_local_network() -> str:
    local_ip = get_local_ip()
    network = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
    return str(network)


def resolve_hostname(ip: str, timeout: float = 1.0) -> str:
    """Resolve IP to hostname with a bounded timeout to avoid hanging scans."""
    old = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout)
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return ""
    finally:
        socket.setdefaulttimeout(old)


# ---------------------------------------------------------------------------
# nmap scan
# ---------------------------------------------------------------------------

def nmap_scan(network: str) -> List[Dict]:
    try:
        import nmap
        nm = nmap.PortScanner()
        nm.scan(hosts=network, arguments="-sn --host-timeout 5s")

        devices = []
        for host in nm.all_hosts():
            addrs = nm[host].get("addresses", {})
            vendor_map = nm[host].get("vendor", {})
            mac = addrs.get("mac", "")
            vendor = vendor_map.get(mac, "") if mac else ""

            devices.append({
                "ip": host,
                "mac": mac,
                "hostname": nm[host].hostname() or resolve_hostname(host),
                "vendor": vendor,
                "scan_method": "nmap",
                "status": "online",
            })

        logger.info("nmap found %d devices", len(devices))
        return devices

    except ImportError:
        logger.warning("python-nmap not installed")
        return []
    except Exception as exc:
        logger.warning("nmap scan failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# scapy ARP scan (requires root)
# ---------------------------------------------------------------------------

def arp_scan(network: str) -> List[Dict]:
    try:
        from scapy.all import ARP, Ether, srp  # type: ignore

        pkt = Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(pdst=network)
        answered, _ = srp(pkt, timeout=3, verbose=False)

        devices = []
        for _, rcv in answered:
            vendor = _lookup_vendor(rcv.hwsrc)
            devices.append({
                "ip": rcv.psrc,
                "mac": rcv.hwsrc,
                "hostname": resolve_hostname(rcv.psrc),
                "vendor": vendor,
                "scan_method": "arp",
                "status": "online",
            })

        logger.info("ARP scan found %d devices", len(devices))
        return devices

    except ImportError:
        logger.warning("scapy not installed")
        return []
    except PermissionError:
        logger.warning("ARP scan requires root privileges")
        return []
    except Exception as exc:
        logger.warning("ARP scan failed: %s", exc)
        return []


def _lookup_vendor(mac: str) -> str:
    """Best-effort MAC vendor lookup via scapy's manuf database."""
    try:
        from scapy.all import conf  # type: ignore
        if conf.manufdb:
            result = conf.manufdb._get_manuf(mac)
            return result or ""
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# /proc/net/arp fallback (no root needed, Linux only)
# ---------------------------------------------------------------------------

def proc_arp_scan() -> List[Dict]:
    """Read ARP cache from the kernel — works without root but only shows
    hosts that have already been communicated with."""
    devices = []
    try:
        with open("/proc/net/arp") as fh:
            next(fh)  # skip header
            for line in fh:
                parts = line.split()
                if len(parts) < 4:
                    continue
                ip, _, flags, mac = parts[0], parts[1], parts[2], parts[3]
                if mac == "00:00:00:00:00:00":
                    continue
                devices.append({
                    "ip": ip,
                    "mac": mac,
                    "hostname": resolve_hostname(ip),
                    "vendor": _lookup_vendor(mac),
                    "scan_method": "arp_cache",
                    "status": "online",
                })
        # Trigger ARP cache population via ping sweep
        _ping_sweep(get_local_network())
    except Exception as exc:
        logger.warning("proc ARP fallback failed: %s", exc)
    return devices


def _ping_sweep(network: str):
    """Quick ping sweep to populate the ARP cache (best-effort).

    Launches pings in batches of 32 to avoid exhausting file descriptors.
    Each process is given a 3-second wait timeout before being killed.
    """
    BATCH = 32
    try:
        net   = ipaddress.IPv4Network(network, strict=False)
        hosts = list(net.hosts())[:254]
        for i in range(0, len(hosts), BATCH):
            procs = [
                subprocess.Popen(
                    ["ping", "-c1", "-W1", str(h)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for h in hosts[i : i + BATCH]
            ]
            for p in procs:
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
    except Exception as exc:
        logger.debug("ping sweep error: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_network() -> List[Dict]:
    """Scan the local /24 subnet. Tries nmap → ARP → ARP cache."""
    network = get_local_network()
    logger.info("Scanning network: %s", network)

    # 1. nmap
    devices = nmap_scan(network)
    if devices:
        return devices

    # 2. scapy ARP
    devices = arp_scan(network)
    if devices:
        return devices

    # 3. kernel ARP cache
    logger.info("Falling back to /proc/net/arp cache")
    return proc_arp_scan()
