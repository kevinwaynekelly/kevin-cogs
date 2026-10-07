#!/usr/bin/env bash
# Shared host-only installation helpers. Never source credentials as shell code.

aria_error() { printf '%s\n' "$*" >&2; return 1; }

aria_valid_path() {
    [[ "$1" == /* && "$1" != *','* && "$1" != *$'\n'* && "$1" != *$'\r'* ]]
}

aria_install_lock() {
    mkdir -p "$aria_data"
    chmod 700 "$aria_data"
    [[ ! -L "$aria_data/install.lock" ]] || { aria_error 'The installer lock must not be a symlink.'; return 1; }
    if [[ "${ARIA_INHERITED_INSTALL_LOCK:-}" == 1 ]]; then
        # The repository migration keeps the installer lock across its child
        # upgrade. Only the exact already-open lock inode may be reused.
        [[ /proc/self/fd/7 -ef "$aria_data/install.lock" ]] || { aria_error 'The inherited Aria installer lock is invalid.'; return 1; }
        flock -n 7 || { aria_error 'The inherited Aria installer lock is unavailable.'; return 1; }
    else
        exec 7> "$aria_data/install.lock"
        flock -n 7 || { aria_error 'Another Aria installation or upgrade is already running.'; return 1; }
    fi
    if [[ -f "$aria_data/management/bridge-update-runner.lock" ]] && ! flock -n "$aria_data/management/bridge-update-runner.lock" true; then
        # Only the detached updater's current child installer may pass its
        # liveness lock. A manual installer must not race source replacement.
        if [[ ! "${ARIA_BRIDGE_UPDATE_ID:-}" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] ||
            ! jq -e --arg id "$ARIA_BRIDGE_UPDATE_ID" '.job_id == $id and .action == "bridge_update" and .status == "running"' "$aria_data/management/bridge-updates/$ARIA_BRIDGE_UPDATE_ID.json" >/dev/null 2>&1 ||
            ! jq -e --arg id "$ARIA_BRIDGE_UPDATE_ID" '.job_id == $id' "$aria_data/management/bridge-update-latest.json" >/dev/null 2>&1; then
            aria_error 'A remote bridge update is running; wait for its final status before installing manually.'
            return 1
        fi
    fi
}

aria_preflight() {
    [[ $EUID -eq 0 && -f /etc/unraid-version ]] || {
        aria_error 'Run this command in the Unraid host terminal as root.'; return 1;
    }
    local aria_command
    for aria_command in docker jq timeout php bash flock stat tail setsid awk cat git; do
        command -v "$aria_command" >/dev/null || { aria_error "$aria_command is required; nothing has been stopped."; return 1; }
    done
    php -r 'exit(PHP_VERSION_ID >= 70400 && function_exists("simplexml_load_string") && function_exists("proc_open") && function_exists("posix_kill") && function_exists("stream_socket_server") && function_exists("flock") ? 0 : 1);' || {
        aria_error 'Native PHP 7.4+ with SimpleXML, POSIX, proc_open, flock and Unix sockets is required.'; return 1;
    }
    [[ -f "$aria_source/host-agent.php" && -f "$aria_source/bridge-update.php" && -f /usr/local/emhttp/plugins/dynamix.docker.manager/include/DockerClient.php ]] || {
        aria_error 'The host agent, bridge updater or native Unraid Docker template converter is missing.'; return 1;
    }
    php -l "$aria_source/host-agent.php" >/dev/null || return 1
    php -l "$aria_source/bridge-update.php" >/dev/null || return 1
    [[ -f "$aria_source/github-auth.php" ]] && php -l "$aria_source/github-auth.php" >/dev/null || { aria_error 'The GitHub authentication helper is missing or invalid.'; return 1; }
    local aria_module
    for aria_module in extensions operations diagnostics applications deployment automation; do
        [[ -f "$aria_source/host-$aria_module.php" ]] || { aria_error "Missing host module: $aria_module"; return 1; }
        php -l "$aria_source/host-$aria_module.php" >/dev/null || return 1
        if [[ "$aria_module" != extensions ]]; then
            jq -e 'type == "array" and length > 0' "$aria_source/$aria_module-tools.json" >/dev/null || return 1
        fi
    done
    [[ -f "$aria_source/automation-service.sh" ]] || { aria_error 'The automation service launcher is missing.'; return 1; }
    aria_valid_path "$aria_data" || { aria_error 'Use an absolute appdata path without commas or newlines.'; return 1; }
    case "$aria_source" in /mnt/*|/boot/*) ;; *) aria_error 'Keep the bridge checkout on persistent Unraid storage under /mnt or /boot.'; return 1;; esac
    [[ ! -L "$aria_data" && ! -L "$aria_data/management" && ! -L /var/run/aria-gpt-bridge ]] || {
        aria_error 'Appdata, management and runtime directories must not be symlinks.'; return 1;
    }
}

aria_run_container() {
    local -a aria_update_labels=()
    local aria_revision
    aria_revision="$(git -C "$aria_source" rev-parse HEAD 2>/dev/null || true)"
    if [[ "$aria_revision" =~ ^[a-f0-9]{40}$ ]]; then
        aria_update_labels+=(--label "com.aria-gpt-bridge.source-revision=$aria_revision")
    fi
    if [[ -n "${ARIA_BRIDGE_UPDATE_ID:-}" ]]; then
        [[ "$ARIA_BRIDGE_UPDATE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] || { aria_error 'Invalid internal bridge update ID.'; return 1; }
        aria_update_labels+=(--label "com.aria-gpt-bridge.update-id=$ARIA_BRIDGE_UPDATE_ID")
    fi
    docker run -d --name aria-gpt-bridge \
        --init --restart unless-stopped --read-only --user 65532:65532 \
        --cap-drop ALL --security-opt no-new-privileges:true \
        "${aria_resource_flags[@]}" \
        "${aria_update_labels[@]}" \
        --log-driver json-file --log-opt max-size=5m --log-opt max-file=2 \
        --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777 \
        --env "CONTROL_PLANE_TUNNEL_ID=$aria_tunnel" \
        --env "ARIA_RED_URL=$aria_red_url" \
        --env ARIA_DOCKER_SNAPSHOT_FILE=/status/containers.json \
        --env ARIA_AGENT_SOCKET=/run/aria-agent/agent.sock \
        --mount "type=bind,src=$aria_key_file,dst=/run/secrets/control-plane-api-key,readonly" \
        --mount "type=bind,src=$aria_red_token_file,dst=/run/secrets/red_update_token,readonly" \
        --mount "type=bind,src=$aria_status_dir,dst=/status,readonly" \
        --volume /var/run/aria-gpt-bridge:/run/aria-agent:ro \
        --mount type=bind,src=/proc/uptime,dst=/host/proc/uptime,readonly \
        --mount type=bind,src=/proc/loadavg,dst=/host/proc/loadavg,readonly \
        --mount type=bind,src=/proc/meminfo,dst=/host/proc/meminfo,readonly \
        --mount type=bind,src=/etc/unraid-version,dst=/host/etc/unraid-version,readonly \
        aria-gpt-bridge:v1
}

aria_record_installed_revision() {
    local aria_revision
    aria_revision="$(git -C "$aria_source" rev-parse HEAD 2>/dev/null || true)"
    [[ "$aria_revision" =~ ^[a-f0-9]{40}$ ]] || return 0
    mkdir -p "$aria_data/management"
    jq -n --arg revision "$aria_revision" --arg installed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        '{revision:$revision,installed_at:$installed_at}' > "$aria_data/management/.installed-revision.tmp"
    chmod 600 "$aria_data/management/.installed-revision.tmp"
    mv -f "$aria_data/management/.installed-revision.tmp" "$aria_data/management/installed-revision.json"
}

aria_wait_healthy() {
    local aria_attempt aria_state
    for ((aria_attempt=0; aria_attempt<100; aria_attempt++)); do
        aria_state="$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' aria-gpt-bridge 2>/dev/null)" || { aria_error 'Could not inspect bridge startup state.'; return 1; }
        [[ "$aria_state" == 'running healthy' ]] && return 0
        if [[ "$aria_state" == exited* || "$aria_state" == dead* || "$aria_state" == *unhealthy ]]; then
            aria_error 'The replacement bridge exited or failed its health check.'; return 1
        fi
        sleep 1
    done
    aria_error 'Bridge did not become healthy within 100 seconds.'
}

aria_capture_upgrade_failure() {
    # Only call for the replacement container owned by this installer. Logs may
    # contain provider data: keep them root-private and never echo their contents.
    local aria_failed_id="$1" aria_failure_log
    mkdir -p "$aria_data/management" || return 0
    aria_failure_log="$(mktemp "$aria_data/management/upgrade-failure.XXXXXXXX.log")" || return 0
    chmod 600 "$aria_failure_log" || return 0
    {
        printf 'Replacement container: %s\n' "$aria_failed_id"
        timeout 5 docker inspect --format '{{json .State}}' "$aria_failed_id" || true
        timeout 5 docker logs --tail 80 "$aria_failed_id" 2>&1 | head -c 32768 || true
    } > "$aria_failure_log" 2>&1
    printf 'Startup diagnostics saved locally: %s\n' "$aria_failure_log" >&2
}
