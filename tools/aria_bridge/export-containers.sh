#!/usr/bin/env bash
# Run on Unraid, optionally once per minute through User Scripts.
set -euo pipefail
umask 077

aria_data="${ARIA_APPDATA_ROOT:-/mnt/user/appdata/aria-gpt-bridge}"
for aria_command in docker jq timeout; do
    command -v "$aria_command" >/dev/null || { printf '%s is required.\n' "$aria_command" >&2; exit 1; }
done
mkdir -p "$aria_data/status"
chmod 755 "$aria_data/status"
aria_tmp="$(mktemp "$aria_data/status/.containers.XXXXXX")"
trap 'rm -f "$aria_tmp"' EXIT
timeout 20 docker ps -a --format '{"Names":[{{json .Names}}],"Image":{{json .Image}},"State":{{json .State}},"Status":{{json .Status}}}' |
    jq -s '{generated_at: (now | floor), containers: .}' > "$aria_tmp"
test "$(wc -c < "$aria_tmp")" -le 1048576 || { printf 'Container snapshot exceeds 1 MiB.\n' >&2; exit 1; }
chmod 644 "$aria_tmp"
mv -f "$aria_tmp" "$aria_data/status/containers.json"
