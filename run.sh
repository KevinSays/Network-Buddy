#!/usr/bin/env bash
# MHS: Mikrotik Homelab Scanner — quick-start script
set -e

VENV=".venv"

# Create virtualenv if needed
if [ ! -d "$VENV" ]; then
  echo "Creating virtual environment…"
  python3 -m venv "$VENV"
fi

source "$VENV/bin/activate"

# Install/upgrade deps quietly
pip install -q -r requirements.txt

echo ""
echo "╔═══════════════════════════════════════╗"
echo "║         MHS is starting…               ║"
echo "╠═══════════════════════════════════════╣"
echo "║  Dashboard → http://localhost:8000     ║"
echo "║                                        ║"
echo "║  For full features (ARP scan +         ║"
echo "║  per-device bandwidth) run with:       ║"
echo "║    sudo bash run.sh                    ║"
echo "╚═══════════════════════════════════════╝"
echo ""

python run.py
