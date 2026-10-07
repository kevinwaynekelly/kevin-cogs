# Aria GPT bridge

Connect ChatGPT to the Unraid host at `10.10.1.200` through an outbound OpenAI Secure MCP Tunnel. This is a separate Docker service, not a Red cog. It opens no public port and does not require Pushover, a Discord webhook or a public reverse proxy.

## Enable container, template and User Scripts management

For an existing Aria installation, run this in the **Unraid host terminal**:

```bash
cd /mnt/user/appdata/aria-gpt-bridge/source
git pull --ff-only
bash tools/aria_bridge/upgrade-unraid.sh
```

The upgrade reuses the existing tunnel ID and credential files. It installs a local host service, builds the bridge, and replaces the bridge container. It does not update your other containers or execute your installed User Scripts. After it succeeds, refresh the Aria plugin's available tools in ChatGPT, or reconnect the plugin if its tool list is cached. Ask **“Check Aria's management capabilities.”** to verify the host service is reachable before requesting changes.

Management runs through a private Unix socket. The tunnel container stays unprivileged and has no Docker socket or public listener. The local service runs as root because Unraid container deployment, flash templates and User Scripts require host access. Enabling it gives the connected tunnel/workspace administrator-level management authority. Access follows the existing tunnel permissions.

See [MANAGEMENT.md](MANAGEMENT.md) for tool arguments, job tracking, template editing, update behavior, deployment and recovery limits.

| GPT tool | Behavior |
| --- | --- |
| `aria_status` | Read host uptime, load, available memory and Unraid version. |
| `aria_containers` | Read live container summaries when host management is configured; otherwise read the host-generated snapshot and report its age. |
| `aria_update_red` | Ask DownloaderPlus to refresh all repos, update unpinned cogs, reload changed loaded cogs and sync enabled slash commands. |
| `aria_red_update_status` | Check whether an update is queued and the latest update result. |

An accepted update is queued work, not a completed update. GPT must check its result. If an update request times out, check status before retrying. These four original tools accept no arguments. The additional management tools use typed container names, template filenames, installed script names and job IDs. They do not expose an arbitrary host shell endpoint.

## 1. Create the account connection

In [OpenAI Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels), create a tunnel for Aria and associate it with your organization and the ChatGPT workspace you use. Save its `tunnel_…` ID.

