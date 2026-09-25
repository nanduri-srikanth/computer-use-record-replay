#!/usr/bin/env bash
# Runs one command with ANTHROPIC_API_KEY loaded from the Keychain, for that process only.
# Usage: scripts/with_api_key.sh <command> [args...]
set -euo pipefail
ANTHROPIC_API_KEY="$(security find-generic-password -a "$USER" -s cua-anthropic-api-key -w)" \
  || { echo "No key in Keychain. Run scripts/store_api_key.sh first." >&2; exit 1; }
export ANTHROPIC_API_KEY
exec "$@"
