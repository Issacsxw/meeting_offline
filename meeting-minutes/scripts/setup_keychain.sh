#!/bin/bash
set -euo pipefail

skill_scripts_dir="$(cd "$(dirname "$0")" && pwd)"
device_service="$(python3 "$skill_scripts_dir/device_context.py" keychain-service)"
service_name="${DASHSCOPE_KEYCHAIN_SERVICE:-$device_service}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This helper requires macOS Keychain." >&2
  exit 1
fi

read -r -s -p "Paste the DashScope API key (input hidden): " dashscope_key
printf '\n'

if [[ -z "$dashscope_key" ]]; then
  echo "No key entered; nothing changed." >&2
  exit 1
fi

/usr/bin/security add-generic-password \
  -U \
  -a "$USER" \
  -s "$service_name" \
  -w "$dashscope_key" >/dev/null

unset dashscope_key
echo "Saved to macOS Keychain service: $service_name"
