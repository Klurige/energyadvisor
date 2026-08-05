#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$SCRIPT_DIR/vattenfall_local_server.py" \
  --log-level "${LOG_LEVEL:-INFO}" \
  "$@"
