# Aria bridge feature guide

Version 2 is a root-authorized Unraid administration bridge. Tools return structured,
bounded results through the outbound tunnel; the host owns durable jobs and credentials.
New tools require refreshing the plugin catalog after installing this version.

## Operating model

Read tools return observations. Most changes return a job ID and must be checked with
`aria_job_status`. A request ID belongs to one operation and its arguments; reuse it
when recovering an uncertain request. Nested JSON key order does not change its identity.
Queued jobs can be cancelled before the worker claims them. Running commands report elapsed
time and output byte counts without exposing unredacted live output. Final results contain
bounded, redacted output. Unknown application outcomes are not automatically retried.

Host execution is deliberately administrator access. Literal argument arrays are passed
without implicit shell evaluation. For shell logic, create or edit a named script, check its
SHA-256, then run it. Host commands, scripts and file changes can affect Unraid itself.
Per-container policies constrain typed tools and are not a security boundary against root
commands or external changes. No container updates, VM operations, migration, scanning,
application API profiles or outgoing messages run merely because these tools are installed.

## Common workflows

- **Scripts:** Read the script, save reviewed Bash with its expected hash, then execute the
  returned version. Parameters are literal arguments; secret environment values use vault
  references. Existing User Scripts cron settings are preserved. Bridge schedules are separate.
- **Files:** Inspect or read a path, then write with its expected hash and a retained backup.
  Small files are bounded to 128KiB. Paths stay within declared roots, symlinks are rejected,
  and filesystem modes/owners are preserved or explicitly selected.
- **Credentials:** Import a local file using its hash into the root-private vault, then reference
  the secret by name in scripts or app profiles. Reads return metadata, not credential values.
  Changing a reference's stored value updates consumers of that reference; it does not change
  the password or API key in the external application itself.
- **Applications:** Save profiles for Sonarr, Radarr, Plex or qBittorrent using a URL and vault
  reference. Query status, health, queues, libraries or sessions, then use a typed command.
  Download tracing correlates one Arr queue item with a qBittorrent hash. TLS verification is
  on by default, redirects are refused, and returned records are projected and redacted.
- **Templates:** Preview changes, save with a hash, inspect version history, and restore a
  retained version when needed. Search templates for paths, addresses or settings, inspect
  drift against running Docker, and pin an image digest. Saved stacks bind template hashes
  and dependency order, then apply serially and stop on failure.
- **Updates:** A running replacement must pass its configured Docker health check before the
  original is removed. Absence of a health check is explicitly unverified. Container policies
  can restrict operations, hold updates, require health checks, select maintenance hours,
  require idle app profiles, or require other containers to be stopped.
- **Trial and migration:** Isolated trials copy stopped appdata within an explicit byte budget,
  use no network/host ports/devices and drop capabilities. Appdata migrations verify hashes,
  ownership and permissions, retain the source, update the selected template, and leave
  deployment as a separate operation. Image rollback cannot reverse database migrations.
- **Backups:** Archive template/script configuration, verify inventory and checksums, preview
  recovery targets, and restore with current-state version checks. Configuration archives do
  not include all application data. Filesystem/appdata backups can be run as reviewed scripts.
- **Diagnostics:** Inspect container exits, restarts, OOM state, logs, mounts, storage, shares,
  permissions, Docker resource history, networks, certificates, devices and disks. Automatic
  resource sampling and storage/container monitoring run every 5 minutes while idle. Native SMART/libvirt/GPU/scanner programs
  are used only when installed; missing dependencies are reported rather than guessed.
- **Virtual machines:** Inspect native libvirt definitions and lifecycle state, edit stopped VM
  definitions with version checks/backups, and manage supported offline qcow2 snapshots.
  Snapshot reversion requires explicit acknowledgement of discarded changes.
- **Automation:** Schedule approved actions by interval, local time or a successful job event.
  Event chains have depth/loop guards, scripts remain hash-bound, and backup verification can
  bind to the exact archive produced by its triggering job. Failures go to a local inbox.

## Daily bridge updater

