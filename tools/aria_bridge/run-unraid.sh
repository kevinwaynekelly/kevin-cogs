#!/usr/bin/env bash
# First installation, including the native host management service.
set -euo pipefail
umask 077

aria_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
aria_data="${ARIA_APPDATA_ROOT:-/mnt/user/appdata/aria-gpt-bridge}"
source "$aria_source/unraid-common.sh"
aria_tunnel="${1:-}"
if [[ ! "$aria_tunnel" =~ ^tunnel_[A-Za-z0-9_-]{8,100}$ ]]; then
    printf 'Usage: bash run-unraid.sh tunnel_YOUR_ID\n' >&2
    exit 1
fi
aria_preflight
timeout 20 docker info >/dev/null
aria_install_lock
if docker container inspect aria-gpt-bridge >/dev/null 2>&1; then
    printf 'aria-gpt-bridge already exists. Run bash tools/aria_bridge/upgrade-unraid.sh instead.\n' >&2
    exit 1
fi
docker build --pull -t aria-gpt-bridge:v1 "$aria_source"
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
ARIA_APPDATA_ROOT="$aria_data" bash "$aria_source/host-service.sh" start 7>&-
ARIA_APPDATA_ROOT="$aria_data" bash "$aria_source/host-service.sh" install-boot 7>&-
aria_red_url="${ARIA_RED_URL:-http://10.10.1.200:8766}"
aria_key_file="$aria_data/secrets/control-plane-api-key"
aria_red_token_file="$aria_data/secrets/red_update_token"
aria_status_dir="$aria_data/status"
aria_resource_flags=(--pids-limit 64 --memory 256m --cpus 1)
aria_run_container
aria_wait_healthy || { aria_error 'Bridge startup failed. Inspect docker logs aria-gpt-bridge locally.'; exit 1; }
aria_record_installed_revision
printf 'Aria bridge is healthy; host management is enabled and will start after reboot.\n'
