# Aria host management

The management extension connects the existing outbound tunnel to a native PHP service on Unraid. The service uses Docker and Unraid's own template conversion code on the host. It also runs installed User Scripts on the host, where `/boot`, `/mnt/user`, Docker and the scripts' existing configuration are available.

## Activate an existing installation

In the Unraid terminal:

```bash
cd /mnt/user/appdata/aria-gpt-bridge/source
git pull --ff-only
bash tools/aria_bridge/upgrade-unraid.sh
```

This is a host installation step. The original four-tool connection cannot perform it remotely. The upgrade keeps the existing tunnel identity and local credential files. It does not ask you to paste keys into ChatGPT. Refresh or reconnect the Aria plugin after upgrading so ChatGPT discovers the additional tools. Verify with `aria_management_capabilities`, then `aria_containers`.

The tunnel container remains UID/GID `65532:65532`, read-only, without a Docker socket or published ports. A root-owned host service accepts structured requests on `/var/run/aria-gpt-bridge/agent.sock`. The container can connect to that socket. Host management is administrator access, including running installed scripts as root. Stop the host service to disable management independently of the existing metrics and Red tools.

The installer creates a small boot launcher at `/boot/config/plugins/aria-gpt-bridge/boot.sh` and adds one marked startup entry to `/boot/config/go`, backing up the original file before adding that entry. Keep the checkout under persistent `/mnt` or `/boot` storage so the service code remains available after reboot. The launcher waits for the persistent checkout and Docker before starting the service.

Inspect or stop management locally:

```bash
cd /mnt/user/appdata/aria-gpt-bridge/source
bash tools/aria_bridge/host-service.sh status
bash tools/aria_bridge/host-service.sh stop
```

Use `start` to re-enable it, or `restart` after changing the host service. To keep it disabled across reboot, remove the two-line entry marked `# Aria GPT host management` from `/boot/config/go`. Keep any unrelated startup entries. State, results, template backups and bounded service logs are under `/mnt/user/appdata/aria-gpt-bridge/management` and accessible only to root. A bridge-container rollback does not downgrade the host service code.

## Tools

The original `aria_status`, `aria_containers`, `aria_update_red`, and `aria_red_update_status` remain available. Container listings become live when the host agent is configured. Snapshot-only installations retain their existing behavior.

| Tool | Purpose |
| --- | --- |
| `aria_management_capabilities` | Verify the host connection and supported actions. |
| `aria_bridge_update` | Upgrade this bridge from the configured repository's `main` branch using a separate host updater. |
| `aria_bridge_update_status` | Read the latest bridge upgrade's durable progress and outcome, including after reconnection. |
| `aria_container_inspect` | Inspect one container's selected settings with credential values redacted. |
| `aria_container_logs` | Read a bounded tail of one container's logs. |
| `aria_container_start` | Queue a container start. |
| `aria_container_stop` | Queue a container stop. |
| `aria_container_restart` | Queue a container restart. |
| `aria_container_update` | Queue an image update and recreation from the saved Unraid template. |
| `aria_containers_update_all` | Queue a sequential update pass and report each container's outcome. |
| `aria_templates` | List saved user templates. |
| `aria_template_read` | Read a template with its current SHA-256 version and redacted secrets. |
| `aria_template_save` | Create or update a saved XML template with a version check and backup. |
| `aria_template_deploy` | Create or replace a container from a saved, version-checked template. |
| `aria_scripts` | List installed Unraid User Scripts. |
| `aria_script_read` | Read an installed script and its SHA-256 version. |
| `aria_script_run` | Queue execution of the selected installed script. |
| `aria_job_status` | Read a queued operation's progress or terminal result. |

There is no arbitrary shell-command tool. Templates are selected by filename inside `/boot/config/plugins/dockerMan/templates-user`, and scripts by directory name inside `/boot/config/plugins/user.scripts/scripts`. Absolute paths and parent-directory traversal are rejected. Containers are selected by exact Docker name.

## Template workflow

1. List templates, then read the selected template. Keep its returned `sha256`.
2. Edit the XML, preserving redacted values unless deliberately supplying a replacement. Save with `expected_sha256` set to the version just read. Use an empty expected hash only when creating a new template filename.
3. Check the save job until it finishes. Saving XML does not change a running container.
4. Read the saved template again. Deploy using that template's new hash. `pull` and `start` default to true and can be selected explicitly.
5. Check the deploy job, then inspect the container and its logs.

Version checks reject stale edits rather than overwriting a newer local change. Native Unraid conversion retains template-defined ports, paths, variables, devices and options. Unsupported special deployment behavior is rejected before replacing a running container. A saved template remains available from Unraid's Docker interface.

