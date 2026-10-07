# DownloaderPlus

The shared theme, owner-only prefix/slash controls, approved Discord webhook messages, private update URLs, optional signed GitHub pushes and daily updates for bundled Red Downloader. Red keeps its repository copies, installed-package records, dependency installer, converters, installation agreement and reload behavior.

## Install

```text
[p]load downloader
[p]cog install kevin-cogs downloaderplus
[p]load downloaderplus
[p]slash enablecog downloaderplus
[p]slash sync
```

Use your configured repository name if it differs from `kevin-cogs`. Requires Red 3.5.24+ and Python 3.10/3.11, with no additional packages. Native Downloader must be loaded for operations; DownloaderPlus can load first and reports an unavailable source clearly. Optional CorePlus combines native Downloader commands with the DownloaderPlus help category.

## Update everything

```text
!updateall
```

Or use `/updateall`. Both are bot-owner only and refresh **all configured
repositories**, update **all unpinned installed cogs** and reload changed loaded
cogs in one native Red update pass, then sync **enabled global slash commands**. Repositories without installed cogs are still
refreshed. No GitHub webhook is needed. Pinned cogs stay pinned, new packages are
not installed, and native dependency/check failures remain visible. This updates
Downloader-managed packages, not Red itself or its bundled core cogs.

`!download updateall` and `/download updateall` provide the same action under
the management group. These commands share the Discord/webhook/daily update lock, so automatic
and manual update operations do not run over each other. The shortcut respects disabled
`download`, `download updateall` and native `cog update` commands.

Slash sync happens after reload and preserves Red's enabled/disabled command selections; it does not enable every installed cog. It also checks the native `slash sync` permissions and disabled state. Unchanged command definitions skip a redundant upload, and changed/failed uploads share a persisted one-minute retry budget. A sync failure does not undo installed updates. Normal `download update` still uses its existing selected-package behavior.

## Discord webhook updates

Use an existing **incoming Discord webhook** in the channel where update results should appear. Copy the webhook's numeric ID from its private URL, the digits immediately after `/webhooks/`. Keep the full webhook URL private. As the bot owner, run in that webhook's server text channel:

```text
!download discord enable WEBHOOK_ID
!download discord
```

Replace `WEBHOOK_ID` with the numeric ID. Slash equivalents are `/download discord enable` and `/download discord status`. The bot needs **View Channel**, **Send Messages** and **Manage Webhooks** there. Red needs server message and Message Content intents enabled. The bot checks the webhook through Discord before saving its exact webhook/server/channel binding; ordinary members cannot authorize it.

Send this JSON as an HTTP POST to the existing private Discord webhook URL:

```json
{"content":"updateall","allowed_mentions":{"parse":[]}}
```

Discord delivers the message to Red through its normal connection. No public bot URL, incoming router port, Docker port mapping or reverse proxy is needed. A normal person typing `updateall`, another webhook using the same name, or a message in another channel cannot activate this trigger. The webhook accepts exactly `updateall` or `!updateall`, ignoring surrounding whitespace and case. It does not run arbitrary commands, add repositories or change pins. Prefix commands posted by webhook accounts are not otherwise enabled.

The approved message queues one bounded worker. Bursts coalesce for three seconds, with at most one waiting followup and at least 30 seconds between starts. The latest accepted Discord message ID prevents replay across reloads. Updates use the configuring owner's current permissions, refresh all configured repositories and unpinned installed cogs, reload changed loaded cogs, then sync enabled slash commands. Results appear in the same channel, and failures reach optional NotificationPlus. The owner must remain a bot owner and a member of that server.

```text
!download discord disable
!download discord enable
```

Disable stops this trigger and its queued worker without deleting the Discord webhook or changing daily/HTTP automation. Enable without an ID reuses the saved binding, provided it still belongs to the current channel. To replace it, enable a different webhook ID in that webhook's channel. To revoke the webhook URL itself, delete that webhook in Discord. Store the URL privately in whichever external service sends requests; DownloaderPlus saves IDs and bounded runtime status, never the URL/token or message contents. Connecting a webhook does not itself give ChatGPT permission or a tool to send HTTP requests; the client must have a supported way to POST to Discord.