Create a separate runtime key in [API key settings](https://platform.openai.com/settings/organization/api-keys). The runtime principal needs **Tunnels: Read and Use**; creating/managing tunnels additionally needs **Manage**. Use a runtime key, not an admin key. The bridge does not call a model API.

Keep the credentials on Aria. The installer below asks for them privately in the host terminal. If the tunnel option is unavailable in your account/workspace, account access must be resolved before this deployment can connect; do not substitute a public URL for the tunnel ID.

Official setup and workspace requirements: [Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

## 2. Enable the existing Red update listener

Run these as the bot owner in the Discord channel where update results should appear:

```text
!cog update True downloaderplus
!download webhook enable
!download webhook link
```

Your existing setup can be reused. Only if the bot says setup is missing, run `!download webhook setup 8766 0.0.0.0` and then enable it. Setup rotates the old credentials.

The bot DMs a private link containing `#token=` followed by 64 hexadecimal characters. The bridge needs **only those 64 characters**, not the whole URL and not the separate GitHub webhook signing secret. Red's container must publish LAN port `8766` to its own port `8766`, unless it already uses host networking. No Internet port forwarding is needed.

## 3. Install on Aria

Use the Unraid host terminal. Put a checkout of this repository at `/mnt/user/appdata/aria-gpt-bridge/source`. If the repository is private, use your existing GitHub authentication or copy the downloaded repository there; do not place an access token in a clone URL.

For a first checkout with Git authentication already configured:

```bash
mkdir -p /mnt/user/appdata/aria-gpt-bridge
git clone https://github.com/kevinwaynekelly/aria-gpt-bridge.git /mnt/user/appdata/aria-gpt-bridge/source
cd /mnt/user/appdata/aria-gpt-bridge/source
bash tools/aria_bridge/run-unraid.sh tunnel_YOUR_ID
```

Replace `tunnel_YOUR_ID` with the ID from step 1. The script privately prompts for the runtime key and Red update token, writes protected credential files, takes an initial container snapshot, builds the image, installs the local management service and starts `aria-gpt-bridge`. Existing nonempty credential files are reused. It requires Unraid's Docker, native PHP with SimpleXML, Git, `jq`, `timeout`, `flock`, `setsid` and Bash.

The image uses Python 3.11 and the SHA256-verified official `tunnel-client` **v0.0.16** Linux AMD64 archive. Runtime UID/GID is `65532:65532`. It has a read-only root filesystem, no Docker socket, no published ports, limited memory/processes/log size and an `unless-stopped` restart policy. The private host-management socket is mounted separately. Docker on Unraid must be enabled after boot for the container to restart. No startup package download is required.

Check startup:

```bash
docker logs --tail 50 aria-gpt-bridge
docker inspect --format '{{.State.Status}} / {{.State.Health.Status}}' aria-gpt-bridge
```

Wait for `running / healthy`. Container health checks test tunnel readiness and, when configured, host-agent connectivity. They do not prove that Red updates or a particular container operation succeed. Check `aria_management_capabilities` and the tool calls in step 5 separately.

## 4. Refresh fallback container status

Management-enabled installations list containers live. The snapshot below remains useful for snapshot-only deployments or after management is deliberately disabled.

The [Unraid User Scripts repository](https://github.com/kevinwaynekelly/unraid-userscripts) includes the standalone job `1_aria_gpt_container_status`. Install it by running your existing `3_pull_github_repo` job, or run this in the Unraid host terminal:

```bash
bash /boot/config/plugins/user.scripts/scripts/3_pull_github_repo/script
```

Refresh the User Scripts page, run `1_aria_gpt_container_status` once and schedule it every minute with custom cron `* * * * *`. No hand-created wrapper or Kevin's Cogs checkout dependency is needed for this scheduled job. The updater preserves existing schedules and does not automatically enable new scripts.

The job only invokes `docker ps`, projects four status fields and atomically replaces `status/containers.json`. Failed refreshes preserve the previous snapshot and use the shared Unraid failure notification system. The bridge reports snapshot age and marks it stale after five minutes, so old values cannot appear to be fresh. The bundled `export-containers.sh` remains available for initial installation and hosts that do not use the script repository.

Host load/memory/uptime are read live. The snapshot contains no container environment variables, mounts or secrets. It is bounded to 1 MiB; the bridge returns up to 100 containers.

## 5. Add the tools to ChatGPT

While the bridge is healthy, open ChatGPT **Plugins → + → Add custom MCP server**. Select **Tunnel** as the connection, choose/paste the same tunnel ID and create the plugin named **Aria**. This MCP server has no additional application login; access is controlled by the tunnel's account/workspace permissions. Install/enable the plugin in the chat that will use it.

Test with:

1. “Use Aria to check the server and list containers.”
2. “Check the latest Red update status.”
3. When you want an actual update: “Update all my Red repos and cogs, then check the result.”

The bridge must keep running for these calls. Use **one active bridge per tunnel ID**. Overlapping stdio tunnel clients with the same ID are unsupported. The private tunnel is for your account/workspace, not a public plugin submission.

## Upgrade, stop and revoke

Once this version is installed and its tools are available, ask ChatGPT to update the Aria bridge. `aria_bridge_update` runs the fixed repository upgrade in a separate host process, and `aria_bridge_update_status` reports its durable result after reconnection. The operation preserves existing connection settings and checks replacement health. It requires a clean, fast-forwardable `main` checkout and an idle management queue. See [remote bridge upgrades](MANAGEMENT.md#upgrade-the-bridge-remotely) for recovery and tool-refresh behavior.

For the initial upgrade to this version, or local recovery, use the installer:

```bash
cd /mnt/user/appdata/aria-gpt-bridge/source
git pull --ff-only
bash tools/aria_bridge/upgrade-unraid.sh
```

The installer reuses credentials and recreates the container with the current settings. It rebuilds the image using Docker's cache and retains the previous bridge until the replacement is healthy. To stop access temporarily, run `docker stop aria-gpt-bridge`. Revoke the runtime key or disable the tunnel in Platform to revoke account access. Disable Red updates separately with `!download webhook disable`. See [MANAGEMENT.md](MANAGEMENT.md) for stopping or removing the native host service.

The image build makes runtime code readable regardless of the checkout's file permissions,
then imports the server as UID `65532`. A failed import stops the upgrade before the working
container is stopped. If the replacement later fails startup, the installer saves its state
and a bounded log tail to `management/upgrade-failure.*.log` before rollback. These files are
root-private and may include sensitive provider output; inspect them locally.

If Red credentials change, replace `secrets/red_update_token` locally, retain owner `65532:65532` and mode `0400`, then **recreate** the container. Recreating also picks up atomically replaced file bind mounts. Do not run another `webhook setup` unless you intend to rotate its credentials.

## Compose alternative and configuration

`compose.yaml` provides the same service for hosts with Docker Compose. Use it instead of the installer, not alongside it. Copy `.env.example` to `.env`, supply the tunnel ID, create the two secret files and initial snapshot, then run `docker compose up -d --build` from this directory. The secret files must be readable by UID `65532` and should have mode `0400`. Their directory may remain root-only because the files are mounted individually.

The base Compose file uses snapshot-only access. To enable management, start the native host service and install its boot hook, then include the management override:

```bash
bash host-service.sh start
bash host-service.sh install-boot
docker compose -f compose.yaml -f compose.management.yaml up -d --build
```

| Setting | Default |
| --- | --- |
| `CONTROL_PLANE_TUNNEL_ID` | Required account-provided ID |
| `ARIA_RED_URL` | `http://10.10.1.200:8766` |
| `ARIA_RED_TOKEN_FILE` | `/run/secrets/red_update_token` |
| `ARIA_HOST_ROOT` | `/host` |
| `ARIA_DOCKER_SNAPSHOT_FILE` | `/status/containers.json` in deployment |
| `ARIA_AGENT_SOCKET` | Empty for snapshot-only access; `/run/aria-agent/agent.sock` for management |
| `ARIA_APPDATA_ROOT` | `/mnt/user/appdata/aria-gpt-bridge` for host scripts |

Read-only metrics use only the three selected `/proc` files and the Unraid version file. Management adds the private host-service socket directory. Neither deployment mounts the Docker socket. The stdio server can alternatively query an administrator-configured `ARIA_DOCKER_SOCKET` through one fixed read endpoint, but that is not used by the supplied deployments. A Docker socket mounted `:ro` still permits Docker API writes.

For a different appdata path, set `ARIA_APPDATA_ROOT` consistently for the installer and scheduled exporter. For another Red address, set `ARIA_RED_URL` when creating the container. Secrets are never command arguments, repository files, tool results or arbitrary provider error text. The tunnel client's health endpoint stays on container loopback at port 8080.

## Verification limits

Regression tests exercise real local HTTP, snapshot files and stdio subprocesses, with mocked Discord and remote provider boundaries. They cover protocol discovery, fixed endpoints, credential handling, redirects, malformed responses, input/output limits and update acknowledgement. The upstream release checksum and binary CLI were checked. This environment cannot build a Docker image or connect to your live Unraid/ChatGPT account; installation, tunnel readiness and live Red calls must be verified on Aria.


## Expanded management and dedicated repository

Version 2 adds host/container execution, version-checked scripts and files, secret references,
application integrations, VM management, diagnostics, recoverable deployment workflows and
host-native scheduling. See [FEATURES.md](FEATURES.md) for supported tools and limits.

Existing installations from `kevin-cogs` should use [STANDALONE.md](STANDALONE.md) for the
one-time repository migration. The old checkout is retained for recovery. Refresh the Aria
plugin after installation so the new MCP tool catalog is discovered.
