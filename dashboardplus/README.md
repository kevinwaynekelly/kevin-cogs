# DashboardPlus

A private web dashboard hosted inside Red's process. It follows the suite's indigo design, works on phones and desktops, and needs no frontend build, Node.js service, Discord OAuth application or hosted website. Each cog remains separately installable; DashboardPlus reads loaded cog APIs at runtime.

## Install and open

```text
[p]cog install kevin-cogs dashboardplus
[p]load dashboardplus
[p]dashboard start
[p]dashboard login
```

Use your configured repository name if it differs from `kevin-cogs`, and replace `[p]` with the actual prefix, such as `!`. Requires Red 3.5.24+ and Python 3.10/3.11. It uses aiohttp already included with Red. The server initially stays off; starting it also saves automatic startup for the next cog load or bot reboot.

On the same host as Red, open **http://127.0.0.1:8765**. Paste the single-use login code sent to you by DM. Codes expire after five minutes. You must be a current bot owner; Discord server administrator access alone does not grant dashboard access. Join a server with that owner account to manage it, and select a text channel both you and Scarlet can read and send in. This channel supplies the existing commands' permission context; command replies appear in the dashboard.

Optional slash controls:

```text
[p]slash enablecog dashboardplus
[p]slash sync
```

`/dashboard login` sends the code in a private ephemeral reply, useful when DMs are disabled. No code, password or bot token appears in a public channel. Issuing a new code replaces your previous unused code. Existing sessions remain active until logout, expiry or revocation.

## Docker / another device on your network

Set these commands once as the owner:

```text
[p]dashboard stop
[p]dashboard bind 0.0.0.0 8765
[p]dashboard url http://10.10.1.200:8765/
[p]dashboard start
```

Publish the port in the Red container configuration. For Compose, add the mapping to your **existing Red service**:

```yaml
ports:
  - "8765:8765"
```

For a `docker run` deployment add `-p 8765:8765`; for a Docker management UI add TCP container port `8765` mapped to host port `8765`. Recreate the container after changing port mappings, preserving its existing Red `/data` volume. The dashboard's own bind/start policy is in that volume with Red Config, so it survives updates and reboots. No startup pip or apt installation is needed for DashboardPlus.

