# Aria GPT bridge

Connect ChatGPT to the Unraid host at `10.10.1.200` through an outbound OpenAI Secure MCP Tunnel. This is a separate Docker service, not a Red cog. It opens no public port and does not require Pushover, a Discord webhook or a public reverse proxy.

| GPT tool | Behavior |
| --- | --- |
| `aria_status` | Read host uptime, load, available memory and Unraid version. |
| `aria_containers` | Read container names, images and states from a small host-generated snapshot; report its age and whether it is stale. |
| `aria_update_red` | Ask DownloaderPlus to refresh all repos, update unpinned cogs, reload changed loaded cogs and sync enabled slash commands. |
| `aria_red_update_status` | Check whether an update is queued and the latest update result. |

An accepted update is queued work, not a completed update. GPT must check its result. If an update request times out, check status before retrying. These tools do not accept shell commands, paths, URLs or container names as arguments.

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
git clone https://github.com/kevinwaynekelly/kevin-cogs.git /mnt/user/appdata/aria-gpt-bridge/source
cd /mnt/user/appdata/aria-gpt-bridge/source
bash tools/aria_bridge/run-unraid.sh tunnel_YOUR_ID
```

Replace `tunnel_YOUR_ID` with the ID from step 1. The script privately prompts for the runtime key and Red update token, writes protected credential files, takes an initial container snapshot, builds the image and starts `aria-gpt-bridge`. Existing nonempty credential files are reused. It requires Docker, `jq`, `timeout` and Bash on the Unraid host.

The image uses Python 3.11 and the SHA256-verified official `tunnel-client` **v0.0.16** Linux AMD64 archive. Runtime UID/GID is `65532:65532`. It has a read-only root filesystem, no Docker socket, no published ports, limited memory/processes/log size and an `unless-stopped` restart policy. Docker on Unraid must be enabled after boot for the container to restart. No startup package download is required.

Check startup:

```bash
docker logs --tail 50 aria-gpt-bridge
docker inspect --format '{{.State.Status}} / {{.State.Health.Status}}' aria-gpt-bridge
```

Wait for `running / healthy`. Health checks test tunnel readiness; they do not prove that Red or the snapshot is usable. Tool calls in step 5 check those separately.

## 4. Refresh container status

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

To upgrade the bridge code, update the checkout and build successfully before stopping the old container:

```bash
cd /mnt/user/appdata/aria-gpt-bridge/source
git pull --ff-only
docker build --pull -t aria-gpt-bridge:v1 tools/aria_bridge
docker stop aria-gpt-bridge
docker rm aria-gpt-bridge
bash tools/aria_bridge/run-unraid.sh tunnel_YOUR_ID
```

The installer reuses credentials and recreates the container with the current settings. It rebuilds the image using Docker's cache. To stop access temporarily, run `docker stop aria-gpt-bridge`. Revoke the runtime key or disable the tunnel in Platform to revoke account access. Disable Red updates separately with `!download webhook disable`.

If Red credentials change, replace `secrets/red_update_token` locally, retain owner `65532:65532` and mode `0400`, then **recreate** the container. Recreating also picks up atomically replaced file bind mounts. Do not run another `webhook setup` unless you intend to rotate its credentials.

## Compose alternative and configuration

`compose.yaml` provides the same service for hosts with Docker Compose. Use it instead of the installer, not alongside it. Copy `.env.example` to `.env`, supply the tunnel ID, create the two secret files and initial snapshot, then run `docker compose up -d --build` from this directory. The secret files must be readable by UID `65532` and should have mode `0400`. Their directory may remain root-only because the files are mounted individually.

| Setting | Default |
| --- | --- |
| `CONTROL_PLANE_TUNNEL_ID` | Required account-provided ID |
| `ARIA_RED_URL` | `http://10.10.1.200:8766` |
| `ARIA_RED_TOKEN_FILE` | `/run/secrets/red_update_token` |
| `ARIA_HOST_ROOT` | `/host` |
| `ARIA_DOCKER_SNAPSHOT_FILE` | `/status/containers.json` in deployment |
| `ARIA_APPDATA_ROOT` | `/mnt/user/appdata/aria-gpt-bridge` for host scripts |

Only the three selected `/proc` files and Unraid version file are mounted from the host. The stdio server can alternatively query a locally configured `ARIA_DOCKER_SOCKET` through one fixed read endpoint, but neither supplied deployment mounts a socket. A Docker socket mounted `:ro` still permits Docker API writes; use the snapshot deployment for ordinary use.

For a different appdata path, set `ARIA_APPDATA_ROOT` consistently for the installer and scheduled exporter. For another Red address, set `ARIA_RED_URL` when creating the container. Secrets are never command arguments, repository files, tool results or arbitrary provider error text. The tunnel client's health endpoint stays on container loopback at port 8080.

## Verification limits

Regression tests exercise real local HTTP, snapshot files and stdio subprocesses, with mocked Discord and remote provider boundaries. They cover protocol discovery, fixed endpoints, credential handling, redirects, malformed responses, input/output limits and update acknowledgement. The upstream release checksum and binary CLI were checked. This environment cannot build a Docker image or connect to your live Unraid/ChatGPT account; installation, tunnel readiness and live Red calls must be verified on Aria.
