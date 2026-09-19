#!/usr/bin/env bash
# Installs the GitHub Actions workflows.
#
# Why this script exists: the automation account that created this branch
# does not have the GitHub `workflows` permission, so it cannot push files
# under .github/workflows/ itself. Run this once from your own account:
#
#   ./scripts/enable-ci.sh
#   git add .github/workflows
#   git commit -m "ci: enable GitHub Actions workflows"
#   git push
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .github/workflows
cp ci/workflows/*.yml .github/workflows/
echo "Copied $(ls ci/workflows/*.yml | wc -l) workflow(s) to .github/workflows/."
echo "Now commit and push them (see the comment at the top of this script)."
