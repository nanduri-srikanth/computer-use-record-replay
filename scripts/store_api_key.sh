#!/usr/bin/env bash
# Stores the Anthropic API key in the macOS login Keychain.
# `security ... -w` with no value prompts for the key itself, so it is never echoed,
# never in shell history, never on disk, and never visible in `ps` as an argument.
set -euo pipefail
SERVICE="cua-anthropic-api-key"

echo "Paste your Anthropic API key at the prompt (input is hidden), then paste it again to confirm."
security add-generic-password -U -a "$USER" -s "$SERVICE" -w

if security find-generic-password -a "$USER" -s "$SERVICE" -w | grep -q '^sk-ant-'; then
  echo "Stored in Keychain as '$SERVICE'."
else
  echo "Stored, but it does not start with sk-ant-. Re-run this script if that was a mistake." >&2
  exit 1
fi