The installed host scheduler checks daily after an initial 24-hour delay, during 02:00–06:00
America/Chicago. It checks only the fixed trusted repository's main branch, verifies both
Python 3.10/3.11 GitHub Actions checks for the exact commit, and installs only bridge changes.
It compares installed revision metadata, so manually pulling source does not hide a pending
image rebuild. Dirty/diverged checkouts, busy/unknown jobs, failed CI and unavailable network
or repository access defer updates. It never schedules updates of unrelated containers.

The scheduler runs on Aria without an open ChatGPT conversation. Self-update records survive
container replacement. New tools can still require a plugin catalog refresh. The local HTML
dashboard is a generated status snapshot and job history, not an interactive web control panel.
Notifications stay in the local inbox; no Slack, email or other messages are sent automatically.
Storage alerts begin at 90% usage and become critical at 95%; unhealthy/restarting containers also
produce local notices. Incident deduplication prevents repeated alerts. History is bounded;
SMART history records explicit jobs and does not wake drives in the background.

## Local browser and terminal management

The optional browser dashboard and CLI remain usable without the ChatGPT tunnel. They use
the same validation, queue, version checks and policies. The browser interface selects an
operation, shows its argument schema, submits reviewed JSON, and polls queued results.

Enable the loopback-only dashboard and keep it enabled after reboot:

```bash
bash tools/aria_bridge/host-service.sh dashboard-enable
```

From your computer, forward the local port with
`ssh -L 8786:127.0.0.1:8786 root@Aria`, then open `http://127.0.0.1:8786`.
Read the generated `management/dashboard-token` in the host root terminal and enter it into
the local login field. The token is kept only in browser memory and is never embedded in the
page, returned in tool results or sent to a public endpoint. Disable the optional service with
`bash tools/aria_bridge/host-service.sh dashboard-disable`. No LAN port is opened automatically.

The CLI also works directly from an Unraid root terminal:

```bash
php tools/aria_bridge/aria-local.php /mnt/user/appdata/aria-gpt-bridge/management --catalog
php tools/aria_bridge/aria-local.php /mnt/user/appdata/aria-gpt-bridge/management containers '{}'
```

Mutations require a request_id and return queue acceptance. Read the resulting job status
before treating the operation as complete. The host worker must be running to execute jobs.


## Extension tool catalog

The original 22 tools remain available. The following tools are loaded from shared manifests
validated by both the public Python bridge and the root PHP host.

### Operations

