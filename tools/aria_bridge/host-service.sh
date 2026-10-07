#!/usr/bin/env bash
# Native root service. Only the local Unix socket is exposed to the bridge.
set -euo pipefail
umask 077
aria_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
aria_data="${ARIA_APPDATA_ROOT:-/mnt/user/appdata/aria-gpt-bridge}"
aria_runtime=/var/run/aria-gpt-bridge
aria_state="$aria_data/management"
source "$aria_source/unraid-common.sh"

aria_pid_start() { local aria_stat; aria_stat="$(cat "/proc/$1/stat" 2>/dev/null)" || return 1; aria_stat="${aria_stat##*) }"; awk '{print $20}' <<< "$aria_stat"; }
aria_alive() {
    local aria_pid aria_start aria_current
    [[ -f "$aria_state/$1.pid" && ! -L "$aria_state/$1.pid" ]] || return 1
    read -r aria_pid aria_start < "$aria_state/$1.pid" || return 1
    [[ "$aria_pid" =~ ^[0-9]+$ && "$aria_start" =~ ^[0-9]+$ ]] || return 1
    aria_current="$(aria_pid_start "$aria_pid")" || return 1
    [[ "$aria_start" == "$aria_current" ]] && kill -0 "$aria_pid" 2>/dev/null
}

aria_stop_role() {
    local aria_role="$1" aria_pid aria_start aria_attempt
    if aria_alive "$aria_role"; then
        read -r aria_pid aria_start < "$aria_state/$aria_role.pid"
        kill -TERM "$aria_pid"
        for ((aria_attempt=0; aria_attempt<15; aria_attempt++)); do
            aria_alive "$aria_role" || break
            sleep 1
        done
        if aria_alive "$aria_role"; then aria_error "Cannot stop $aria_role supervisor cleanly; refusing to create a duplicate."; return 1; fi
    fi
    # Older supervisors let their background wait inherit this lock. Wait for
    # that child and for the supervisor's final exit before launching a replacement.
    if ! flock -w 5 "$aria_state/$aria_role.supervisor.lock" true; then
        aria_error "The $aria_role supervisor lock is still held; refusing to create a duplicate."
        return 1
    fi
    rm -f "$aria_state/$aria_role.pid"
}

aria_rotate() {
    local aria_log="$1"
    if [[ -f "$aria_log" ]] && (( $(stat -c %s "$aria_log") > 2097152 )); then
        tail -c 1048576 "$aria_log" > "$aria_log.previous"
        : > "$aria_log"
    fi
}

aria_ready() {
    aria_alive serve && aria_alive work && [[ -S "$aria_runtime/agent.sock" && -f "$aria_state/worker.lock" ]] || return 1
    # A live worker owns this lock. Supervisors alone are not readiness evidence.
    if flock -n "$aria_state/worker.lock" true; then return 1; fi
    timeout 4 php -r '
        $s = @stream_socket_client("unix://" . $argv[1], $e, $m, 2);
        if (!$s) exit(1);
        stream_set_timeout($s, 2);
        fwrite($s, "{\"action\":\"capabilities\",\"arguments\":{}}\n");
        $r = json_decode(fgets($s, 65536), true);
        fclose($s);
        exit(is_array($r) && ($r["ok"] ?? false) === true && is_array($r["result"] ?? null) ? 0 : 1);
    ' "$aria_runtime/agent.sock" >/dev/null 2>&1
}

aria_supervise() {
    local aria_role="$1" aria_child='' aria_child_start='' aria_attempt aria_active_pid='' aria_active_start='' aria_active_stat=''
    [[ "$aria_role" == serve || "$aria_role" == work ]] || exit 1
    exec 8> "$aria_state/$aria_role.supervisor.lock"
    flock -n 8 || exit 0
    printf '%s %s\n' "$$" "$(aria_pid_start "$$")" > "$aria_state/$aria_role.pid"
    aria_signal_active() {
        aria_active_pid=''
        if [[ "$aria_role" == work && -f "$aria_state/active-process.json" && ! -L "$aria_state/active-process.json" ]]; then
            read -r aria_active_pid aria_active_start < <(jq -r '[.pid, .start_time] | @tsv' "$aria_state/active-process.json" 2>/dev/null) || aria_active_pid=''
        fi
        if [[ ! "$aria_active_pid" =~ ^[0-9]+$ || ! "$aria_active_start" =~ ^[0-9]+$ || "$aria_active_pid" -le 1 || "$(aria_pid_start "$aria_active_pid" || true)" != "$aria_active_start" ]]; then
            # PHP is frozen before shutdown. Catch the direct timeout child even
            # if shutdown landed between proc_open and its durable PID record.
            aria_active_pid="$(awk '{print $1}' "/proc/$aria_child/task/$aria_child/children" 2>/dev/null || true)"
            [[ "$aria_active_pid" =~ ^[0-9]+$ && "$aria_active_pid" -gt 1 ]] || { aria_active_pid=''; return 0; }
            aria_active_start="$(aria_pid_start "$aria_active_pid" || true)"
            [[ -n "$aria_active_start" ]] || { aria_active_pid=''; return 0; }
        fi
        aria_active_stat="$(cat "/proc/$aria_active_pid/stat" 2>/dev/null)" || { aria_active_pid=''; return 0; }
        aria_active_stat="${aria_active_stat##*) }"
        if [[ "$(awk '{print $3}' <<< "$aria_active_stat")" != "$aria_active_pid" ]]; then aria_active_pid=''; return 0; fi
        kill -TERM -- "-$aria_active_pid" 2>/dev/null || true
        kill -CONT -- "-$aria_active_pid" 2>/dev/null || true
    }
    aria_cleanup() {
        trap - TERM INT EXIT
        if [[ -n "$aria_child" && "$(aria_pid_start "$aria_child" || true)" == "$aria_child_start" ]]; then
            kill -STOP -- "-$aria_child" 2>/dev/null || true
            aria_signal_active
            kill -TERM -- "-$aria_child" 2>/dev/null || true
            kill -CONT -- "-$aria_child" 2>/dev/null || true
            for ((aria_attempt=0; aria_attempt<10; aria_attempt++)); do
                if ! kill -0 -- "-$aria_child" 2>/dev/null && { [[ -z "$aria_active_pid" ]] || ! kill -0 -- "-$aria_active_pid" 2>/dev/null; }; then break; fi
                sleep 1
            done
            kill -KILL -- "-$aria_child" 2>/dev/null || true
            wait "$aria_child" 2>/dev/null || true
        else
            aria_signal_active
        fi
        if [[ -n "$aria_active_pid" ]]; then kill -KILL -- "-$aria_active_pid" 2>/dev/null || true; fi
        rm -f "$aria_state/$aria_role.pid"
        exit 0
    }
    trap aria_cleanup TERM INT EXIT
    while true; do
        aria_rotate "$aria_state/$aria_role.log"
        if [[ "$aria_role" == serve ]]; then
            setsid php -d short_open_tag=On "$aria_source/host-agent.php" serve "$aria_state" "$aria_runtime/agent.sock" 8>&- >> "$aria_state/$aria_role.log" 2>&1 &
        else
            setsid php -d short_open_tag=On "$aria_source/host-agent.php" work "$aria_state" 8>&- >> "$aria_state/$aria_role.log" 2>&1 &
        fi
        aria_child=$!
        aria_child_start="$(aria_pid_start "$aria_child" || true)"
        while kill -0 "$aria_child" 2>/dev/null; do
            aria_rotate "$aria_state/$aria_role.log"
            sleep 2 8>&- & wait $! || true
        done
        wait "$aria_child" 2>/dev/null || true
        # A crashed PHP worker must not leave an earlier host command running.
        aria_signal_active
        if [[ -n "$aria_active_pid" ]]; then kill -KILL -- "-$aria_active_pid" 2>/dev/null || true; fi
        kill -KILL -- "-$aria_child" 2>/dev/null || true
        aria_child=''
        sleep 2 8>&- & wait $! || true
    done
}

