#!/usr/bin/env bash
# Move an existing installation to the dedicated repository without rewriting
# the old checkout or moving tunnel credentials, appdata, jobs or settings.
set -euo pipefail
umask 077
aria_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
aria_old_source="$(cd -- "$aria_source/../.." && pwd)"
aria_data="${ARIA_APPDATA_ROOT:-/mnt/user/appdata/aria-gpt-bridge}"
aria_origin='https://github.com/kevinwaynekelly/aria-gpt-bridge.git'
source "$aria_source/unraid-common.sh"
aria_preflight
[[ $# == 0 ]] || { aria_error 'Usage: bash tools/aria_bridge/migrate-repository.sh'; exit 1; }
export GIT_TERMINAL_PROMPT=0

aria_git() {
    git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c submodule.recurse=false "$@"
}
aria_assert_old_checkout() {
    [[ "$(aria_git -C "$aria_old_source" rev-parse --show-toplevel)" == "$aria_old_source" ]] || { aria_error 'The old bridge checkout is unavailable.'; return 1; }
    [[ "$(aria_git -C "$aria_old_source" symbolic-ref --quiet --short HEAD)" == main ]] || { aria_error 'The old bridge checkout must use main.'; return 1; }
    [[ -z "$(aria_git -C "$aria_old_source" status --porcelain=v1 --untracked-files=all)" ]] || { aria_error 'The old bridge checkout has local changes; preserve or commit them before migration.'; return 1; }
    case "$(aria_git -C "$aria_old_source" remote get-url --all origin)" in
        https://github.com/kevinwaynekelly/kevin-cogs|https://github.com/kevinwaynekelly/kevin-cogs.git|git@github.com:kevinwaynekelly/kevin-cogs|git@github.com:kevinwaynekelly/kevin-cogs.git|ssh://git@github.com/kevinwaynekelly/kevin-cogs.git) ;;
        *) aria_error 'Migration must be run from the original kevinwaynekelly/kevin-cogs checkout.'; return 1 ;;
    esac
    [[ "$(aria_git -C "$aria_old_source" rev-parse --verify HEAD)" == "$aria_old_revision" ]] || { aria_error 'The old bridge checkout changed during migration.'; return 1; }
}
aria_old_revision="$(aria_git -C "$aria_old_source" rev-parse --verify HEAD)"
aria_assert_old_checkout
timeout 20 docker info >/dev/null
if [[ -z "${ARIA_APPDATA_ROOT:-}" ]]; then
    aria_status_dir="$(docker inspect --format '{{range .Mounts}}{{if and (eq .Type "bind") (eq .Destination "/status")}}{{.Source}}{{end}}{{end}}' aria-gpt-bridge)"
    aria_valid_path "$aria_status_dir" && [[ -d "$aria_status_dir" ]] || { aria_error 'The installed bridge status directory is unavailable.'; exit 1; }
    aria_data="$(dirname -- "$aria_status_dir")"
    aria_preflight
fi
[[ -d "$aria_data/management" ]] || { aria_error 'The existing host management state is unavailable.'; exit 1; }
aria_destination="$aria_data/source-standalone"
[[ ! -e "$aria_destination" && ! -L "$aria_destination" ]] || { aria_error "Destination already exists and was preserved: $aria_destination"; exit 1; }
[[ ! -L "$aria_data/repository-migration.lock" ]] || { aria_error 'The migration lock must not be a symlink.'; exit 1; }
exec 6> "$aria_data/repository-migration.lock"
flock -n 6 || { aria_error 'Another repository migration is already running.'; exit 1; }

aria_stage=''
aria_host_stopped=false
aria_complete=false
aria_recover() {
    local aria_exit=$? aria_recovered=true
    trap - EXIT INT TERM
    if [[ "$aria_complete" != true && "$aria_host_stopped" == true ]]; then
        printf 'Migration failed; restoring the original host-service and boot paths.\n' >&2
        flock -u 5 || true
        ARIA_APPDATA_ROOT="$aria_data" bash "$aria_source/host-service.sh" restart 5>&- 6>&- 7>&- || aria_recovered=false
        ARIA_APPDATA_ROOT="$aria_data" bash "$aria_source/host-service.sh" install-boot 5>&- 6>&- 7>&- || aria_recovered=false
        if [[ "$aria_recovered" != true ]]; then
            printf 'Host recovery needs attention. Original source retained at %s\n' "$aria_old_source" >&2
        fi
        printf 'The upgrade script attempts container rollback. Inspect its output before retrying.\n' >&2
    fi
    if [[ -n "$aria_stage" && -d "$aria_stage" ]]; then rm -rf -- "$aria_stage"; fi
    exit "$aria_exit"
}
trap aria_recover EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
aria_stage="$(mktemp -d "$aria_data/.repository-stage-XXXXXXXX")"
# The clone uses only the fixed dedicated origin and keeps the old tree intact.
timeout 190 php "$aria_source/github-auth.php" git clone --no-recurse-submodules --single-branch --branch main --origin origin "$aria_origin" "$aria_stage/checkout" 6>&-
aria_new_source="$aria_stage/checkout"
[[ "$(aria_git -C "$aria_new_source" remote get-url --all origin)" == "$aria_origin" ]] || { aria_error 'Unexpected dedicated repository origin.'; exit 1; }
[[ "$(aria_git -C "$aria_new_source" symbolic-ref --quiet --short HEAD)" == main ]] || { aria_error 'The dedicated repository must use main.'; exit 1; }
[[ -z "$(aria_git -C "$aria_new_source" status --porcelain=v1 --untracked-files=all)" ]] || { aria_error 'The new checkout is not clean.'; exit 1; }
for aria_file in host-agent.php bridge-update.php host-service.sh unraid-common.sh upgrade-unraid.sh; do
    [[ -f "$aria_new_source/tools/aria_bridge/$aria_file" && ! -L "$aria_new_source/tools/aria_bridge/$aria_file" ]] || { aria_error 'The dedicated repository is missing a required bridge file.'; exit 1; }
done
bash -n "$aria_new_source/tools/aria_bridge/upgrade-unraid.sh"
php -l "$aria_new_source/tools/aria_bridge/host-agent.php" >/dev/null
php -l "$aria_new_source/tools/aria_bridge/bridge-update.php" >/dev/null

# Reserve all mutations while the migration switches host services. No queued
# or active work is discarded to make room for this operation.
aria_install_lock
[[ ! -L "$aria_data/management/queue.lock" ]] || { aria_error 'The management queue lock is invalid.'; exit 1; }
exec 5> "$aria_data/management/queue.lock"
flock -w 15 5 || { aria_error 'Management is busy; try migration after current work finishes.'; exit 1; }
for aria_job in "$aria_data/management/jobs/"*.json "$aria_data/management/bridge-updates/"*.json; do
    [[ -e "$aria_job" ]] || continue
    [[ ! -L "$aria_job" ]] && jq -e 'type == "object" and (.status | type == "string")' "$aria_job" >/dev/null || { aria_error 'A management job record is invalid; inspect state before migration.'; exit 1; }
    if jq -e '.status == "queued" or .status == "running" or .status == "unknown"' "$aria_job" >/dev/null; then
        aria_error 'Queued, running or uncertain management work must be resolved before migration.'; exit 1
    fi
done
aria_assert_old_checkout
[[ ! -e "$aria_destination" && ! -L "$aria_destination" ]] || { aria_error 'The migration destination appeared concurrently and was preserved.'; exit 1; }
mv -T -n -- "$aria_new_source" "$aria_destination"
[[ ! -e "$aria_new_source" ]] || { aria_error 'The migration destination appeared concurrently and was preserved.'; exit 1; }
aria_host_stopped=true
ARIA_APPDATA_ROOT="$aria_data" bash "$aria_source/host-service.sh" quiesce 5>&- 6>&- 7>&-
# Socket and scheduler submitters are stopped while the queue is reserved.
# Release it while stopping the idle worker; keeping it locked here can make
# native PHP shutdown block inside flock. Reserve it again before replacement.
flock -u 5
ARIA_APPDATA_ROOT="$aria_data" bash "$aria_source/host-service.sh" stop 5>&- 6>&- 7>&-
flock -w 15 5 || { aria_error 'The queue became busy after host shutdown; migration stopped.'; exit 1; }
ARIA_APPDATA_ROOT="$aria_data" ARIA_INHERITED_INSTALL_LOCK=1 bash "$aria_destination/tools/aria_bridge/upgrade-unraid.sh" 5>&- 6>&-
aria_complete=true
printf 'Migration complete. Active bridge source: %s\nOriginal source retained for recovery: %s\n' "$aria_destination" "$aria_old_source"
