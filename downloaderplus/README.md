# DownloaderPlus

The shared theme, owner-only prefix/slash controls and optional GitHub webhook updates for bundled Red Downloader. Red keeps its repository copies, installed-package records, dependency installer, converters, installation agreement and reload behavior.

## Install

```text
[p]load downloader
[p]cog install kevin-cogs downloaderplus
[p]load downloaderplus
[p]slash enablecog downloaderplus
[p]slash sync
```

Use your configured repository name if it differs from `kevin-cogs`. Requires Red 3.5.24+ and Python 3.10/3.11, with no additional packages. Native Downloader must be loaded for operations; DownloaderPlus can load first and reports an unavailable source clearly. Optional CorePlus combines native Downloader commands with the DownloaderPlus help category.

## Commands

Every new control requires bot ownership. Server administrator permission alone is insufficient. Root commands have a slash fallback: `[p]download` is `/download status`, and `[p]download repos` is `/download repos list`.

| Text | Slash | Purpose |
| --- | --- | --- |
| `[p]download` | `/download status` | Repository, installed-cog and pinned counts. |
| `[p]download help` | `/download help` | Command help. |
| `[p]download repos` | `/download repos list` | List configured repositories. |
| `[p]download repos add <name> <url> [branch]` | `/download repos add` | Add a repository through Red's installation agreement. |
| `[p]download repos remove <names>` | `/download repos remove` | Remove space-separated repositories. |
| `[p]download repos info <name>` | `/download repos info` | Repository information. |
| `[p]download repos update [names]` | `/download repos update` | Refresh repository copies, all if omitted. |
| `[p]download available <repo>` | `/download available` | Packages available in a repository. |
| `[p]download installed` | `/download installed` | Installed packages, repository names and pinned state. |
| `[p]download info <repo> <package>` | `/download info` | Package metadata. |
| `[p]download install <repo> <packages>` | `/download install` | Install space-separated packages; load afterward. |
| `[p]download uninstall <packages>` | `/download uninstall` | Native uninstall and unload handling. |
| `[p]download update [reload] [packages]` | `/download update` | Update selected packages or all unpinned packages. Reload defaults to true. |
| `[p]download checkupdates` | `/download checkupdates` | Check without installing updates. |
| `[p]download pin <packages>` | `/download pin` | Exclude selected installed packages from normal updates. |
| `[p]download unpin <packages>` | `/download unpin` | Enable normal updates for them. |
| `[p]download pinned` | `/download pinned` | Show pinned packages. |
| `[p]download version <repo> <revision> [reload] [packages]` | `/download version` | Update installed packages from a particular revision. |
| `[p]download find <command>` | `/download find` | Find a command's installed package and repository. |
| `[p]download webhook` | `/download webhook status` | Listener, accepted repositories/branches and latest update result. |
| `[p]download webhook setup [port] [bind]` | `/download webhook setup` | Prepare a listener in this result channel and DM a generated private secret. Disabled until enabled. |
| `[p]download webhook enable` | `/download webhook enable` | Start signed push updates and choose this channel for native results. |
| `[p]download webhook disable` | `/download webhook disable` | Stop the listener and cancel its queued worker. |

For example, `[p]download update True audioplus communityplus` updates and reloads those installed packages. `[p]download version kevin-cogs origin/main True audioplus` uses that repository's main revision. Pinning, compatibility/dependency failures and revision errors are reported by the original engine. Review repository code before accepting Red's native installation agreement; DownloaderPlus does not automatically accept it.

## GitHub webhook updates

A signed **push to the tracked branch of any installed GitHub repository** triggers a normal update of **every configured repository and every unpinned installed cog**. Changed loaded packages reload automatically. This does not install additional packages, unpin cogs, force a revision or accept a new repository's installation agreement. Other GitHub events, other branches, tag pushes, branch deletions and repositories not already configured in Red are ignored.

Run these in the private channel where update results should appear:

