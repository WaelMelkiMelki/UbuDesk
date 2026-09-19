#!/usr/bin/env bash
# Development helper: run the server with the test-pattern source, no TLS,
# loopback only. Safe on any machine, no desktop session needed.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/ubudesk serve --source test --no-tls --bind 127.0.0.1 --pair --log-level debug "$@"