## Private update link

In your private Discord result channel, run:

```text
!download webhook setup 8766 0.0.0.0
!download webhook enable
!download webhook link
```

The bot DMs a private link starting with `http://10.10.1.200:8766/update#token=...`. **Opening that full link in a browser requests an update**, reloads changes and syncs enabled slash commands. No GitHub webhook registration is needed for this link. The page displays queued/running/completed status; detailed native results go to the selected Discord channel.

In Unraid, edit **red-discordbot** and map **host TCP 8766 → container TCP 8766** if using bridge networking, then apply. Host networking does not need a port mapping. DashboardPlus keeps port 8765. The LAN address is reachable only from your LAN or a connected private network; publishing this code alone does not activate the listener or configure Docker.

For remote access, forward `/update` and `/update/status` through a reachable HTTPS reverse proxy to `http://10.10.1.200:8766`, then run `!download webhook link https://YOUR-UPDATE-HOST`. Use a dedicated root host, without a path or login. A remote service can send an empty `POST /update` with `Authorization: Bearer TOKEN`, using the token from the private link, and poll `GET /update/status` with the same header. A plain GET, HEAD, link preview or bare `/update` URL does not update anything. Browser JavaScript reads the fragment, removes it from browser history and sends the credential in the POST header; fragments are not sent in HTTP URLs. Prerendered pages wait until activated.

Treat the full link as a credential: anyone holding it can request these owner-authorized updates. It cannot select repositories, install new packages or run arbitrary commands. Setup/link send it only by DM, and the HTTP page/status never expose it. GitHub signing and manual bearer credentials are distinct; running setup again rotates both, disables the listener and invalidates old links. `download webhook disable` stops both endpoints. No webhook payloads or private links are logged by the listener.

## Daily updates

Run in the Discord channel where results should appear:

```text
!download daily enable
!download daily
```

The initial schedule is **04:00 America/Chicago**. It updates every configured repository and unpinned installed cog, reloads changed loaded cogs and syncs enabled slash commands. It does not require the webhook listener or an exposed port. Change the schedule or turn it off with:

```text
!download daily enable 03:30 America/Chicago
!download daily disable
```

Slash equivalents are `/download daily enable`, `/download daily status` and `/download daily disable`; the time option is named `clock`. Omitting time/timezone reuses saved choices. The schedule survives reloads/restarts, uses local calendar days and follows daylight saving changes. A skipped clock time moves forward by the DST gap; a repeated clock time runs on its first occurrence. Missed days coalesce into one run when the cog starts again.

Before an attempt starts, its next daily run is saved. An interrupted installation is not blindly repeated on restart; check the last result and run `!updateall` if needed. A failed attempt is reported and retried on the next scheduled day. Daily updates are disabled until an owner enables them and selects a result channel. The configuring user must remain a bot owner and a member of that channel's server. Unload/disable cancels the worker; a Git/Pip job already running in Red's executor may still finish. Optional NotificationPlus receives scheduler/update/sync errors through the existing error logger.

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
| `[p]updateall` | `/updateall` | Refresh all repos, update unpinned cogs, reload changes and sync enabled slash commands. |
| `[p]download updateall` | `/download updateall` | The same complete update under the management group. |
| `[p]download checkupdates` | `/download checkupdates` | Check without installing updates. |
| `[p]download pin <packages>` | `/download pin` | Exclude selected installed packages from normal updates. |
| `[p]download unpin <packages>` | `/download unpin` | Enable normal updates for them. |
| `[p]download pinned` | `/download pinned` | Show pinned packages. |
| `[p]download version <repo> <revision> [reload] [packages]` | `/download version` | Update installed packages from a particular revision. |
| `[p]download find <command>` | `/download find` | Find a command's installed package and repository. |
| `[p]download webhook` | `/download webhook status` | Listener, accepted repositories/branches and latest update result. |
| `[p]download webhook setup [port] [bind] [base_url]` | `/download webhook setup` | Prepare a listener and DM a private trigger link/GitHub secret. Disabled until enabled. |
| `[p]download webhook link [base_url]` | `/download webhook link` | DM the current private update URL, optionally using an HTTPS proxy address. |
| `[p]download webhook enable` | `/download webhook enable` | Start private URL and signed push updates; use this channel for results. |
| `[p]download webhook disable` | `/download webhook disable` | Stop the listener and cancel its queued worker. |
| `[p]download daily` | `/download daily status` | Show the schedule, next run and latest result. |
| `[p]download daily enable [clock] [timezone]` | `/download daily enable` | Enable/change daily updates and use this channel for results. |
| `[p]download daily disable` | `/download daily disable` | Disable daily automation and cancel its worker. |
| `[p]download discord` | `/download discord status` | Show the approved Discord webhook, channel and latest result. |
| `[p]download discord enable [webhook_id]` | `/download discord enable` | Approve an existing incoming webhook in this channel; reuse its saved ID if omitted. |
| `[p]download discord disable` | `/download discord disable` | Stop this trigger without deleting the Discord webhook. |

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

