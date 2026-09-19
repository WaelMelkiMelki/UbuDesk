#!/usr/bin/env bash
# Installs everything the UbuDesk server needs on Ubuntu 24.04+.
# Idempotent; run it again after OS upgrades.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Installing system packages (sudo required)…"
sudo apt-get update
sudo apt-get install -y \
    python3-venv python3-gi python3-gi-cairo \
    gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
    gstreamer1.0-pipewire pipewire \
    xdg-desktop-portal xdg-desktop-portal-gnome \
    android-tools-adb libnotify-bin

echo "==> Creating virtualenv (with system site packages for PyGObject)…"
if [ ! -d .venv ]; then
    python3 -m venv --system-site-packages .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"

echo "==> Done. Activate with:  source .venv/bin/activate"
echo "==> Then check your setup: ubudesk doctor"
