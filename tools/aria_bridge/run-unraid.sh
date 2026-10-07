#!/usr/bin/env bash
# First installation. Credentials are entered locally and never put in argv.
set -euo pipefail
umask 077

aria_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
aria_data="${ARIA_APPDATA_ROOT:-/mnt/user/appdata/aria-gpt-bridge}"
aria_tunnel="${1:-}"
if [[ ! "$aria_tunnel" =~ ^tunnel_[A-Za-z0-9_-]{8,100}$ ]]; then
    printf 'Usage: bash run-unraid.sh tunnel_YOUR_ID\n' >&2
    exit 1
fi
if [[ $EUID -ne 0 || ! -f /etc/unraid-version ]]; then
    printf 'Run this installer in the Unraid host terminal as root.\n' >&2
    exit 1
fi
if [[ "$aria_data" != /* || "$aria_data" == *','* || "$aria_data" == *$'\n'* ]]; then
    printf 'ARIA_APPDATA_ROOT must be an absolute path without commas or newlines.\n' >&2
    exit 1
fi
for aria_command in docker jq timeout; do
    command -v "$aria_command" >/dev/null || { printf '%s is required.\n' "$aria_command" >&2; exit 1; }
done
if docker container inspect aria-gpt-bridge >/dev/null 2>&1; then
    printf 'aria-gpt-bridge already exists. Follow the upgrade steps in README.md.\n' >&2
    exit 1
fi
mkdir -p "$aria_data/secrets" "$aria_data/status"
chmod 700 "$aria_data" "$aria_data/secrets"
chmod 755 "$aria_data/status"

aria_save_secret() {
    local aria_name="$1" aria_prompt="$2" aria_secret
    if [[ ! -s "$aria_data/secrets/$aria_name" ]]; then
        read -r -s -p "$aria_prompt: " aria_secret < /dev/tty
        printf '\n' > /dev/tty
        if [[ -z "$aria_secret" || ( "$aria_name" == red_update_token && ! "$aria_secret" =~ ^[0-9a-f]{64}$ ) ]]; then
            printf 'Invalid credential; no value saved.\n' >&2
            exit 1
        fi
        printf '%s\n' "$aria_secret" > "$aria_data/secrets/$aria_name"
        unset aria_secret
    fi
    chown 65532:65532 "$aria_data/secrets/$aria_name"
    chmod 400 "$aria_data/secrets/$aria_name"
}
aria_save_secret control-plane-api-key 'OpenAI tunnel runtime API key'
aria_save_secret red_update_token '64-character token after #token= in Red private update link'
ARIA_APPDATA_ROOT="$aria_data" bash "$aria_source/export-containers.sh"
docker build --pull -t aria-gpt-bridge:v1 "$aria_source"
docker run -d --name aria-gpt-bridge \
    --init --restart unless-stopped --read-only --user 65532:65532 \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --pids-limit 64 --memory 256m --cpus 1 \
    --log-driver json-file --log-opt max-size=5m --log-opt max-file=2 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777 \
    --env "CONTROL_PLANE_TUNNEL_ID=$aria_tunnel" \
    --env "ARIA_RED_URL=${ARIA_RED_URL:-http://10.10.1.200:8766}" \
    --env ARIA_DOCKER_SNAPSHOT_FILE=/status/containers.json \
    --mount "type=bind,src=$aria_data/secrets/control-plane-api-key,dst=/run/secrets/control-plane-api-key,readonly" \
    --mount "type=bind,src=$aria_data/secrets/red_update_token,dst=/run/secrets/red_update_token,readonly" \
    --mount "type=bind,src=$aria_data/status,dst=/status,readonly" \
    --mount type=bind,src=/proc/uptime,dst=/host/proc/uptime,readonly \
    --mount type=bind,src=/proc/loadavg,dst=/host/proc/loadavg,readonly \
    --mount type=bind,src=/proc/meminfo,dst=/host/proc/meminfo,readonly \
    --mount type=bind,src=/etc/unraid-version,dst=/host/etc/unraid-version,readonly \
    aria-gpt-bridge:v1
printf 'Container started. Check docker logs aria-gpt-bridge and its health before connecting ChatGPT.\n'