aria_install_boot() {
    local aria_boot=/boot/config/plugins/aria-gpt-bridge aria_go=/boot/config/go
    [[ -f "$aria_go" && ! -L "$aria_go" ]] || { aria_error 'Expected a regular /boot/config/go startup file.'; return 1; }
    mkdir -p "$aria_boot"
    {
        printf '#!/usr/bin/env bash\nset -euo pipefail\numask 077\n'
        printf 'aria_source=%q\naria_data=%q\n' "$aria_source" "$aria_data"
        cat <<'BOOT'
mkdir -p /var/run/aria-gpt-bridge
chown root:65532 /var/run/aria-gpt-bridge
chmod 750 /var/run/aria-gpt-bridge
exec 9>/var/run/aria-gpt-bridge-boot.lock
flock -n 9 || exit 0
while true; do
    if [[ -f "$aria_source/host-service.sh" ]] && timeout 5 docker info >/dev/null 2>&1; then
        ARIA_APPDATA_ROOT="$aria_data" bash "$aria_source/host-service.sh" start
        exit $?
    fi
    sleep 2
done
BOOT
    } > "$aria_boot/boot.sh.tmp"
    chmod 700 "$aria_boot/boot.sh.tmp"
    mv -f "$aria_boot/boot.sh.tmp" "$aria_boot/boot.sh"
    if ! grep -Fqx '# Aria GPT host management' "$aria_go"; then
        cp -p "$aria_go" "$aria_go.aria-backup-$(date +%Y%m%d%H%M%S)"
        printf '\n# Aria GPT host management\nbash /boot/config/plugins/aria-gpt-bridge/boot.sh >/var/log/aria-gpt-bridge-boot.log 2>&1 &\n' >> "$aria_go"
    fi
}

aria_preflight
mkdir -p "$aria_state" "$aria_runtime"
chown root:root "$aria_state"
chmod 700 "$aria_state"
chown root:65532 "$aria_runtime"
chmod 750 "$aria_runtime"
if [[ "${1:-}" == _supervise ]]; then aria_supervise "${2:-}"; exit; fi
exec 9> "$aria_state/service.lock"
flock -w 30 9 || { aria_error 'Another host-service operation is in progress.'; exit 1; }
case "${1:-}" in
    start|restart)
        if [[ "$1" == restart ]]; then aria_stop_role serve; aria_stop_role work; fi
        for aria_role in serve work; do
            if ! aria_alive "$aria_role"; then
                ARIA_APPDATA_ROOT="$aria_data" setsid nohup bash "$aria_source/host-service.sh" _supervise "$aria_role" 9>&- </dev/null >/dev/null 2>&1 &
            fi
        done
        for ((aria_attempt=0; aria_attempt<30; aria_attempt++)); do
            if aria_ready; then
                printf 'Aria host management is running.\n'; exit 0
            fi
            sleep 1
        done
        aria_error 'Host management failed to start; inspect management/serve.log and work.log.'
        exit 1
        ;;
    stop) aria_stop_role serve; aria_stop_role work; rm -f "$aria_runtime/agent.sock" ;;
    status) for aria_role in serve work; do if aria_alive "$aria_role"; then printf '%s running\n' "$aria_role"; else printf '%s stopped\n' "$aria_role"; fi; done ;;
    install-boot) aria_install_boot; printf 'Boot startup installed; the original go file was backed up before adding its marker.\n' ;;
    *) aria_error 'Usage: bash host-service.sh {start|stop|restart|status|install-boot}'; exit 1 ;;
esac