| Tool | Mode | Purpose |
| --- | --- | --- |
| `aria_file_roots` | Read | List managed host filesystem roots and size bounds. File operations reject symlinks and the bridge management state directory. |
| `aria_file_stat` | Read | Inspect a managed path and obtain a signature for a subsequent permission change. |
| `aria_file_list` | Read | List one directory page, without recursion or following symlinks. Filesystem ordering may change between calls. |
| `aria_file_read` | Read | Read a UTF8 managed text file up to 128 KiB. Output is limited to its last 64 KiB and known secrets are redacted. Private key and shadow files cannot be read. Hash covers original bytes, never edit redaction markers as if they were original values. |
| `aria_host_exec` | Change / job | Queue a privileged root command as a literal argv array. First argument must be an absolute executable path; no implicit shell parsing occurs. Use script_save plus script_run_arguments for shell text. This can change the host; only use for the requested administration task. Output is bounded and redacted on a best-effort basis. |
| `aria_container_exec` | Change / job | Queue a literal argv command inside an existing container, resolved to its immutable ID. First command argument must be an absolute executable path. Supports user, working directory and named secret environment references; rejects bridge self-execution. A timeout stops the Docker client; the command inside the container may continue and requires inspection. |
| `aria_script_save` | Change / job | Create or edit an installed Unraid User Script using expected SHA256. Validates Bash syntax, backs up the previous script and writes atomically; does not execute it. Store credentials separately and use named secret references. |
| `aria_script_run_arguments` | Change / job | Run exactly the installed User Script bytes identified by SHA256 with literal positional arguments and optional named secret environment references. Root execution uses the script directory and a bounded timeout. |
| `aria_secret_list` | Read | List named credential references and their opaque revisions. Secret values are never returned. |
| `aria_secret_import` | Change / job | Import or rotate a named secret from a managed host UTF8 file up to 16 KiB. Source hash prevents importing changed content; expected_revision is empty for a new name or the current revision when replacing it. Raw file bytes are preserved, including trailing newlines. Values are stored root-only and never returned. |
| `aria_secret_delete` | Change / job | Delete a named credential reference only if its revision still matches. Profiles referencing it will stop working until reconfigured. |
| `aria_file_write` | Change / job | Create or atomically replace a managed text file up to 128 KiB using expected SHA256. Existing content is backed up. Content containing redaction placeholders is rejected; supply complete intended content. |
| `aria_file_mkdir` | Change / job | Create exactly one managed directory with an existing parent. Does not overwrite an existing path. |
| `aria_file_permissions` | Change / job | Change permissions and optionally numeric owner/group for one managed file or directory, only if its stat signature matches. Never recursive. Mode is a four-digit octal string without special bits. |
| `aria_file_backup` | Change / job | Save a root-only recovery copy of a managed regular file up to 128 KiB after checking its SHA256. |
| `aria_file_restore` | Change / job | Restore a previously backed-up file to its original managed path, checking current SHA256 first. Empty expected SHA means currently absent. Backs up overwritten content again. |
| `aria_config_inventory` | Change / job | Queue a complete bounded template/User Script inventory and a content/mode signature for archive restore preconditions. This is queued because large inventories may exceed interactive timeouts; it does not change configuration. |
| `aria_archive_create` | Change / job | Queue a root-only compressed recovery archive of Docker XML templates and installed User Scripts script files, with per-file hashes. Excludes appdata, vault credentials, plugin schedule settings and container images. |
| `aria_archive_list` | Read | List up to 100 retained configuration recovery archives with integrity hashes and size metadata. |
| `aria_archive_verify` | Change / job | Queue archive integrity, XML parsing and Bash syntax checks. Does not start applications or validate backed-up application data. |
| `aria_archive_restore` | Change / job | Restore configuration files from a verified archive, after checking the current complete inventory hash. Backs up current configuration first and attempts rollback on write failure. overwrite must be true to replace different existing files. Does not deploy containers, remove extra files, or restore changed bridge templates. |

### Diagnostics

