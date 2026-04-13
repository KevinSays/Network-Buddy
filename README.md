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
- WAN throughput chart with history (1 h / 6 h / 24 h / 7 d window)
- Live updates via WebSocket every 2 s; auto-rescan every 30 s
- **Sortable columns** — click any column header to sort the device table
- **First seen / Last seen** timestamps for every device
- **Device aliases** — set a friendly name for any device
- **Bandwidth throttling** — cap any device's speed via RouterOS Simple Queues
- **Traffic logging** — 7-day SQLite history with per-device history charts
- **Recently disconnected** section — catches transient / scanning devices
- **Alerts** — new device and offline notifications via webhook or ntfy.sh
- **HTTP Basic Auth** — optional password-protect the dashboard
- **Auto-reconnect** — reconnects to MikroTik automatically if it goes offline
- Graceful fallback: works without MikroTik (local scan + psutil)

---

## Where to run MHS

MHS runs on a **separate Linux machine on the same LAN** as your MikroTik.
It cannot run on the MikroTik device itself (RouterOS does not have Python).

```
┌─────────────────────┐         ┌──────────────────────┐
│   Raspberry Pi /    │  REST   │  MikroTik RB5009      │
│   Linux server      │ ──────► │  RouterOS REST API    │
│   (runs MHS)        │  :443   │  192.168.4.1          │
└─────────────────────┘         └──────────────────────┘
         │
         │  browser  http://<server-ip>:8000
         ▼
    Any device on your LAN
```

**Recommended hardware:**

| Device | Notes |
|--------|-------|
| Raspberry Pi 3 / 4 / 5 | Ideal — low power, always-on, ~$35–80 |
| Linux server or NAS | Perfect if you already have one |
| Proxmox LXC container | Great for homelabs |
| Any always-on Linux PC | Works fine |

---

## MikroTik Setup  *(required for full features)*

### 1 — Enable the REST API on the router

```
/ip service enable www-ssl
/ip service set www-ssl port=443
```

### 2 — Create a dedicated API user  *(recommended over using `admin`)*

```
/user group add name=mhs-api policy=read,write,api,!local,!telnet,!ssh,!ftp,!reboot,!policy,!test,!winbox,!password,!web,!sniff,!sensitive,!romon
/user add name=mhs-api group=mhs-api password=STRONG_PASSWORD
```

Set `ROUTER_USER=mhs-api` and `ROUTER_PASS=STRONG_PASSWORD` in your `.env`.

### 3 — Enable IP Accounting  *(for per-device bandwidth rates)*

MHS enables this automatically on startup. You can also do it manually:

```
/ip accounting set enabled=yes account-local-traffic=no
```

### 4 — Repeat for the CRS310 switch  *(optional)*

Same steps — REST API, a dedicated user, and note the switch management IP.
Set `SWITCH_HOST`, `SWITCH_USER`, `SWITCH_PASS` in `.env`.

---

## System Requirements

| Requirement | Minimum | Notes |
|-------------|---------|-------|
| OS | Linux | Uses `/proc/net/arp` and systemd |
| Python | 3.9+ | Checked by the installer |
| `nmap` | any recent | Fallback LAN scan — not needed with MikroTik |
| `ping` (`iputils-ping`) | — | Fallback ping sweep; pre-installed on most distros |
| `curl` or `wget` | — | Used by `install.sh` to download Chart.js |
| systemd | — | Required for `install.sh`; not needed for `run.sh` |

**Install system packages (Debian / Ubuntu / Raspberry Pi OS):**

```bash
sudo apt install python3 python3-venv nmap curl
```

**Permissions:** The installer grants `CAP_NET_RAW` and `CAP_NET_ADMIN` so
nmap and scapy can do raw-socket scanning without running as root.

---

## Install as a service  *(recommended)*

One command installs MHS as a systemd service that starts on boot:

```bash
sudo bash install.sh
```

The installer will:
1. Create a Python virtualenv and install all Python dependencies
2. Download Chart.js 4.4.0 locally (no CDN at runtime)
3. Prompt for your MikroTik credentials and write `.env`
4. Register and start the `mhs` systemd service

**Useful commands after install:**