For Red on `10.10.1.200`, open [http://10.10.1.200:8765/](http://10.10.1.200:8765/) from your phone or another device. Use the IP of the machine running Red, rather than loopback or the container's internal address. If your host differs, use its address in the saved URL too. `[p]dashboard url <address>` controls the link shown in login/status; it does not bind the listener, publish a port or allow a hostname. Run `[p]dashboard url` without an address to restore the automatically derived listener link. Only root HTTP/HTTPS URLs without credentials, queries or fragments are accepted.

For a private DNS name or reverse proxy, allow that exact hostname:

```text
[p]dashboard host aria.monitor-pirate.ts.net
[p]dashboard url https://aria.monitor-pirate.ts.net/
```

Supply the hostname alone, without a scheme, port or path. The proxy must preserve the browser's Host header, route the dashboard at `/`, and forward requests to this listener. Same-origin HTTPS through a private proxy is supported, including secure cookies. Keep the dashboard on your private network/Tailscale or behind your existing authenticated HTTPS access; it controls owner-authorized bot features. For loopback behind a same-host proxy, the default bind can stay `127.0.0.1`.

## What is available

| Page | Controls |
| --- | --- |
| Overview | Bot name, gateway latency, loaded-cog count, connected servers, current track with thumbnail and shortcuts. |
| Music | Public source link/artwork, progress, voice/listener state, up to 100 upcoming tracks, queue by search/URL, pause/resume, skip, stop/clear, shuffle, repeat, volume and remove. API also supports the original seek command. |
| Settings | 32 reviewed editors across AudioPlus, CommunityPlus, LevelPlus, LogPlus, EmojiStealerPlus, OwoPlus, IntroPlus and PresencePlus. Hover, focus or tap each help control for its effect and limits. Only loaded, currently permitted commands appear. Presence settings explicitly apply bot-wide. |
| Data | 33 searchable, paginated datasets across all fourteen suite cogs. Choose a view from the Cog data selector, search it, or use Previous/Next and Refresh. |
| Cogs | All loaded cogs, including Red's native packages, and their enabled state in the selected server. |

Music refreshes from the server every five seconds while the page is visible; the browser animates progress between refreshes. This does not increase the three-second Discord now-playing edit interval. Artwork comes from original public video metadata/pages; resolved audio-stream URLs and headers never appear in the API.

Settings cover music panels, skip votes, fair queue, autoplay, history, request limits, recovery and normalization; sticky roles, welcomes, activity tracking and solo voice timeout; XP sources/announcements/multiplier; log destination, style and retained-history policy; emoji capture/reactions/notices; automatic transformations/chance; intro enable/volume/cooldown; and global presence enable/interval. Changes use the actual source commands and their parsers, checks, hooks, cooldowns and validation. Their normal cache/timer/player refreshes still run. Optional SettingsHub configuration auditing attributes changes to the authenticated owner's Discord ID. The browser shows the resulting command reply; source features may still send their normal Discord music/session/event notices.

There is no generic command, Python, shell, package installer or raw Config editor in the web API. Use `/core` and `/download` in Discord for management. The Data page reads existing cog records on demand, preserving their source permissions and retention. Private chat exports, credentials, resolved media streams/headers, local file paths and complete database files are not exposed. Stop or unload the dashboard without stopping AudioPlus playback.

## Data for every cog

The original dashboard showed settings and current playback, so saved intros, activity and XP were absent. The Data page now provides reviewed views across all fourteen suite cogs. Unloaded or currently disabled data sources are unavailable. Changing the selected server or command-context channel repeats the relevant source checks; bot ownership does not bypass a source command's additional requirements.

Records load when you open, select, search, page or refresh a dataset. They are not fetched with every five-second music update. Pages contain 25 rows by default, with an API maximum of 100; the source's existing data remains intact. DashboardPlus adds no separate copy of member activity or XP and does not prune source databases. Data views are read-only; use the cogs' existing commands to change records.

| Cog | Read-only data |
| --- | --- |
| IntroPlus | Members with saved intros, source titles/links/artwork, clip duration/start and local-copy readiness. |
| CommunityPlus | Member activity totals, last seen/presence, recorded voice time, named-game activity, events, polls and temporary rooms. |
| LevelPlus | Member XP/levels, achievement/streak progress, season archives and configured reward roles. |
| AudioPlus | Recent listening history, personal playlists/favorites, shared playlists/suggestions and the queue checkpoint, with public track metadata. |
| LogPlus | Retained event history, incident cases and aggregate activity. |
| EmojiStealerPlus | Captured emoji mappings and Discord-hosted images. |
| OwoPlus | Personal overrides/opt-outs, dictionaries/custom styles, saved haiku and contests with entries/aggregate votes. |
| SettingsHub | Visible configuration change history with previous/new values and snapshot metadata. |
| BackupPlus | Snapshot inventory, structure counts and pending-create counts. Requires the source's Administrator check. |
| PresencePlus | Global saved profiles and schedules. |
| ExportPlus | Your export job progress and metadata. Chat archive contents and filesystem paths stay private. |
| CorePlus | Runtime status and loaded-cog inventory; CorePlus has no separate stored member data. |
| DownloaderPlus | Native repository and installed-package inventory, excluding credentials and paths. |
| DashboardPlus | Listener/startup/link status without codes, cookies, CSRF values or session hashes. |

The authenticated owner can browse saved music collections for members in the selected server, with their owner attribution; the view also requires the music administration and collection source checks. SettingsHub filters audit details using its existing visibility rules. Some cogs have global configuration or transient jobs rather than server member records; their views label that scope instead of inventing a new database. Empty datasets report that no records are available.

## Owner commands

| Text | Slash | Purpose |
| --- | --- | --- |
| `[p]dashboard` | `/dashboard status` | Listener, saved startup policy, URL and safe error type. |
| `[p]dashboard help` | `/dashboard help` | Setup/command help. |
| `[p]dashboard start` | `/dashboard start` | Start and save automatic startup. |
| `[p]dashboard stop` | `/dashboard stop` | Stop, revoke every login and disable automatic startup. |
| `[p]dashboard bind [address] [port]` | `/dashboard bind` | Set a stopped listener's IP/port; defaults to `127.0.0.1:8765`. |
| `[p]dashboard url [address]` | `/dashboard url` | Save the displayed root HTTP/HTTPS link; omit the address to reset it. Does not change the listener or allowed hosts. |
| `[p]dashboard host <name>` | `/dashboard host` | Allow a private DNS/proxy hostname, up to ten names. |
| `[p]dashboard unhost <name>` | `/dashboard unhost` | Remove an allowed hostname. |
| `[p]dashboard login` | `/dashboard login` | Private single-use five-minute login code. |
| `[p]dashboard logout` | `/dashboard logout` | Revoke all of your browser sessions and unused codes. |

The browser's Sign out button closes its current session. Sessions last at most eight hours and expire after thirty idle minutes; a visible page's polling keeps it active within that maximum. Stop, unload or restart clears every session. If a bind fails, status reports its safe error type; check that the IP exists in the container and that its port is free. Red keeps running when automatic dashboard startup fails.

## Data and checks

Config identifier `702035014` stores only global listener settings: enabled, bind, port, allowed hostnames and the displayed URL. The URL defaults to an empty string on upgrade through Red's merged defaults, preserving the existing bind, port and startup policy. Login/session credentials are hashed in bounded memory, never saved. At most 32 codes and 32 sessions are retained. Login/request rate windows are bounded and are not an access log. User-data export returns that owner's transient login counts without credentials; deletion revokes their access. Read-only data requests use the source cogs' records and policies without saving a separate history.

The server checks allowed hosts, same-origin writes, JSON content/body limits, per-session CSRF, current bot ownership, server membership, channel permissions, DashboardPlus availability and original source command/cog checks. Expired or revoked owners cannot keep using a cookie. Fixed HTTP actions run Red's real parser and command pipeline with an authenticated owner Context and web-only replies. Cleanup cancels owned startup/request tasks, closes the listener and revokes login state.

Tests exercise a real local HTTP listener, cookies, login replay/expiry/revocation, origin/CSRF/host boundaries, source disabling, actual Red music/settings commands, typed converters, restart/unload and privacy hooks. Discord and media-provider transport are mocked; this is not a live Scarlet deployment test.
