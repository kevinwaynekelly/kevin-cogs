#!/usr/bin/env bash
# Supervised by host-service; no independent daemon or exposed HTTP listener.
set -euo pipefail
umask 077
aria_automation_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
aria_automation_state="${1:?Usage: automation-service.sh STATE_DIRECTORY}"
[[ "$aria_automation_state" == /* && ! -L "$aria_automation_state" ]] || exit 1
exec php -d short_open_tag=On "$aria_automation_source/host-automation.php" daemon "$aria_automation_state"
