#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"
source "$REPO_ROOT/.venv/bin/activate"

# Cron should set PIPELINE_TRIGGER=cron; direct invocations are recorded as manual.
export PIPELINE_TRIGGER="${PIPELINE_TRIGGER:-manual}"

python "$REPO_ROOT/scripts/update_research_pipeline.py" \
  --log-file "$REPO_ROOT/logs/daily_pipeline.log"