```bash
systemctl status mhs          # check if it's running
journalctl -fu mhs            # tail live logs
sudo systemctl restart mhs    # apply .env changes
sudo bash uninstall.sh        # remove the service
```

Dashboard: **`http://<this-machine-IP>:8000`**

---

## Quick Start  *(dev / one-off)*

```bash
# 1. Copy and edit credentials
cp .env.example .env
nano .env          # set ROUTER_HOST, ROUTER_PASS at minimum

# 2. Install dependencies
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Download Chart.js
curl -fsSL https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.js \
     -o static/chart.umd.js

# 4. Run
python3 run.py
```

Open **http://localhost:8000**.

---

## Configuration (`.env`)

Copy `.env.example` → `.env` and fill in your values.

### Router / Switch

| Variable | Default | Description |
|----------|---------|-------------|
| `ROUTER_HOST` | `192.168.4.1` | RB5009 management IP |
| `ROUTER_USER` | `admin` | RouterOS username |
| `ROUTER_PASS` | *(empty)* | RouterOS password |
| `SWITCH_HOST` | *(empty)* | CRS310 IP — leave blank to skip |
| `SWITCH_USER` | `admin` | Switch username |
| `SWITCH_PASS` | *(empty)* | Switch password |

### App server

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_HOST` | `0.0.0.0` | Listen address |
| `APP_PORT` | `8000` | Listen port |

### Alerts  *(optional — leave blank to disable)*

| Variable | Description |
|----------|-------------|
| `ALERT_WEBHOOK_URL` | POST `{title, message, source}` JSON to this URL on new-device / offline events |
| `ALERT_NTFY_TOPIC` | Publish to `https://ntfy.sh/<topic>` (alphanumeric, hyphens, underscores only) |

Both are optional. Set one, both, or neither.

### Dashboard auth  *(optional — leave blank to disable)*

| Variable | Description |
|----------|-------------|
| `DASHBOARD_USER` | HTTP Basic Auth username |
| `DASHBOARD_PASS` | HTTP Basic Auth password |

When set, the browser will prompt for credentials before showing the dashboard.
The WebSocket connection is protected by the same credentials.

---

## Dashboard Features

### Device table

- **Sortable columns** — click any column header (Hostname, MAC, Vendor,
  Download, Upload, First Seen, Last Seen) to sort ascending; click again to reverse
- **Search / filter** — type in the search bar to filter by IP, MAC, hostname,
  vendor, or alias in real time
- **Click a row** to open a bandwidth history chart for that device
- **Alias** — hover a device name and click the pencil icon to set a friendly name

### Bandwidth history panel

Slides up from the bottom when you click a device row or the **WAN History** button.

- **Time window** — 1 h / 6 h / 24 h / 7 d selector
- **Peak and average** download / upload displayed in the header
- **CSV export** — download the history as a spreadsheet

### WAN traffic

Click **WAN History** (next to the "Network Throughput" chart title) to open the
same history panel for total WAN traffic.

### Recently Disconnected

Devices that joined the network and disconnected within 5 minutes appear in a
separate section with first seen, last seen, and duration. Useful for spotting
phones, scanners, or misconfigured devices.

---

## Bandwidth Throttling

When RouterOS is the active data source a **Throttle** column appears in the device
table. Use the dropdown to instantly cap any device's upload and download speed.

| Option | RouterOS queue value |
|--------|----------------------|
| Unlimited | queue removed |
| 5 Mbps | `5M/5M` |
| 30 Mbps | `30M/30M` |
| 500 Mbps | `500M/500M` |
| 1 Gbps | `1G/1G` |

Limits apply symmetrically to upload and download. MHS creates a RouterOS
**Simple Queue** named `nb-<ip>` for each throttled device. Limits persist on the
router and survive an app restart. Changes made in Winbox are reflected in the
dashboard within 30 s.

---

## Alerts

Configure `ALERT_WEBHOOK_URL` and/or `ALERT_NTFY_TOPIC` in `.env` to receive
notifications when:

- A **new device** joins the network for the first time
- An **aliased device** goes offline

### ntfy.sh example

```bash
# .env
ALERT_NTFY_TOPIC=my-home-network
```

