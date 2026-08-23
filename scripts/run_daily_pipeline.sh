#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"
source "$REPO_ROOT/.venv/bin/activate"

python "$REPO_ROOT/scripts/update_research_pipeline.py" \
  --log-file "$REPO_ROOT/logs/daily_pipeline.log"
