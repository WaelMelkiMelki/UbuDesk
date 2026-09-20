#!/usr/bin/env bash
# Keep the maintained CI definitions and installed GitHub workflows in sync.
# Default: install test/build workflows only, not the tag-triggered release publisher.
# --check: fail if the checked-in copies drift (also run by CI).
# --include-release: explicitly opt into the separate release publisher template.
set -euo pipefail
cd "$(dirname "$0")/.."

case "${1:-}" in
    ""|--check|--include-release) ;;
    *) echo "Usage: $0 [--check|--include-release]" >&2; exit 2 ;;
esac

if [[ "${1:-}" == --check ]]; then
    for workflow in android-ci server-ci; do
        if ! cmp -s "ci/workflows/$workflow.yml" ".github/workflows/$workflow.yml"; then
            echo "$workflow is missing or out of sync. Run ./scripts/enable-ci.sh." >&2
            exit 1
        fi
    done
    echo "CI workflow copies match."
    exit 0
fi

mkdir -p .github/workflows
for workflow in android-ci server-ci; do
    cp "ci/workflows/$workflow.yml" .github/workflows/
done
if [[ "${1:-}" == --include-release ]]; then
    cp ci/workflows/release.yml .github/workflows/
fi
echo "CI workflows installed under .github/workflows/. Commit and push to run them."
