#!/bin/bash
# Nightly wrapper for autoruns_velociraptor.py. Intended to be run from cron.
# Uses the repo folder it lives in, the repo's virtual environment, and
# appends output to nightly.log.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1
{
  echo "=== run started $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  "$DIR/.venv/bin/python3" autoruns_velociraptor.py \
    --api-config "$DIR/api.config.yaml" \
    --db "$DIR/autoruns.sqlite" \
    --out-dir "$DIR/reports"
  echo "=== exit code $? ==="
} >> "$DIR/nightly.log" 2>&1
