#!/usr/bin/env bash
# Foreground, single-instance paper scheduler. Use nohup on an always-running host.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p reports/_state
exec flock -n reports/_state/paper-scheduler.lock \
  .venv/bin/python -u -m sequoia_x.reporting.simulation daemon \
  --root reports --valuations valuations.local.json --send "$@"