```text
[p]download webhook setup 8766 0.0.0.0
[p]download webhook enable
[p]download webhook
```

Setup sends the secret by DM and stores it in Red's persistent Config. If DMs fail, existing settings are preserved. Running setup again rotates the secret and stops automation; update the GitHub webhook's secret before enabling again.

In Unraid's Red container settings, publish **TCP port 8766** to container port **8766**, unless the container already uses host networking. Configure your HTTPS reverse proxy to forward your webhook URL to `http://10.10.1.200:8766/github`. The public endpoint must be reachable by GitHub; a private LAN or tailnet address alone is insufficient. Keep the body and signature headers unchanged. Port 8765 remains available for DashboardPlus.

In each installed GitHub repository whose pushes should trigger updates, open **Settings → Webhooks → Add webhook** and set:

| GitHub setting | Value |
| --- | --- |
| Payload URL | Your publicly reachable HTTPS URL ending in `/github`. |
| Content type | `application/json`. |
| Secret | The private secret sent by DownloaderPlus. |
| Events | Just the push event. |
| SSL verification | Enabled. |

The signed GitHub ping returns `200` when the listener is ready. Matching pushes return `202 queued` immediately; that acknowledgement means the update was queued, not that installation succeeded. Red's native update and reload messages go to the selected channel, and webhook status keeps the latest outcome. Listener, update and reload failures emit errors for optional NotificationPlus/Unraid alerts.

The endpoint verifies HMAC-SHA256 with a constant-time comparison, accepts up to 1 MiB of uncompressed JSON and eight concurrent request readers, and retains at most 256 delivery IDs/body digests for duplicate protection. It never stores payloads, commits, senders or repository access tokens. Push bursts coalesce for three seconds into one worker with at most one waiting followup; checks start no more often than every 30 seconds, including after restart. A previously interrupted update is retried after load. Self-reloads preserve the listener and a shared update lock, and DownloaderPlus reloads last.

The configuring user must still be a bot owner and present in the result channel's server. Native command disabling and cog checks apply when an update executes. Keep the channel private if repository names or installation reports should be private. Addon/manual overlay updates share the worker's lock; avoid running native `[p]cog` or `[p]repo` mutation commands concurrently with automation. Disable stops the listener and cancels queued/active Python work; an already started Git/Pip process in Red's native executor may finish. An already completed installation remains installed.

## Native behavior

Existing `[p]repo`, `[p]cog`, `[p]findcog` and `[p]pipinstall` replies sent through their Context use the shared theme when this addon is enabled. Their commands, checks and arguments remain unchanged. New controls invoke the original parser, full command/parent/cog checks, hooks, cooldowns and error handlers. Disabling a native source command also blocks its new control. The new public `download` group is owner-only, including its `find` shortcut; native `findcog` retains its existing access policy.

An update's reload option follows Red's native behavior. Repository-copy updates alone do not install changed cog files. Installation does not load a package automatically. Unloading DownloaderPlus removes only its commands and removable styling hook; bundled Downloader and its database remain intact.

## Data and validation

DownloaderPlus retains optional webhook listener settings, a private secret, the configuring owner's ID, result-channel ID, bounded delivery IDs/body digests, one pending flag and the latest safe update result. Red Downloader remains responsible for repository copies and installed-package data. The configuring owner's user-data export includes their ID, result channel and enabled state; it excludes the secret and delivery digests. Deleting that owner's data cancels/waits in-flight setup or enable operations, stops the listener and clears webhook configuration. Settings-only exports include only the enabled/bind/port policy and exclude credentials and runtime records.

Regression tests use real HTTP signatures and Red parsing/global checks, with Git/package installation and Discord transport mocked. They check tampered/oversized requests, allowlisted branches, duplicate delivery across restart, burst coalescing, interrupted recovery, pinning, failed dependency reports, self-reload locking, owner revocation and deletion races. They do not install packages on a live bot or expose a live public webhook.
