#!/usr/bin/env bash
# Preserve command output/exit status and expose a useful error in GitHub's
# annotations API, even when the full log download is unavailable.
set -euo pipefail
if [[ $# -eq 0 ]]; then
    echo "Usage: $0 COMMAND [ARG ...]" >&2
    exit 2
fi
log=$(mktemp)
trap 'rm -f "$log"' EXIT
set +e
"$@" 2>&1 | tee "$log"
status=$?
set -e
if [[ "$status" -ne 0 ]]; then
    python3 - "$log" "$status" <<'PY'
import re
import sys
from pathlib import Path

lines = Path(sys.argv[1]).read_text(errors="replace").splitlines()
errors = [
    line for line in lines
    if re.search(r"(^e: |error:|Error:|Exception|FAIL(?:ED|URE)|^> (?!Task |Configure project ))", line)
]
details = errors[:30] + ["--- log tail ---"] + lines[-15:]
message = f"CI command exited with status {sys.argv[2]}:\n" + "\n".join(details)
message = message[:14000].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
print("::error::" + message)
PY
fi
exit "$status"
