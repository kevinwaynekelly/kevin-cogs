# Kevin Cogs

Fifteen independently installable cogs for server features, shared settings, Core/Downloader management, a local dashboard and Unraid failure notifications for [Red Discord Bot](https://docs.discord.red/en/stable/), maintained by [Kevin Kelly](https://github.com/kevinwaynekelly).

Music, personal voice entrance clips, saved bot status profiles, community tools, leveling, event logging, message transformations, automatic emoji capture, readable chat exports and server structure backups. Each cog can be installed separately.

## Cogs

| Cog | What it does | Commands | Guide |
| --- | --- | --- | --- |
| AudioPlus | Native playback, short-song caching, recovery, shared playlists, DJ policies and daily checks | `[p]play`, `/play`, `[p]audiocache` | [Setup and commands](audioplus/README.md) |
| IntroPlus | Cached personal YouTube voice entrance clips, duration/start controls and music overlays | `[p]intro set`, `/intro test`, `[p]intro duration` | [Setup and commands](introplus/README.md) |
| PresencePlus | Owner-only saved bot status, rotation, timezone schedules and selected-server music presence | `[p]presence set`, `/presence preview`, `[p]presence music` | [Setup and commands](presenceplus/README.md) |
| CommunityPlus | Roles, welcomes, temporary rooms, onboarding, birthdays and recurring events | `[p]community`, `[p]voiceroom`, `/birthday set` | [Setup and commands](communityplus/README.md) |
| LevelPlus | XP, custom achievements, streaks, monthly seasons, filtered boards and rank cards | `[p]level`, `[p]achievement`, `/streak` | [Setup and commands](levelplus/README.md) |
| LogPlus | Event logs, retained history, burst alerts, daily digests and staff incidents | `[p]log`, `[p]logalerts`, `/incident list` | [Setup and commands](logplus/README.md) |
| OwoPlus | Custom styles, author Undo, scoped transformations, approved haiku and contests | `[p]owo`, `[p]owoundo`, `/haikucontest list` | [Setup and commands](owoplus/README.md) |
| EmojiStealerPlus | Automatically copy external static/animated emoji from messages and reactions | `[p]emoji`, `[p]yoink`, `/emoji status` | [Setup and commands](emojistealerplus/README.md) |
| ExportPlus | Privately export accessible server chats, forums and threads into readable ChatGPT files | `[p]export server`, `/export channel`, `[p]export text` | [Setup and commands](exportplus/README.md) |
| BackupPlus | Named role/channel/permission snapshots, private restore previews and optional automatic backups | `[p]backup create`, `/backup preview`, `[p]backup restore` | [Setup and commands](backupplus/README.md) |
| SettingsHub | Shared themes, command discovery, diagnostics, configuration history, readiness checks and snapshots | `[p]settings`, `/theme show`, `[p]snapshots` | [Setup and commands](settingshub/README.md) |
| DownloaderPlus | Themed repo/cog updates, Discord/webhook and daily automation, with slash sync | `[p]download`, `/download update`, `[p]download repos` | [Setup and commands](downloaderplus/README.md) |
| DashboardPlus | Private local dashboard for music, explained settings and searchable data from every suite cog | `[p]dashboard start`, `/dashboard login` | [Setup and commands](dashboardplus/README.md) |
| CorePlus | Category help, `/help`, themed native Core replies and checked bot-management controls | `[p]help`, `/core status`, `[p]core reload` | [Setup and commands](coreplus/README.md) |
| NotificationPlus | Suite error detection and bounded Unraid alerts using existing email settings | `[p]notifications enable`, `/notifications test` | [Setup and commands](notificationplus/README.md) |

`[p]` means your bot's command prefix. For example, `[p]level show` becomes `!level show` when your prefix is `!`. Angle brackets mark required arguments; do not type the brackets.

## Guided setup and member tools

Each setup panel offers current-server channel/role pickers or toggles, expires after three minutes, and repeats the original requester and administrator checks on every click. The guides describe feature defaults, limits, and saved data.

| Cog | Administrator setup | Member features |
| --- | --- | --- |
| AudioPlus | `[p]audioset setup` or `/audioset setup` | Playback buttons, seek/queue editing, private saved playlists/favorites, and optional listener vote skipping. |
| IntroPlus | `[p]intro enable`, `[p]intro volume`, `[p]intro channel` | Personal `[p]intro set <seconds> <video>`, start offsets, previews and clearing. |
| CommunityPlus | `[p]community setup` or `/community setup` | `[p]roles`, polls, event RSVPs/reminders, and posted safe self-service role pickers. Voice reports and weekly summaries retain administrator checks. |
| LevelPlus | `[p]level setup` or `/level setup` | Rank/lifetime boards, `[p]periodboard`, season history, `[p]achievements`, `[p]challenges`, and `[p]rankcard`. Administrators configure rewards, boosts, and farming controls. |
| LogPlus | `[p]log setup` or `/log setup` | Administrator routing, permission diffs, delivery recovery, and opt-in retained history with timeline/search/export. |
| EmojiStealerPlus | `[p]emoji` or `/emoji status` | Automatic external emoji capture and checked manual `[p]yoink` controls. |
| OwoPlus | `[p]owo setup` or `/owo setup` | `[p]owooptout`, `[p]owoify <text>`, `[p]stylize <style> <text>`, and `[p]haiku <text>`. Manual transformations leave source messages alone. |

The original five feature cogs expose 312 slash actions; EmojiStealerPlus adds six, ExportPlus adds nine, BackupPlus adds 13, IntroPlus adds 16, PresencePlus adds 24, optional SettingsHub adds 23, CorePlus adds 14, DownloaderPlus adds 32, DashboardPlus adds ten and NotificationPlus adds four, for 463 total actions. The fifteen cogs together register 92 roots, within Discord's 100-root limit. These counts are checked against Red's command tree. Enable the desired cogs, reload after updating, and run `slash sync` to publish their definitions to Discord.

## Shared settings dashboard

Install optional [SettingsHub](settingshub/README.md) for `[p]settings` or `/settings panel`, a single picker for the six loaded feature cogs' configuration panels. `/settings health` inspects cog status and local prerequisites. `/settings backup` exports selected server configuration; `/settings restore` validates an attached same-server backup and previews changes before applying them. Member records, credentials, active events, histories, and runtime cursors are excluded and preserved. All controls retain current administrator, command, and cog checks.

```text
[p]cog install kevin-cogs settingshub
[p]load settingshub
[p]slash enablecog settingshub
[p]slash sync
```

## Local web dashboard

Install optional [DashboardPlus](dashboardplus/README.md) for a private browser dashboard hosted inside Red, with server/channel selection, current track thumbnails and queue controls, 32 explained settings and 33 searchable, paginated data views across all fourteen suite cogs. Browse intro owners and clip timing, Community activity and games, Level XP and rewards, music history and saved collections, and the other cogs' reviewed records. Data loads when requested, rather than being copied into another database or refreshed with every music update. Current source permissions still apply.

Use `[p]dashboard start` and `[p]dashboard login`; the initial URL is `http://127.0.0.1:8765`. For the Red host at `10.10.1.200`, bind to `0.0.0.0`, publish TCP port `8765`, and save `[p]dashboard url http://10.10.1.200:8765/` so login/status show the correct link. Open [http://10.10.1.200:8765/](http://10.10.1.200:8765/). Listener/start/link settings survive restarts; login codes and sessions stay in bounded memory. No separate frontend service or bot-token input is needed. Changes use existing Red command checks, converters, hooks and validation.

## Failure emails and automatic updates

Install [NotificationPlus](notificationplus/README.md), load it and run `[p]notifications enable`. It records unexpected suite command errors, background failures, failed notifications and AudioPlus playback/check failures. The included [Unraid User Script](notificationplus/unraid/README.md) checks the persistent outbox every minute and submits grouped alerts through Unraid's existing notification/email settings. Matching failures are suppressed for ten minutes; records exclude chat content, member names, media URLs and credentials. Run `[p]notifications test` after host setup to verify delivery. A queued event or successful Unraid submission does not prove email arrival.

[DownloaderPlus webhook setup](downloaderplus/README.md#github-webhook-updates) creates a private signing secret and an optional local listener. A valid push to an installed repository's tracked branch queues updates for all native Downloader repositories and cogs, preserving pins and dependency checks. Expose only the webhook route through a public HTTPS proxy for GitHub delivery. Update failures also reach NotificationPlus; the listener is disabled until configured. Private update links and optional daily updates use the same engine. `updateall` and automated updates reload changes and sync enabled slash commands, preserving Red enable choices. Alternatively, [approve an existing Discord webhook](downloaderplus/README.md#discord-webhook-updates) to post `updateall` in its bound channel without opening a public bot port. The bot checks the configured owner and exact webhook identity; only IDs and bounded runtime records persist. See the guide for Discord bindings, port mapping, private links and daily schedule controls.

## GPT access to Aria

The optional [Aria GPT bridge](tools/aria_bridge/README.md) runs separately on Unraid and connects four MCP tools to ChatGPT through an outbound private tunnel: live host metrics, container status snapshots, all-repo/cog updates and update status. Its installer uses protected credential files, automatic container restart and a pinned, checksum-verified tunnel client. It exposes no public port and does not mount Docker's control socket. Follow the guide to create the account tunnel, enable Red's existing update listener, install on Aria and connect the plugin; publishing this repository does not activate those services.

## Discord presentation

All fifteen cogs share an indigo theme, consistent headings and footers, readable settings, and matching success, warning, and error colors. SettingsHub optionally customizes the server's colors and footer, including live music/poll edits and log delivery retries. Long results are paginated, and replies fall back to text when embeds are unavailable. See the [design and visual preview](docs/PRESENTATION.md).

Optional [CorePlus](coreplus/README.md) replaces `[p]help` with a themed category selector and adds `/help`. It also themes bundled Core/CogManagerUI replies and adds checked `/core` management controls. Red's native commands remain available. Optional [DownloaderPlus](downloaderplus/README.md) themes native repository/cog replies and adds owner-only `/download` controls backed by bundled Downloader. Without CorePlus, native `[p]help` lists descriptions for every cog command. Use `[p]help community`, `[p]help level`, or `[p]help log` to see their subcommands, and append a subcommand for its arguments and details. Cog names such as `[p]help CommunityPlus` also show a category overview.

## Install

Run these commands as the bot owner:

```text
[p]load downloader
[p]repo add kevin-cogs https://github.com/kevinwaynekelly/kevin-cogs main
[p]cog list kevin-cogs
```

Install and load the cogs you want. For example:

```text
[p]cog install kevin-cogs communityplus levelplus logplus owoplus
[p]load communityplus levelplus logplus owoplus
```

AudioPlus can load before its local system dependencies are installed. Follow the [AudioPlus setup guide](audioplus/README.md), then install it separately:

```text
[p]cog install kevin-cogs audioplus
[p]load audioplus
```

Use `[p]play <song or URL>`, `[p]skip`, `[p]pause`, `[p]np`, and `[p]queue` for music controls. Enable their slash counterparts as the bot owner with `[p]slash enablecog audioplus`, then `[p]slash sync`. The [AudioPlus guide](audioplus/README.md#enable-slash-commands) lists the direct and slash controls. Legacy `[p]audio ...` commands remain available.

AudioPlus includes public YouTube thumbnails across track cards, controls, queues, searches, saved music, history and session summaries. Artwork follows the live track, works with existing saved/cached songs and adds no media lookup. The now-playing panel refreshes progress every three seconds.

The owner can enable [daily YouTube playback checks](audioplus/README.md#daily-youtube-playback-checks) with `[p]audiocheck enable`. A silent three-second native voice probe runs at 09:00 America/Chicago by default and DMs its configuring owner on failure. Successful checks stay quiet; busy voice connections postpone the probe.

Daily failure alerts identify the stage and safe error details. `[p]audiostatus` retains the latest check result in the monitored server after the probe disconnects, so an unexpected failure can be investigated without losing its context.

CommunityPlus, LevelPlus, LogPlus, OwoPlus and EmojiStealerPlus declare Red **3.5.0 or newer**. SettingsHub requires **3.5.24 or newer**. AudioPlus requires **Red 3.5.24 or newer**, native Discord voice, **yt-dlp**, **PyNaCl**, and **davey**. Downloader installs yt-dlp and its matching EJS package. Install **PyNaCl>=1.5.0,<1.6** and **davey>=0.1.6** once in Red's Python environment; working voice libraries previously installed by Downloader remain supported and are kept during cog updates. Install **FFmpeg**, **libopus**, and **Deno 2.3+ or Node.js 22+** inside the Red container. AudioPlus no longer needs Lavalink, Wavelink, or Java. Restart Red when changing voice libraries. Its guide includes voice-library and Deno installation commands and an optional persistent container image recipe. LevelPlus installs Pillow for PNG rank cards. CommunityPlus, LogPlus and EmojiStealerPlus have no additional required Python packages. OwoPlus can use optional syllable-counting packages, described in its guide.

These guides describe the current source. Compatibility metadata is not a record of live testing on every Red, Discord, or media-provider version.

If music reports that PyNaCl or davey cannot import or has incompatible native APIs, update/reload AudioPlus and run `[p]audiorepair` as the bot owner. It repairs failing voice libraries in Red's running Python environment and verifies their required APIs in a fresh process. This includes a missing `davey.DAVE_PROTOCOL_VERSION` despite a successful import. Restart Red after the install attempt, then run `[p]audiostatus` and `[p]play <query>`. See the [voice setup guide](audioplus/README.md#one-time-native-voice-library-setup) for update commands, limits, and manual container setup.

## First setup

For configuration repositories, use the [all-cog storage audit and settings-only exporter](docs/CONFIG_STORAGE.md). Red's raw `settings.json` files are databases containing member activity, XP, saved music and snapshots as well as policy. The exporter keeps reviewed settings across all cogs while excluding those records and credentials, without changing the live database.

Read the cog's guide before loading it on an existing server. Some features start working immediately:

| Cog | Initial behavior |
| --- | --- |
| AudioPlus | Runs music search and playback locally with `[p]play` or `/play`. Automatically caches known non-live provider songs under five minutes for three calendar months, subject to a 2 GiB bot-wide limit. Inspect/clear copies with `[p]audiocache`. Joins your voice channel, or the available channel with the most people if you are not in voice. Disconnects after the queue is idle for 10 seconds. Unload Red's bundled Audio cog before loading AudioPlus. Check dependencies with `[p]audiostatus`; enable and sync slash commands once as the bot owner. |
| IntroPlus | Automatic intros are enabled only for members who have a saved clip. Use `[p]intro set 8 <YouTube video or search>`, check local-copy readiness with `[p]intro show` and preview with `[p]intro test`. Prepares the selected 0.5–30 second segment locally for faster joins; ready copies survive restarts. Cooldown defaults to 60 seconds. Uses the native AudioPlus prerequisites and can install independently. |
| PresencePlus | Bot-owner controls only. Automation defaults off; `[p]presence set custom <text>` enables it. Saved profiles rotate every five minutes by default, with optional weekly timezone schedules and `[p]presence music true` for one selected AudioPlus server. Status is visible across all the bot's servers. No extra dependencies. |
| CommunityPlus | Sticky roles, activity tracking, and solo voice cleanup are enabled. Solo voice cleanup defaults to 900 seconds. Autorole and welcome/goodbye targets need to be configured. |
| LevelPlus | Message, reaction, and voice XP are enabled, along with level-up announcements. |
| LogPlus | Needs a destination channel or route before it can post logs. |
| OwoPlus | Disabled until `[p]owo enable`. Haiku formatting is enabled within the cog's settings. |
| EmojiStealerPlus | Automatically captures external custom emoji in member messages, edits and reactions. Needs Create Expressions and message/reaction intents. Notices default off; pause with `[p]emoji enabled false`. |
| BackupPlus | Captures only on explicit administrator request; automatic snapshots default off. Use `[p]backup create baseline` and `[p]backup download baseline`, then preview any restore. |

Common starting points:

```text
[p]community help
[p]level help
[p]log help
[p]owo help
```

Server management commands generally require Red's admin access or the **Manage Server** permission. LevelPlus also exposes member commands. AudioPlus legacy node commands remain owner-only for compatibility; ordinary native playback controls are server commands. Each guide lists the permissions required by its features.

## Direct and slash commands

| Direct text command | Slash equivalent | Purpose |
| --- | --- | --- |
| `[p]rank [@member]` | `/rank` | Member level, XP, and progress. |
| `[p]leaderboard [top]` | `/leaderboard` | Highest XP totals. Text alias: `lb`. |
| `[p]levellookup <query>` | `/levellookup` | Find member IDs by mention, ID, or name. |
| `[p]roles` | `/roles` | Choose configured safe self-service roles. |
| `[p]voicehours [@member]` | `/voicehours` | Recorded voice duration. |
| `[p]seen [@member]` | `/seen` | Last-seen information. |
| `[p]seendetail [@member]` | `/seendetail` | Detailed activity timestamps. |
| `[p]activity [@member]` | `/activity` | Activity counters and games. Text alias: `stats`. |
| `[p]seenlist [limit]` | `/seenlist` | Recently active members. |
| `[p]logstatus` | `/logstatus` | Logging settings. |
| `[p]logchannel [#channel]` | `/logchannel` | Show or set the log destination. |
| `[p]lograte [seconds]` | `/lograte` | Show or set duplicate suppression. |

Community reports and logging shortcuts retain administrator checks. The `roles` picker is available to members. Grouped text commands remain available under `community`, `level`, `log`, and `owo`. Slash groups use a `status` subcommand for their settings panel, such as `/community status`. These four cogs offer 250 slash actions; their guides list the deeper or ID-based paths that remain text-only.

Run these commands once as the bot owner to enable the new slash groups and shortcuts:

```text
[p]slash enablecog communityplus
[p]slash enablecog levelplus
[p]slash enablecog logplus
[p]slash enablecog owoplus
[p]slash sync
```

Cog package names used by Downloader and `load`/`reload` still include `plus`. Public command names do not. Slash commands use the same Red checks and saved permission rules as their text counterparts; shortcuts also check the original grouped command. Discord synchronization and a bot invite with application-command access are required before slash commands appear.

## Updates

```text
[p]cog update True communityplus levelplus logplus owoplus
[p]reload communityplus levelplus logplus owoplus
[p]slash sync
```

For the AudioPlus native-backend upgrade, use `[p]cog update False audioplus`, install the local dependencies, and restart Red. Later source updates can use `[p]cog update True audioplus`. See its [upgrade instructions](audioplus/README.md#upgrading-from-the-lavalink-backend).

If new AudioPlus prefix commands remain silent, run `[p]reload audioplus` and `[p]help play` after updating. An update that reports the cog is already current does not automatically reload it. See the [command-loading checks](audioplus/README.md#if-new-prefix-commands-do-not-respond).

`[p]repo update kevin-cogs` updates the downloaded repository; use `cog update` to update installed cogs. If you named the repository differently when adding it, use that name in repository and installation commands.

The command rename intentionally replaces `com` with `community`, `logplus` with `log`, and `owoplus` with `owo`, without old-name aliases. Saved cog settings, XP, and member records are unchanged. Reapply custom Red permission or disabled-command rules that referenced an old command path using its new name. Run `slash enablecog` for each newly enabled cog and `slash sync` after reloading.

## Music additions

AudioPlus now has automatic player panels with checked buttons, `seek`, queue `remove`/`move`, private saved `playlist`/`favorite` collections, and `audioset` DJ/vote policies. Use `[p]audioset setup` for its guided settings panel. See the [new controls](audioplus/README.md#player-panels-queue-tools-and-saved-music) and run `slash sync` after reloading to upload their slash counterparts.

## Emoji, rewards, sessions, events and support

Install the new cog with `[p]cog install kevin-cogs emojistealerplus`, then `[p]load emojistealerplus`. Automatic external emoji capture begins immediately when permissions allow; `[p]emoji` shows its controls and `[p]yoink <custom emoji>` copies one manually. See the [emoji guide](emojistealerplus/README.md).

Use `[p]level rewards preview` or `/level rewards preview` for a read-only formula/reward scenario. Enable music summaries with `[p]audioset summary true [#channel]`; after disconnect, save the latest session with its playlist button or `[p]playlist session <name>` within three minutes. Summaries start disabled and retain bounded transient public metadata, requester IDs and decoded playback time; no audio is saved.

`[p]event native <id> [voice_channel] [minutes]` links a local event to Discord's Events tab; `[p]event nativeset true` enables mirrors for new events. Native events start disabled and need Create Events, plus ordinary voice access when selected. Interested subscriptions remain separate from local RSVPs. `[p]settings support` produces a bounded ZIP of whitelisted dependency/source/permission information, readiness flags and recent error types. All these controls have slash counterparts and preserve current permission checks.

## New server features

| Cog | Added features | Starting commands |
| --- | --- | --- |
| AudioPlus | Listening history/replay, atomic request limits, opt-in queue recovery, empty-room pause/departure, normalization and DJ-approved shared playlists | `history`, `replay`, `audioset limits`, `recoverqueue`, `serverplaylist` |
| CommunityPlus | Join-to-create rooms with owner controls, rules acceptance, opt-in month/day birthdays and recurring events with capacity waitlists | `voiceroom`, `onboard`, `birthday`, `event policy` |
| LevelPlus | Administrator-defined achievement goals/rewards, bounded daily streak bonuses, role-filtered boards and automatic monthly season closure/winners | `achievement`, `streakset`, `streak`, `roleboard`, `monthlyseason` |
| LogPlus | Join/delete/permission burst warnings, aggregate daily digests, staff incident notes/log attachments/resolution and private repeated-error notices | `logalerts`, `incident` |
| OwoPlus | Named custom dictionaries/decorations, two-minute author Undo, approved haiku submissions and deadline/voting contests | `customstyle`, `stylize`, `owoundo`, `haikuhall`, `haikucontest` |
| SettingsHub | Server colors/footer, safe diagnostics with installed-source fingerprints, permission-aware usage/examples and opt-in configuration snapshots/diffs/restore | `theme`, `settings diagnostics`, `commandbrowser`, `snapshots` |

Use the prefix or matching slash action shown in each guide. Recovery needs an explicit `recoverqueue`; it does not automatically reconnect after restart. New automatic notices, birthday policies, streak bonuses, monthly seasons and snapshots require configuration. Owo Undo controls default on while automatic transformation itself remains disabled until enabled. Features have documented record/time limits and user-data hooks. Configuration snapshots cover selected source-cog settings; they preserve member and operational records rather than replacing them.

## Data

EmojiStealerPlus stores bounded emoji source/destination mappings and image fingerprints, with no member IDs, chat text or saved image files. Copied emojis remain on Discord until removed there. Automatic capture starts enabled on loading the cog.

Settings and persistent records use Red's Config system. CommunityPlus records activity, sticky roles, temporary-room ownership, opted-in birthday dates and rules acceptance. LevelPlus retains XP, display names, achievements/streaks and bounded season archives. OwoPlus stores personal preferences and explicitly submitted haiku/contest entries/votes for up to 90 days; author Undo text stays only in memory for two minutes. SettingsHub stores themes, up to ten selected configuration snapshots and bounded actor-attributed configuration changes, with identified-user export/deletion.

AudioPlus preserves legacy Lavalink connection settings, including the old node password, for rollback. Native playback ignores them. The optional daily monitor stores recipient/test/schedule/result records. Personal/shared playlists retain public metadata and proposer/requester attribution. Listening history starts enabled and retains 100 public playback starts for 30 days, capped at 512 KiB, with requester export/deletion. Opt-in queue recovery retains up to 100 public records and playback state for seven days; ordinary queues remain transient when recovery is off. The automatic short-song cache stores up to 2 GiB/2,000 server-scoped audio copies for three calendar months with hashed source IDs and bounded requester metadata outside Config/backups. User-data hooks export identified cache metadata and erase associated copies; administrators can clear server copies. Extracted stream URLs, credentials, HTTP headers and yt-dlp disk caches are not persisted.

PresencePlus stores global owner-supplied status profiles/templates, availability, rotation, timezone, weekly rules and an optional music source server ID. Dynamic song/listener information is read from memory and not persisted. Status text is visible across the bot's servers. It has no member records; the owner can reset all presence settings explicitly.

LogPlus posts edited/deleted text to configured Discord channels. Opt-in history retains bounded event details for 1 to 90 days; staff incident cases retain selected log excerpts/notes and attribution for up to 90 days. Daily digests store aggregate counts, and owner error policies store the configuring recipient. OwoPlus reposts transformed messages through webhooks and attempts to delete originals. Each guide and `info.json` describes its records and limits. All eleven cogs implement Red's user-data hooks. Deletion removes or anonymizes associated records; messages already posted to Discord remain managed there.

ExportPlus reads chat history only on explicit administrator requests and privately sends readable text/JSONL ZIPs. Temporary exports expire after 24 hours or are cleared on cancellation/unload and user-data deletion. Only accessible history is included; skipped sources and partial results are listed. See the [export guide](exportplus/README.md).

BackupPlus stores bounded structural snapshots, including role/member permission overwrite IDs, and keeps manual, automatic and pre-restore copies separate. Downloads/previews are private; restores require a fresh administrator confirmation and preserve message history and member role assignments. See the [backup guide](backupplus/README.md).

## Development and support

- [Contributing and local checks](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)
- [Report an issue](https://github.com/kevinwaynekelly/kevin-cogs/issues)
- [Red Downloader documentation](https://docs.discord.red/en/stable/cog_guides/downloader.html)

Each cog has a small `__init__.py` entry point, `cog.py` command/event implementation, `constants.py` defaults, metadata, and a README. Level calculations and haiku detection have separate modules. The regression suite checks real Red Config storage and command registration with mocked Discord calls, plus local yt-dlp extraction and FFmpeg decoding; CI covers Python 3.10/3.11 with Red 3.5.24 and native voice dependencies. See [CONTRIBUTING.md](CONTRIBUTING.md) for test and benchmark commands.
