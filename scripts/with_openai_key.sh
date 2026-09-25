#!/usr/bin/env bash
# Runs one command with OPENAI_API_KEY loaded from the Keychain, for that process only.
# Usage: scripts/with_openai_key.sh <command> [args...]
set -euo pipefail
OPENAI_API_KEY="$(security find-generic-password -a "$USER" -s cua-openai-api-key -w)" \
  || { echo "No key in Keychain. Run scripts/store_openai_key.sh first." >&2; exit 1; }
export OPENAI_API_KEY
exec "$@"