| Tool | Mode | Purpose |
| --- | --- | --- |
| `aria_diagnostics_health` | Read | Read live host memory, load, uptime, Unraid and source versions, worker observation and job counts. |
| `aria_diagnostics_storage` | Read | Read filesystem use and mount inventory without scanning file contents. |
| `aria_diagnostics_shares` | Read | List Unraid share settings, including SMB export and access lists, excluding credentials. |
| `aria_diagnostics_permissions` | Read | Inspect POSIX ownership and permissions for one existing path under /mnt; does not recurse or modify files. |
| `aria_diagnostics_container` | Read | Diagnose one container using Docker state, OOM and restart evidence, mount existence and redacted recent logs. |
| `aria_diagnostics_resources` | Read | Sample container CPU, RAM, network, block I/O and PIDs. Retains up to 64 bounded snapshots locally. |
| `aria_diagnostics_resource_history` | Read | Read up to 20 retained resource snapshots collected on demand or by a configured monitoring schedule. |
| `aria_diagnostics_network` | Read | Run a bounded host DNS, TCP, HTTP or TLS certificate check. DNS needs host; TCP needs host and port; TLS needs host and optional port (443); HTTP needs url. No redirects, credentials or response bodies are returned. |
| `aria_diagnostics_devices` | Read | List GPU utilization, PCI and USB inventory using installed host tools. Missing dependencies are reported. |
| `aria_diagnostics_disks` | Read | List block devices, filesystems, models and mountpoints using lsblk. |
| `aria_diagnostics_vm_list` | Read | List all native libvirt VMs and their current state. |
| `aria_diagnostics_vm_inspect` | Read | Read one VM configuration, disks, interfaces and passed-through devices, excluding secrets and raw XML. |
| `aria_diagnostics_vm_action` | Change / job | Queue a native VM start, graceful shutdown, reboot, pause or resume. Guest shutdown/reboot completion is asynchronous. |
| `aria_diagnostics_smart` | Change / job | Queue a read-only SMART health report for one physical disk. Does not start self-tests or change disk settings. |
| `aria_diagnostics_security_capabilities` | Read | Report whether the host has a supported Trivy vulnerability scanner installed. |
| `aria_diagnostics_security_scan` | Change / job | Queue a bounded vulnerability scan using installed Trivy. May download vulnerability databases and image metadata; reports findings and completeness. |
| `aria_diagnostics_cleanup_plan` | Read | Show Docker disk-use and reclaimable-space estimates. Does not delete images, containers or volumes. |
| `aria_diagnostics_device_assignments` | Read | Map explicitly assigned GPU/USB/device paths to containers and saved VM configurations. Shared paths are reported as observations, not proven conflicts. |
| `aria_diagnostics_vm_config` | Read | Read inactive VM XML with opaque credential markers and the exact original XML SHA256 for editing. |
| `aria_diagnostics_vm_config_save` | Change / job | Queue an offline VM configuration edit. Requires matching original XML hash, same name and UUID, backs up the original, restores credential markers, and runs native libvirt schema validation before defining it. |
| `aria_diagnostics_vm_snapshots` | Read | List native libvirt snapshots for one VM. |
| `aria_diagnostics_vm_snapshot_read` | Read | Read one snapshot metadata summary and hash for guarded reversion. |
| `aria_diagnostics_vm_snapshot_create` | Change / job | Queue an atomic internal snapshot of a shut-off VM with file-backed qcow2 writable disks. Requires current configuration hash. Unsupported libvirt/storage features are reported as failures. |
| `aria_diagnostics_vm_snapshot_revert` | Change / job | Revert a shut-off qcow2 VM to an offline internal snapshot. Discards disk changes since that snapshot. Requires current configuration and snapshot hashes and explicit discard acknowledgement; saves current XML but does not back up current disk contents. |
| `aria_diagnostics_monitor` | Read | Run bounded storage and container health monitoring. Records local inbox warnings at 90% filesystem use, critical alerts at 95%, and unhealthy or restarting container alerts. Deduplicates active incidents and never repairs automatically. |
| `aria_diagnostics_history` | Read | Read bounded storage, monitor or explicitly sampled SMART history. Retains at most 288 samples and 4 MiB per category; no SMART scans are started by this read. |

### Applications

| Tool | Mode | Purpose |
| --- | --- | --- |
| `aria_application_profiles` | Read | List saved Sonarr, Radarr, Plex and qBittorrent profiles, API operations and credential setup requirements. |
| `aria_application_profile_get` | Read | Read a saved application profile and its SHA. Credentials are represented only by a host vault reference. |
| `aria_application_profile_save` | Change / job | Create or replace an application profile with a backup and expected SHA. Set secret_ref to a previously imported host secret. Sonarr/Radarr/Plex secrets are raw tokens; qBittorrent secrets are JSON {username,password}. Does not contact the application. |
| `aria_application_profile_delete` | Change / job | Remove a saved integration profile with a backup and expected SHA. Does not delete the application or its secret. |
| `aria_application_query` | Read | Read typed application information. Arr supports status/health/queue/history/missing/commands/library; Plex status/sessions/library; qBittorrent status/downloads. Results are bounded and projected to omit credentials and tracker URLs. item_id selects an Arr library item/command or Plex library section. |
| `aria_application_command` | Change / job | Queue a typed application action against the reviewed profile SHA. Arr refresh/rescan requires one series/movie id; Sonarr search one series id; Radarr search movie ids; rss_sync takes no ids. Plex rescan requires one section id. qBittorrent pause/resume/recheck/reannounce requires explicit torrent hashes. Acceptance does not mean the provider finished. |
| `aria_application_activity` | Read | Read bounded activity across explicitly named profiles: Plex sessions, Arr queues, and qBittorrent transfer status. Missing or unreachable applications are marked unavailable, never assumed idle. This does not authorize maintenance. |
| `aria_application_download_trace` | Read | Correlate a torrent hash with Sonarr/Radarr import queue diagnostics and one qBittorrent download. Scans up to600 queue records; marks partial/unavailable evidence explicitly. Never changes either application. |

### Deployment

