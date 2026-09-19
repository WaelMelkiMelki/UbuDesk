#!/usr/bin/env bash
# Installs the systemd --user unit for UbuDesk (optional; you can always just
# run `ubudesk serve` in a terminal).
set -euo pipefail
SERVER_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -x "$SERVER_DIR/.venv/bin/ubudesk" ]; then
    echo "Run server/scripts/install-deps.sh first (no .venv/bin/ubudesk found)." >&2
    exit 1
fi

mkdir -p "$HOME/.local/share/ubudesk"
ln -sfn "$SERVER_DIR/.venv/bin" "$HOME/.local/share/ubudesk/venv-bin"

mkdir -p "$HOME/.config/systemd/user"
cp "$SERVER_DIR/packaging/ubudesk.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload

echo "Installed. Start with:   systemctl --user start ubudesk"
echo "Autostart on login:      systemctl --user enable ubudesk"
echo "Logs:                    journalctl --user -u ubudesk -f"
