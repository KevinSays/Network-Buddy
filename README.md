# MHS: Mikrotik Homelab Scanner

Home network scanner and real-time bandwidth monitor with a web dashboard.
When a MikroTik router is present it uses the RouterOS REST API for authoritative
device names, exact per-device bandwidth, and per-port traffic — no packet
sniffing required. Falls back to local ARP/nmap scanning if the router is
unreachable.

## Features

- Discovers all devices via RouterOS DHCP leases + ARP (or local ARP/nmap)
- Real-time **per-device** upload / download rates via RouterOS IP Accounting
- **Per-port traffic** panel for the RB5009 router and CRS310 switch
- WAN throughput chart (2-minute rolling window)
- Live updates via WebSocket every 2 s; auto-rescan every 30 s
- Graceful fallback: works without MikroTik (local scan + psutil)

## MikroTik Setup  *(required for full features)*

### 1 — Enable the REST API on the router

```
/ip service enable www-ssl
/ip service set www-ssl port=443
```

### 2 — Create a read/write API user (recommended over using `admin`)

```
/user group add name=netbuddy policy=read,write,api,!local,!telnet,!ssh,!ftp,!reboot,!policy,!test,!winbox,!password,!web,!sniff,!sensitive,!romon
/user add name=netbuddy group=netbuddy password=STRONG_PASSWORD
```

### 3 — Enable IP Accounting (for per-device rates)

MHS enables this automatically via the API on startup. You can also
do it manually:

```
/ip accounting set enabled=yes account-local-traffic=no
```

### 4 — Repeat for the CRS310 switch

Same steps — REST API, a dedicated user, and note the switch management IP.

## Bandwidth Throttling

When RouterOS is the active data source a **Throttle** column appears in the device
table. Use the dropdown to instantly cap any device's upload and download speed.

### Preset limits

| Option    | RouterOS queue value |
|-----------|----------------------|
| Unlimited | queue removed        |
| 5 Mbps    | `5M/5M`              |
| 30 Mbps   | `30M/30M`            |
| 500 Mbps  | `500M/500M`          |
| 1 Gbps    | `1G/1G`              |

Limits apply symmetrically to upload and download.

### How it works

MHS creates a RouterOS **Simple Queue** named `nb-<ip>` (e.g.
`nb-192.168.4.42`) for each throttled device. You can see and edit these in Winbox
under **Queues → Simple Queues**. Setting a device back to *Unlimited* deletes the
queue entirely.

Limits persist on the router — they survive an app restart and remain in effect even
if MHS is not running. Limits set or changed outside the app (e.g. in
Winbox) are reflected in the dashboard within 30 s.

### REST API

| Method | Endpoint | Body | Action |
|--------|----------|------|--------|
| `GET` | `/api/device/{query}` | — | Look up a device by IP or MAC (404 if not found) |
| `GET` | `/api/history/{ip}?minutes=60` | — | Bandwidth history samples for a device |
| `GET` | `/api/transient?hours=24` | — | Devices that joined and disconnected within 5 min |
| `GET` | `/api/limits` | — | List all active limits `{ip: mbps}` |
| `POST` | `/api/limits/{ip}` | `{"limit_mbps": 30}` | Set limit (0 = unlimited) |
| `DELETE` | `/api/limits/{ip}` | — | Remove limit (unlimited) |

## System Requirements

| Requirement | Minimum | Notes |
|-------------|---------|-------|
| OS | Linux | Uses `/proc/net/arp` and systemd; macOS/Windows not supported |
| Python | 3.9+ | Checked by the installer |
| `nmap` binary | any recent | Required for fallback LAN scan — not needed if a MikroTik router is configured |
| `ping` (`iputils-ping`) | — | Used by fallback ping sweep; pre-installed on most distros |
| systemd | — | Required for `install.sh` service setup; not needed for `run.sh` dev mode |

**Install system packages (Debian / Ubuntu / Raspberry Pi OS):**

```bash
sudo apt install python3 python3-venv nmap
```

**Permissions:** The installer grants the service `CAP_NET_RAW` and `CAP_NET_ADMIN` so
nmap and scapy can do raw-socket scanning without running as root.

