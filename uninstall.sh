#!/usr/bin/env bash
# uninstall.sh — remove the MHS systemd service
set -euo pipefail

SERVICE_NAME="mhs"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "error: run with sudo:  sudo bash uninstall.sh"
  exit 1
fi

echo ""
echo "Stopping and disabling MHS service…"
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}" 2>/dev/null || true

if [[ -f "${SERVICE_FILE}" ]]; then
  rm "${SERVICE_FILE}"
  echo "Removed ${SERVICE_FILE}"
else
  echo "(service file not found — already removed?)"
fi

systemctl daemon-reload

echo ""
read -rp "Remove Python virtual environment (.venv)? [y/N]: " REMOVE_VENV
if [[ "${REMOVE_VENV,,}" == "y" ]]; then
  rm -rf "${INSTALL_DIR}/.venv"
  echo "Removed .venv"
fi

echo ""
echo "MHS has been uninstalled."
echo "Your .env and source files were kept. Remove them manually if no longer needed."