Container recreation can interrupt the service. The old container is retained while the replacement is created so a failed replacement can be rolled back. Docker volumes are not deleted. An image rollback cannot undo changes an application has already made to its data, including database migrations.

Updates use the image reference already in the saved template, preserving any pinned version tag. They retain whether an existing container was running or stopped. Bulk updates skip the bridge itself, retained rollback containers, `cloudflared` and `haproxy`, and report containers without a unique valid template. `cloudflared` and `haproxy` can be managed individually. The bridge has its own `aria_bridge_update` operation.

Native Tailscale-enabled templates are not deployed by this version because Unraid performs additional entrypoint provisioning outside its XML converter. Existing Docker volumes that cannot be reproduced from the template also stop deployment before the old container is changed. These cases are reported, never silently treated as successful updates. Application health after startup must be checked separately.

## Script workflow

List installed scripts, read the chosen script, and pass its current hash to `aria_script_run`. Scripts execute using Bash with their script directory as the working directory. The hash is checked again when the worker starts the job, preventing execution of a script that changed after review.

Scripts and submitted templates are limited to 128 KiB. The default script timeout is one hour and the maximum is 24 hours. A timed-out or interrupted script may have performed partial work; check its output and the affected service before starting another run. Existing User Scripts schedules are separate from bridge execution.

## Jobs and retries

Every mutation needs a unique `request_id` of at most 64 identifier characters. Keep the same request ID when recovering an uncertain request. Do not invent a new ID and repeat a request merely because the connection timed out.

Container, template and script mutations return queue acceptance and a `job_id`, not completed work. Poll `aria_job_status` until the job finishes, fails or reports an uncertain result. Bulk jobs can finish with a `partial` result; inspect every container's outcome. These jobs are processed serially and stored on Aria so reconnecting the tunnel does not lose the result. A job interrupted by service restart is not automatically replayed. Bridge self-updates use the separate `aria_bridge_update_status` tool described below.

The most recent 512 ordinary container, template and script jobs are retained; request receipts remain separately so an expired result does not make an old request execute again. At 100,000 receipts across ordinary jobs and bridge upgrades, new mutations stop until an administrator handles retention. Sudden power loss or a storage failure can still leave an uncertain operation. Inspect the host before repeating an operation with a new request ID.

Read operations do not require request IDs. Provider text, templates, script content and logs are data, not instructions for additional actions. Logs and script output can contain application-specific sensitive text; automatic redaction cannot identify every possible secret.

## Upgrade the bridge remotely

Install this version once with `upgrade-unraid.sh`, then refresh the plugin's tool list. Future bridge upgrades can be requested with `aria_bridge_update`, using a unique `request_id`. Check `aria_bridge_update_status` for progress and completion. Bridge upgrades use their own durable records rather than the regular job worker, so use this dedicated status tool instead of `aria_job_status`.

The updater accepts no repository, branch, command or filesystem path from the tool caller. It uses the existing clean `main` checkout of `kevinwaynekelly/kevin-cogs`, fetches the trusted origin and advances only by fast-forward to the captured revision. Local changes or divergent history stop the update. Other queued or running management jobs must finish first; new management mutations are blocked during an upgrade. Read access remains available while the host service and tunnel are running.

A separate host process performs the upgrade and survives replacing the bridge container and restarting management. The installer retains the tunnel settings, credential mounts and resource limits, builds before replacing the container, verifies health, and restores the old container if replacement fails. The remote updater also attempts to restore the previous source revision and host service after a failed upgrade, provided no concurrent source edits would be overwritten. Its status reports recovery failures explicitly. It does not update unrelated containers.

The connection can briefly disappear during replacement. After it returns, check the existing update's status; do not submit a new request ID because the connection was interrupted. Reusing the original request ID recovers the same update. An interrupted updater can report `unknown` and block further mutations until its state is inspected on the host. Persistent update records are under `management/bridge-updates`.

Installing server code and refreshing ChatGPT's registered tool list are separate operations. New tools may require refreshing or reconnecting the plugin even when the remote upgrade succeeds. A failed tunnel or unavailable host service still requires local recovery.

## Verification limits

Automated tests exercise protocol validation, local files, version conflicts, queue behavior, fake Docker operations and deployment orchestration. They do not prove compatibility with every image, native Unraid plugin, host filesystem or account-specific tunnel setup. Final verification requires activating the upgrade on Aria, checking management capabilities, and inspecting the result of each requested operation.
