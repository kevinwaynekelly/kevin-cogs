#!/usr/bin/env bash
# Preserve the actual tunnel, endpoint, file credentials and resource limits.
set -euo pipefail
umask 077
aria_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
aria_data="${ARIA_APPDATA_ROOT:-/mnt/user/appdata/aria-gpt-bridge}"
source "$aria_source/unraid-common.sh"
aria_preflight
timeout 20 docker info >/dev/null

# Project a fixed allowlist immediately. Never print or persist full inspect data.
aria_config="$(docker inspect aria-gpt-bridge | jq -ce '
    .[0] | {
      tunnel: ([.Config.Env[]? | select(startswith("CONTROL_PLANE_TUNNEL_ID=")) | ltrimstr("CONTROL_PLANE_TUNNEL_ID=")][0] // ""),
      red_url: ([.Config.Env[]? | select(startswith("ARIA_RED_URL=")) | ltrimstr("ARIA_RED_URL=")][0] // "http://10.10.1.200:8766"),
      key_file: ([.Mounts[]? | select(.Type == "bind" and .Destination == "/run/secrets/control-plane-api-key") | .Source][0] // ""),
      red_file: ([.Mounts[]? | select(.Type == "bind" and .Destination == "/run/secrets/red_update_token") | .Source][0] // ""),
      status_dir: ([.Mounts[]? | select(.Type == "bind" and .Destination == "/status") | .Source][0] // ""),
      running: .State.Running,
      memory: (.HostConfig.Memory // 0), memory_swap: (.HostConfig.MemorySwap // 0),
      pids: (.HostConfig.PidsLimit // 0), nano_cpus: (.HostConfig.NanoCpus // 0),
      cpu_period: (.HostConfig.CpuPeriod // 0), cpu_quota: (.HostConfig.CpuQuota // 0),
      cpu_shares: (.HostConfig.CpuShares // 0), cpuset: (.HostConfig.CpusetCpus // "")
    }')"
aria_tunnel="$(jq -r .tunnel <<< "$aria_config")"
aria_red_url="$(jq -r .red_url <<< "$aria_config")"
aria_key_file="$(jq -r .key_file <<< "$aria_config")"
aria_red_token_file="$(jq -r .red_file <<< "$aria_config")"
aria_status_dir="$(jq -r .status_dir <<< "$aria_config")"
aria_was_running="$(jq -r .running <<< "$aria_config")"
[[ "$aria_tunnel" =~ ^tunnel_[A-Za-z0-9_-]{8,100}$ ]] || { aria_error 'Existing tunnel ID is missing or invalid; the old bridge is unchanged.'; exit 1; }
[[ "$aria_red_url" == http://* || "$aria_red_url" == https://* ]] || { aria_error 'Existing Red endpoint is invalid.'; exit 1; }
for aria_path in "$aria_key_file" "$aria_red_token_file" "$aria_status_dir"; do
    aria_valid_path "$aria_path" || { aria_error 'An existing mount path is invalid or missing.'; exit 1; }
done
[[ -s "$aria_key_file" && -s "$aria_red_token_file" && -d "$aria_status_dir" ]] || { aria_error 'An existing credential file or status directory is unavailable.'; exit 1; }
if [[ -z "${ARIA_APPDATA_ROOT:-}" ]]; then
    aria_data="$(dirname -- "$aria_status_dir")"
    aria_preflight
fi
aria_install_lock
aria_resource_flags=()
for aria_pair in 'memory --memory' 'memory_swap --memory-swap' 'pids --pids-limit' 'cpu_shares --cpu-shares'; do
    read -r aria_field aria_flag <<< "$aria_pair"
    aria_value="$(jq -r --arg field "$aria_field" '.[$field]' <<< "$aria_config")"
    if [[ "$aria_value" != 0 ]]; then aria_resource_flags+=("$aria_flag" "$aria_value"); fi
done
aria_value="$(jq -r .nano_cpus <<< "$aria_config")"
if [[ "$aria_value" != 0 ]]; then
    aria_resource_flags+=(--cpus "$(jq -r '.nano_cpus / 1000000000' <<< "$aria_config")")
else
    for aria_field in cpu_period cpu_quota; do
        aria_value="$(jq -r --arg field "$aria_field" '.[$field]' <<< "$aria_config")"
        if [[ "$aria_value" != 0 ]]; then aria_resource_flags+=("--${aria_field//_/-}" "$aria_value"); fi
    done
fi
aria_value="$(jq -r .cpuset <<< "$aria_config")"
if [[ -n "$aria_value" ]]; then aria_resource_flags+=(--cpuset-cpus "$aria_value"); fi
unset aria_config

# This expensive operation completes before stopping any running service.
docker build --pull -t aria-gpt-bridge:v1 "$aria_source"
ARIA_APPDATA_ROOT="$aria_data" bash "$aria_source/host-service.sh" restart 7>&-
ARIA_APPDATA_ROOT="$aria_data" bash "$aria_source/host-service.sh" install-boot 7>&-

aria_backup="aria-gpt-bridge-rollback-$(date +%s)-$$"
aria_old_stopped=false
aria_old_renamed=false
aria_new_created=false
aria_new_id=''
aria_completed=false
aria_rollback() {
    local aria_exit=$? aria_current_id='' aria_current_update='' aria_current_info='' aria_owned=false
    trap - EXIT INT TERM
    if [[ "$aria_completed" != true && "$aria_old_stopped" == true ]]; then
        printf 'Upgrade failed; restoring the previous bridge container.\n' >&2
        if [[ "$aria_old_renamed" == true ]] && docker container inspect "$aria_backup" >/dev/null 2>&1; then
            if [[ "$aria_new_created" == true ]] || docker container inspect aria-gpt-bridge >/dev/null 2>&1; then
                aria_current_info="$(docker inspect --format '{{.Id}} {{index .Config.Labels "com.aria-gpt-bridge.update-id"}}' aria-gpt-bridge 2>/dev/null)" || true
                read -r aria_current_id aria_current_update <<< "$aria_current_info" || true
                if [[ "$aria_current_id" =~ ^[a-f0-9]{64}$ && "$aria_current_id" == "$aria_new_id" ]]; then aria_owned=true; fi
                if [[ "$aria_current_id" =~ ^[a-f0-9]{64}$ && -n "${ARIA_BRIDGE_UPDATE_ID:-}" && "$aria_current_update" == "$ARIA_BRIDGE_UPDATE_ID" ]]; then aria_owned=true; fi
                if [[ "$aria_owned" != true ]]; then
                    aria_error "A different bridge container now owns the name; it was preserved. The original is retained as $aria_backup."
                    exit "$aria_exit"
                fi
                aria_capture_upgrade_failure "$aria_current_id" || true
                docker rm -f "$aria_current_id" >/dev/null 2>&1 || true
            fi
            if ! docker rename "$aria_backup" aria-gpt-bridge; then aria_error "Restore the retained container named $aria_backup manually."; exit "$aria_exit"; fi
        fi
        if [[ "$aria_was_running" == true ]]; then docker start aria-gpt-bridge >/dev/null || aria_error 'The previous bridge could not be restarted; its container has been retained.'; fi
    fi
    exit "$aria_exit"
}
trap aria_rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Mark before stop so an interrupted stop is also recovered.
aria_old_stopped=true
docker stop --time 20 aria-gpt-bridge >/dev/null
aria_old_renamed=true
docker rename aria-gpt-bridge "$aria_backup"
aria_new_id="$(aria_run_container)"
aria_new_created=true
aria_wait_healthy
aria_completed=true
aria_record_installed_revision
if ! docker rm "$aria_backup" >/dev/null; then
    # A healthy replacement is committed. Failure to remove a stopped backup
    # must not make the detached updater roll back a successful installation.
    if docker update --restart=no "$aria_backup" >/dev/null; then
        printf 'The healthy bridge is installed. Stopped backup retained with restart disabled: %s\n' "$aria_backup" >&2
    else
        printf 'The healthy bridge is installed. Inspect retained backup %s; its restart policy could not be disabled.\n' "$aria_backup" >&2
    fi
fi
printf 'Aria bridge is healthy. Tunnel settings and credentials were preserved; host management is enabled.\n'