Then subscribe on your phone with the [ntfy app](https://ntfy.sh) using topic
`my-home-network`. No account required.

### Generic webhook example

```bash
ALERT_WEBHOOK_URL=https://your-server.com/hooks/network
```

MHS will POST:

```json
{ "title": "New Device", "message": "raspberrypi (dc:a6:32:xx:xx:xx) joined the network", "source": "MHS" }
```

---

## Traffic Logging

MHS logs per-device bandwidth to a local SQLite database (`mhs.db`) every 30 s.

| Setting | Value |
|---------|-------|
| Retention | 7 days |
| Sample interval | 30 s |
| DB location | `mhs.db` in the project root |

---

## REST API

All endpoints return JSON. History endpoints accept a `?minutes=` query parameter
(clamped to max 10 080 = 7 days).

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Uptime, connection status, device count, DB size |
| `GET` | `/api/devices` | Full device list + bandwidth snapshot |
| `GET` | `/api/device/{ip\|mac}` | Look up a single device by IP or MAC |
| `GET` | `/api/history/wan?minutes=60` | WAN bandwidth history samples |
| `GET` | `/api/history/wan/export` | WAN history as CSV download (default 7 d) |
| `GET` | `/api/history/{ip}?minutes=60` | Per-device bandwidth history samples |
| `GET` | `/api/history/{ip}/export` | Device history as CSV download (default 7 d) |
| `GET` | `/api/transient?hours=24` | Devices online < 5 min in the last N hours |
| `GET` | `/api/aliases` | All user-set friendly names `{ip: alias}` |
| `PUT` | `/api/alias/{ip}` | Set alias — body: `{"alias": "My Phone"}` |
| `DELETE` | `/api/alias/{ip}` | Remove alias (reverts to router hostname) |
| `GET` | `/api/limits` | Active bandwidth limits `{ip: mbps}` |
| `POST` | `/api/limits/{ip}` | Set limit — body: `{"limit_mbps": 30}` (0 = unlimited) |
| `DELETE` | `/api/limits/{ip}` | Remove limit |
| `GET` | `/api/ports` | Router + switch port stats |
| `POST` | `/api/scan` | Trigger an immediate re-scan (10 s cooldown) |
| `WS` | `/ws` | Real-time push every 2 s |

---

## Data Source Priority

```
MikroTik RouterOS REST API  ←── used when router is reachable
   • DHCP leases  → authoritative hostnames
   • ARP table    → IP / MAC mapping
   • IP Accounting → exact per-device upload / download rates
   • /interface   → per-port traffic on router and switch

Local fallback  ←── when MikroTik is unreachable
   • nmap → scapy ARP → /proc/net/arp
   • psutil interface counters (total only)
```

MHS checks the router every 60 s and reconnects automatically when it comes
back online.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `fastapi` + `uvicorn` | Web server & WebSocket |
| `httpx` | Async HTTP for RouterOS REST API and alerts |
| `scapy` | Fallback ARP scanning |
| `psutil` | Fallback interface bandwidth stats |
| `python-nmap` | Fallback enhanced scanning |
| `python-dotenv` | Loads `.env` credentials |

Chart.js 4.4.0 is downloaded once by `install.sh` and served locally —
no CDN dependency at runtime.

---

## Project Layout

```
Network-Buddy/
├── app/
│   ├── main.py        # FastAPI app, REST endpoints, WebSocket push
│   ├── mikrotik.py    # RouterOS REST client + monitor
│   ├── scanner.py     # Local device discovery (fallback)
│   ├── bandwidth.py   # Local bandwidth monitor (fallback)
│   ├── db.py          # SQLite traffic log, session store, aliases
│   └── alerts.py      # Webhook + ntfy.sh alert dispatch
├── static/
│   ├── index.html     # Dashboard SPA
│   ├── style.css      # Dark theme
│   ├── app.js         # WebSocket client, charts, sort, aliases
│   └── chart.umd.js   # Chart.js 4.4.0 (downloaded by install.sh)
├── settings.py        # Config — reads .env
├── run.py             # Entry point
├── run.sh             # Convenience start script
├── install.sh         # Systemd service installer
├── uninstall.sh       # Service removal
├── .env.example       # Credential + config template
├── requirements.txt
└── mhs.db             # SQLite database (runtime, gitignored)
```
