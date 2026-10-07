#!/usr/bin/env bash
# Opt-in listener for the root-only local dashboard. Never bind a LAN interface.
set -euo pipefail
umask 077
aria_dashboard_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
aria_dashboard_state="${1:?Usage: dashboard-service.sh STATE_DIRECTORY}"
[[ "$aria_dashboard_state" == /* && ! -L "$aria_dashboard_state" ]] || exit 1
[[ $EUID -eq 0 || "${ARIA_AGENT_TEST_MODE:-}" == 1 ]] || { printf '%s\n' 'Run the local dashboard as root.' >&2; exit 1; }
aria_dashboard_port=8786
if [[ "${ARIA_AGENT_TEST_MODE:-}" == 1 && -n "${ARIA_DASHBOARD_PORT:-}" ]]; then
    [[ "$ARIA_DASHBOARD_PORT" =~ ^[0-9]{1,5}$ && "$ARIA_DASHBOARD_PORT" -ge 1024 && "$ARIA_DASHBOARD_PORT" -le 65535 ]] || exit 1
    aria_dashboard_port="$ARIA_DASHBOARD_PORT"
fi
export ARIA_DASHBOARD_STATE="$aria_dashboard_state"
unset PHP_CLI_SERVER_WORKERS
php -d short_open_tag=On "$aria_dashboard_source/host-dashboard.php" init "$aria_dashboard_state"
printf 'Aria local dashboard listening at http://127.0.0.1:%s\n' "$aria_dashboard_port"
exec php -d short_open_tag=On -d display_errors=Off -d log_errors=On -S "127.0.0.1:$aria_dashboard_port" "$aria_dashboard_source/host-dashboard.php"