| Tool | Mode | Purpose |
| --- | --- | --- |
| `aria_template_preview` | Read | Compare reviewed template XML to the saved template without writing; secret values stay redacted. |
| `aria_template_history` | Read | List matching saved template backups and hashes, including backups retained by existing template edits. |
| `aria_template_restore` | Change / job | Restore a selected template backup after checking the current template hash; save another backup and do not deploy automatically. |
| `aria_config_search` | Read | Search redacted saved template settings for paths, ports, images and addresses; never search secret values. |
| `aria_config_drift` | Read | Compare running image, network, privilege, declared mounts and ports against its unique saved template. |
| `aria_image_details` | Read | Read the running image immutable ID, repository digests, build time and OCI release/source metadata; does not pull. |
| `aria_image_pin` | Change / job | Pin a template to a verified locally available repository digest of the running image, retaining a backup. Does not deploy. |
| `aria_container_policy` | Read | Read the bridge policy for a container and its revision hash. Root scripts and external Unraid changes are outside this boundary. |
| `aria_container_policy_save` | Change / job | Replace a container bridge policy with optimistic concurrency. Explicitly configurable actions, update hold, UTC hours, stopped dependencies and Docker health requirements. Optional require_idle_profiles blocks changes while configured app activity is busy or unknown. |
| `aria_stack_save` | Change / job | Save a reusable stack of reviewed template hashes and template dependencies; validates all members and rejects cycles. |
| `aria_stacks` | Read | List saved reusable stack definitions and revision hashes. |
| `aria_stack_plan` | Read | Validate all saved stack template hashes and show dependency order and active policy before deployment. |
| `aria_stack_apply` | Change / job | Deploy a reviewed stack in dependency order and stop on the first failure. Completed members remain deployed and are reported; not an atomic multi-container transaction. |
| `aria_upgrade_test` | Change / job | Test an image in a temporary network-none container with copied, size-bounded appdata and no host devices, published ports or original mounts. Requires all source writers stopped, simple bind-only template and no privileged/device/extra command features. Cleans its own container and copied data. |
| `aria_appdata_migrate` | Change / job | Copy and checksum-verify one stopped appdata directory to a new appdata path within the configured appdata root, then update matching reviewed template mounts and back up the template. Original data is retained. Does not deploy or delete either copy. |

### Automation

| Tool | Mode | Purpose |
| --- | --- | --- |
| `aria_automation_status` | Read | Read host scheduler heartbeat, last bridge update check and schedule runtime. |
| `aria_automation_schedules` | Read | List durable schedules with compare-and-swap hashes. Action arguments are redacted. |
| `aria_automation_dashboard` | Read | Create and return a private static HTML status snapshot, available on the host without ChatGPT. No HTTP listener. |
| `aria_automation_jobs` | Read | List recent jobs and queued/running states. Read individual job status for full output. |
| `aria_automation_inbox` | Read | Read locally stored job failure and automation notifications; no external messages. |
| `aria_automation_schedule_save` | Change / job | Create/update an interval, daily or job-success workflow. Edits require the current schedule hash. Automation runs only when management is idle; changed scripts fail their pinned hash. No container schedules are installed by default. |
| `aria_automation_schedule_delete` | Change / job | Delete a schedule using its current hash. |
| `aria_automation_job_cancel` | Change / job | Immediately cancel a queued job atomically. Running or ambiguous jobs cannot be cancelled through this action. |
| `aria_automation_inbox_ack` | Change / job | Acknowledge one local notification. |
| `aria_automation_bridge_check` | Change / job | Check fixed repository main for bridge changes and verify GitHub CI without installing. The separate default daily host schedule can install CI-verified revisions during its maintenance window. |

## Verification and limits

Automated tests exercise real PHP, Bash, local Git and files with mocked Docker, libvirt,
Unraid installation boundaries and HTTP providers. They do not demonstrate live compatibility
with every container, VM disk, plugin, app version or tunnel configuration. Optional scanners
are integrations, not bundled installations. Redaction is best effort for unknown log formats.
The offline trial can fail for apps that require network or hardware. Native Tailscale template
provisioning remains unsupported by the typed deploy tool.