Temporary settings-storage errors do not stop the update worker. Accepted work stays pending until its start is saved, and retries are delayed to avoid a busy loop. A failure to save an update's error status is logged separately; later requests can still run once storage recovers.

The endpoint verifies HMAC-SHA256 with a constant-time comparison, accepts up to 1 MiB of uncompressed JSON and eight concurrent request readers, and retains at most 256 delivery IDs/body digests for duplicate protection. It never stores payloads, commits, senders or repository access tokens. Push bursts coalesce for three seconds into one worker with at most one waiting followup; checks start no more often than every 30 seconds, including after restart. A previously interrupted update is retried after load. Self-reloads preserve the listener and a shared update lock, and DownloaderPlus reloads last.

The configuring user must still be a bot owner and present in the result channel's server. Native command disabling and cog checks apply when an update executes. Keep the channel private if repository names or installation reports should be private. Addon/manual overlay updates share the worker's lock; avoid running native `[p]cog` or `[p]repo` mutation commands concurrently with automation. Disable stops the listener and cancels queued/active Python work; an already started Git/Pip process in Red's native executor may finish. An already completed installation remains installed.

## Native behavior

Existing `[p]repo`, `[p]cog`, `[p]findcog` and `[p]pipinstall` replies sent through their Context use the shared theme when this addon is enabled. Their commands, checks and arguments remain unchanged. New controls invoke the original parser, full command/parent/cog checks, hooks, cooldowns and error handlers. Disabling a native source command also blocks its new control. The new public `download` group is owner-only, including its `find` shortcut; native `findcog` retains its existing access policy.

An update's reload option follows Red's native behavior. Repository-copy updates alone do not install changed cog files. Installation does not load a package automatically. Unloading DownloaderPlus removes only its commands and removable styling hook; bundled Downloader and its database remain intact.

## Data and validation

DownloaderPlus retains optional webhook listener settings, a private secret, the configuring owner's ID, result-channel ID, bounded delivery IDs/body digests, one pending flag and the latest safe update result. The manual trigger credential is derived from the secret. Daily automation stores its time/timezone, owner/channel IDs, generation, next run and latest result. Discord triggers retain approved webhook/server/channel/owner IDs, generation, one latest-message cursor, pending state and latest result, without webhook URLs/tokens or message contents. Slash sync stores only a schema fingerprint and two timestamps. Red Downloader remains responsible for repository copies and installed-package data. User-data exports include the requesting owner's ID, result channels, enabled states and Discord webhook/server bindings; they exclude credentials and replay digests. Deleting that owner's data cancels/waits in-flight configuration, stops their automation and clears its settings, preserving another owner's automation. Settings-only exports include webhook enabled/bind/port, daily enabled/time/timezone and Discord-trigger enabled state; they exclude credentials, identities and runtime records. Additive defaults preserve existing webhook configuration without changing its secret.

Regression tests use real local HTTP signatures and Red parsing/global checks, with Git/package installation and Discord transport mocked. They check Discord webhook identity/permissions, bounded message admission and replay, signed and private triggers, non-mutating page previews, credential isolation, burst coalescing, schedule claims/DST/restarts, slash enable choices/rate budgets, pinning, dependency failures, self-reload locking, owner revocation and deletion races. They do not install packages on a live bot or expose a live public webhook.