---

## Install as a service  *(recommended)*

One command installs MHS as a systemd service that starts on boot and restarts
automatically on failure.

```bash
sudo bash install.sh
```

The installer will:
1. Create a Python virtualenv and install all dependencies
2. Prompt for your MikroTik credentials and write `.env`
3. Register and start the `mhs` systemd service

**Useful commands after install:**

```bash
systemctl status mhs          # check if it's running
journalctl -fu mhs            # tail live logs
sudo systemctl restart mhs    # apply .env changes
sudo bash uninstall.sh        # remove the service
```

Dashboard is available at **`http://<this-machine-IP>:8000`** from any device on
your network.

## Quick Start  *(dev / one-off)*

```bash
# 1. Copy and edit credentials
cp .env.example .env
# Edit .env — set ROUTER_PASS, SWITCH_HOST, SWITCH_PASS

# 2. Run (foreground)
bash run.sh
```

Open **http://localhost:8000**. The process runs in the foreground and stops
when the terminal is closed — use the service install above for persistent
operation.

## Configuration (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `ROUTER_HOST` | `192.168.4.1` | RB5009 IP |
| `ROUTER_USER` | `admin` | RouterOS username |
| `ROUTER_PASS` | *(empty)* | RouterOS password |
| `SWITCH_HOST` | *(empty)* | CRS310 IP — leave blank to skip |
| `SWITCH_USER` | `admin` | Switch username |
| `SWITCH_PASS` | *(empty)* | Switch password |

## Dependencies

| Package | Purpose |
|---------|---------|
| `fastapi` + `uvicorn` | Web server & WebSocket |
| `httpx` | Async HTTP for RouterOS REST API |
| `scapy` | Fallback ARP scanning & packet capture |
| `psutil` | Fallback interface bandwidth stats |
| `python-nmap` | Fallback enhanced scanning |
| `python-dotenv` | Loads `.env` credentials |

## Data source priority

```
MikroTik RouterOS REST API  ←── used when router is reachable
   • DHCP leases  → authoritative hostnames
   • ARP table    → IP / MAC mapping
   • IP Accounting → exact per-device upload / download rates
   • /interface   → per-port traffic on router and switch

Local fallback  ←── when MikroTik is unreachable
   • nmap → scapy ARP → /proc/net/arp
   • psutil interface counters (total only)
   • scapy packet sniffer (per-device, needs root)
```

## Traffic Logging

MHS logs per-device bandwidth to a local SQLite database (`mhs.db`) every 30 seconds.

| Feature | Detail |
|---------|--------|
| Retention | 7 days of bandwidth history |
| Sample interval | 30 s |
| History query | `GET /api/history/{ip}?minutes=60` |
| Dashboard | Click any device row to open a 1-hour history chart |

### Recently Disconnected

The dashboard shows a **Recently Disconnected** section for devices that joined the
network but disconnected within 5 minutes. This catches:

- Phones connecting briefly to check connectivity
- Scanning/probing devices
- Misconfigured devices that drop off quickly

| Setting | Value |
|---------|-------|
| Session considered "transient" if | online < 5 minutes |
| Session closed after offline for | 3 minutes |
| Lookback window shown | last 24 hours |

Query directly: `GET /api/transient?hours=24`

## Project Layout

```
Network-Buddy/
├── app/
│   ├── main.py        # FastAPI app — orchestrates data sources
│   ├── mikrotik.py    # RouterOS REST client + monitor
│   ├── scanner.py     # Local device discovery (fallback)
│   ├── bandwidth.py   # Local bandwidth monitor (fallback)
│   └── db.py          # SQLite traffic log + device session store
├── static/
│   ├── index.html     # Dashboard
│   ├── style.css      # Dark theme
│   └── app.js         # WebSocket client, charts, port panel
├── settings.py        # Config (reads .env)
├── run.py             # Entry point
├── run.sh             # Convenience start script
├── .env.example       # Credential template
├── requirements.txt
└── mhs.db             # SQLite database (created at runtime, gitignored)
```
