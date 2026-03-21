# Network Buddy

A home network scanner and bandwidth monitor — web dashboard served locally,
accessible from any browser on your network.

## Features

- Discovers all connected devices (IP, MAC, hostname, vendor)
- Real-time per-device bandwidth (upload / download rates)
- Interface-level total throughput chart (2-minute rolling window)
- Auto-rescans every 30 s; manual "Scan Now" button
- Live updates via WebSocket (refreshes every 2 s)
- Scan strategy: **nmap → ARP → ARP cache** (auto-selects best available)

## Quick Start

```bash
# Basic run (device discovery only — no root needed)
bash run.sh

# Full features: ARP scanning + per-device bandwidth via packet capture
sudo bash run.sh
```

Then open **http://localhost:8000** in your browser.
To access from another device on your network use your machine's IP, e.g. `http://192.168.1.x:8000`.

## Manual Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# run (add sudo for ARP scan + packet sniffing)
python run.py
```

## Dependencies

| Package | Purpose |
|---------|---------|
| `fastapi` + `uvicorn` | Web server & WebSocket |
| `scapy` | ARP scanning & packet capture |
| `psutil` | Interface-level bandwidth stats |
| `python-nmap` | Optional enhanced scanning |

## How Bandwidth Is Measured

- **Total throughput** (the chart): read directly from the OS network counters
  via `psutil` — always accurate for your machine's interface.
- **Per-device rates**: captured via packet sniffing (scapy). On a typical home
  switch only traffic visible to the scanner machine is counted (traffic
  to/from the scanner, plus broadcast/multicast). For full per-device stats,
  run the app on your router or a device with network tap access.

## Project Layout

```
Network-Buddy/
├── app/
│   ├── main.py        # FastAPI app, WebSocket, background tasks
│   ├── scanner.py     # Device discovery (nmap / ARP / cache)
│   └── bandwidth.py   # Throughput monitor (psutil + scapy)
├── static/
│   ├── index.html     # Dashboard
│   ├── style.css      # Dark theme styles
│   └── app.js         # WebSocket client + Chart.js
├── run.py             # Entry point
├── run.sh             # Convenience start script
└── requirements.txt
```
