#!/usr/bin/env bash
# Stores the OpenAI API key (used only for the walkthrough voiceover) in the macOS login Keychain.
#
# The key is read with `read -s` (hidden, never echoed, never in shell history) and handed to
# `security -i` on stdin, so it never appears as a process argument in `ps` and is never written
# to disk. (`security add-generic-password -w` with no value would prompt by itself, but that
# prompt silently truncates input at 128 characters, and OpenAI project keys are longer.)
set -euo pipefail
SERVICE="cua-openai-api-key"

read -rsp "Paste your OpenAI API key (input is hidden), then press Enter: " key; echo
read -rsp "Paste it again to confirm, then press Enter: " again; echo
[[ "$key" == "$again" ]] || { echo "The two entries differ. Nothing was stored." >&2; exit 1; }
[[ "$key" =~ ^sk-[A-Za-z0-9_-]+$ ]] || { echo "That does not look like an OpenAI key (sk-...). Nothing was stored." >&2; exit 1; }

printf 'add-generic-password -U -a "%s" -s "%s" -w "%s"\n' "$USER" "$SERVICE" "$key" | security -i >/dev/null
stored="$(security find-generic-password -a "$USER" -s "$SERVICE" -w)"
if [[ "$stored" == "$key" ]]; then
  echo "Stored in Keychain as '$SERVICE' (${#key} characters)."
else
  echo "Storing failed: the Keychain holds a different value. Re-run this script." >&2
  exit 1
fi
unset key again stored
