#!/bin/bash
# Store the DashScope API key in the macOS Keychain under this device's service name.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "这个脚本只用于 macOS 钥匙串。其他系统请设置环境变量 DASHSCOPE_API_KEY。" >&2
  exit 1
fi

skill_scripts_dir="$(cd "$(dirname "$0")" && pwd)"
device_service="$(python3 "$skill_scripts_dir/device_context.py" keychain-service)"
service_name="${DASHSCOPE_KEYCHAIN_SERVICE:-$device_service}"

echo "接下来会提示你粘贴阿里云 DashScope API Key 两次，输入内容不会显示。"
# `-w` last with no value makes `security` prompt for the key itself, so the key never
# appears on a command line where other processes could see it.
/usr/bin/security add-generic-password -U -a "$USER" -s "$service_name" -w

echo "已保存到 macOS 钥匙串（服务名：$service_name）。"
