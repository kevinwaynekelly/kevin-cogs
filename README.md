# Kevin Cogs

Five feature cogs and an optional shared settings hub for [Red Discord Bot](https://docs.discord.red/en/stable/), maintained by [Kevin Kelly](https://github.com/kevinwaynekelly).

Music, community tools, leveling, event logging, and message transformations. Each cog can be installed separately.

## Cogs

| Cog | What it does | Commands | Guide |
| --- | --- | --- | --- |
| AudioPlus | Native playback, recovery, normalization, shared playlists, DJ policies and daily checks | `[p]play`, `/play`, `[p]audioset` | [Setup and commands](audioplus/README.md) |
| CommunityPlus | Roles, welcomes, temporary rooms, onboarding, birthdays and recurring events | `[p]community`, `[p]voiceroom`, `/birthday set` | [Setup and commands](communityplus/README.md) |
| LevelPlus | XP, custom achievements, streaks, monthly seasons, filtered boards and rank cards | `[p]level`, `[p]achievement`, `/streak` | [Setup and commands](levelplus/README.md) |
| LogPlus | Event logs, retained history, burst alerts, daily digests and staff incidents | `[p]log`, `[p]logalerts`, `/incident list` | [Setup and commands](logplus/README.md) |
| OwoPlus | Custom styles, author Undo, scoped transformations, approved haiku and contests | `[p]owo`, `[p]owoundo`, `/haikucontest list` | [Setup and commands](owoplus/README.md) |
| SettingsHub | Shared themes, permission-aware command discovery, diagnostics and configuration snapshots | `[p]settings`, `/theme show`, `[p]snapshots` | [Setup and commands](settingshub/README.md) |

`[p]` means your bot's command prefix. For example, `[p]level show` becomes `!level show` when your prefix is `!`. Angle brackets mark required arguments; do not type the brackets.

## Guided setup and member tools

Each setup panel offers current-server channel/role pickers or toggles, expires after three minutes, and repeats the original requester and administrator checks on every click. The guides describe feature defaults, limits, and saved data.

| Cog | Administrator setup | Member features |
| --- | --- | --- |
| AudioPlus | `[p]audioset setup` or `/audioset setup` | Playback buttons, seek/queue editing, private saved playlists/favorites, and optional listener vote skipping. |
| CommunityPlus | `[p]community setup` or `/community setup` | `[p]roles`, polls, event RSVPs/reminders, and posted safe self-service role pickers. Voice reports and weekly summaries retain administrator checks. |
| LevelPlus | `[p]level setup` or `/level setup` | Rank/lifetime boards, `[p]periodboard`, season history, `[p]achievements`, `[p]challenges`, and `[p]rankcard`. Administrators configure rewards, boosts, and farming controls. |
| LogPlus | `[p]log setup` or `/log setup` | Administrator routing, permission diffs, delivery recovery, and opt-in retained history with timeline/search/export. |
| OwoPlus | `[p]owo setup` or `/owo setup` | `[p]owooptout`, `[p]owoify <text>`, `[p]stylize <style> <text>`, and `[p]haiku <text>`. Manual transformations leave source messages alone. |

The five feature cogs expose 297 slash actions; optional SettingsHub adds 16. The six cogs together register 77 roots, within Discord's 100-root limit. These counts are checked against Red's command tree. Enable the desired cogs, reload after updating, and run `slash sync` to publish their definitions to Discord.

## Shared settings dashboard

Install optional [SettingsHub](settingshub/README.md) for `[p]settings` or `/settings panel`, a single picker for the five loaded cogs' setup panels. `/settings health` inspects cog status and local prerequisites. `/settings backup` exports selected server configuration; `/settings restore` validates an attached same-server backup and previews changes before applying them. Member records, credentials, active events, histories, and runtime cursors are excluded and preserved. All controls retain current administrator, command, and cog checks.

```text
[p]cog install kevin-cogs settingshub
[p]load settingshub
[p]slash enablecog settingshub
[p]slash sync
```

## Discord presentation

All six cogs share an indigo theme, consistent headings and footers, readable settings, and matching success, warning, and error colors. SettingsHub optionally customizes the server's colors and footer, including live music/poll edits and log delivery retries. Long results are paginated, and replies fall back to text when embeds are unavailable. See the [design and visual preview](docs/PRESENTATION.md).

Red's native `[p]help` lists descriptions for every cog command. Use `[p]help community`, `[p]help level`, or `[p]help log` to see their subcommands, and append a subcommand for its arguments and details. Cog names such as `[p]help CommunityPlus` also show a category overview.

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

The owner can enable [daily YouTube playback checks](audioplus/README.md#daily-youtube-playback-checks) with `[p]audiocheck enable`. A silent three-second native voice probe runs at 09:00 America/Chicago by default and DMs its configuring owner on failure. Successful checks stay quiet; busy voice connections postpone the probe.

Daily failure alerts identify the stage and safe error details. `[p]audiostatus` retains the latest check result in the monitored server after the probe disconnects, so an unexpected failure can be investigated without losing its context.

CommunityPlus, LevelPlus, LogPlus, and OwoPlus declare Red **3.5.0 or newer**. SettingsHub requires **3.5.24 or newer**. AudioPlus requires **Red 3.5.24 or newer**, native Discord voice, **yt-dlp**, **PyNaCl**, and **davey**. Downloader installs yt-dlp and its matching EJS package. Install **PyNaCl>=1.5.0,<1.6** and **davey>=0.1.6** once in Red's Python environment; working voice libraries previously installed by Downloader remain supported and are kept during cog updates. Install **FFmpeg**, **libopus**, and **Deno 2.3+ or Node.js 22+** inside the Red container. AudioPlus no longer needs Lavalink, Wavelink, or Java. Restart Red when changing voice libraries. Its guide includes voice-library and Deno installation commands and an optional persistent container image recipe. LevelPlus installs Pillow for PNG rank cards. CommunityPlus and LogPlus have no additional required Python packages. OwoPlus can use optional syllable-counting packages, described in its guide.

These guides describe the current source. Compatibility metadata is not a record of live testing on every Red, Discord, or media-provider version.

If music reports that PyNaCl or davey cannot import or has incompatible native APIs, update/reload AudioPlus and run `[p]audiorepair` as the bot owner. It repairs failing voice libraries in Red's running Python environment and verifies their required APIs in a fresh process. This includes a missing `davey.DAVE_PROTOCOL_VERSION` despite a successful import. Restart Red after the install attempt, then run `[p]audiostatus` and `[p]play <query>`. See the [voice setup guide](audioplus/README.md#one-time-native-voice-library-setup) for update commands, limits, and manual container setup.

## First setup

Read the cog's guide before loading it on an existing server. Some features start working immediately:

| Cog | Initial behavior |
| --- | --- |
| AudioPlus | Runs music search and playback locally with `[p]play` or `/play`. Joins your voice channel, or the available channel with the most people if you are not in voice. Disconnects after the queue is idle for 10 seconds. Unload Red's bundled Audio cog before loading AudioPlus. Check dependencies with `[p]audiostatus`; enable and sync slash commands once as the bot owner. |
| CommunityPlus | Sticky roles, activity tracking, and solo voice cleanup are enabled. Solo voice cleanup defaults to 900 seconds. Autorole and welcome/goodbye targets need to be configured. |
| LevelPlus | Message, reaction, and voice XP are enabled, along with level-up announcements. |
| LogPlus | Needs a destination channel or route before it can post logs. |
| OwoPlus | Disabled until `[p]owo enable`. Haiku formatting is enabled within the cog's settings. |

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

Community reports and logging shortcuts retain administrator checks. The `roles` picker is available to members. Grouped text commands remain available under `community`, `level`, `log`, and `owo`. Slash groups use a `status` subcommand for their settings panel, such as `/community status`. These four cogs offer 245 slash actions; their guides list the deeper or ID-based paths that remain text-only.

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

## New server features

| Cog | Added features | Starting commands |
| --- | --- | --- |
| AudioPlus | Opt-in queue/position recovery after restart, empty-room pause/departure, loudness normalization and DJ-approved shared playlists | `audioset recovery`, `recoverqueue`, `audioset emptypause`, `audioset normalize`, `serverplaylist` |
| CommunityPlus | Join-to-create rooms with owner controls, rules acceptance, opt-in month/day birthdays and recurring events with capacity waitlists | `voiceroom`, `onboard`, `birthday`, `eventpolicy` |
| LevelPlus | Administrator-defined achievement goals/rewards, bounded daily streak bonuses, role-filtered boards and automatic monthly season closure/winners | `achievement`, `streakset`, `streak`, `roleboard`, `monthlyseason` |
| LogPlus | Join/delete/permission burst warnings, aggregate daily digests, staff incident notes/log attachments/resolution and private repeated-error notices | `logalerts`, `incident` |
| OwoPlus | Named custom dictionaries/decorations, two-minute author Undo, approved haiku submissions and deadline/voting contests | `customstyle`, `stylize`, `owoundo`, `haikuhall`, `haikucontest` |
| SettingsHub | Server colors/footer, safe diagnostics with installed-source fingerprints, permission-aware usage/examples and opt-in configuration snapshots/diffs/restore | `theme`, `settings diagnostics`, `commandbrowser`, `snapshots` |

Use the prefix or matching slash action shown in each guide. Recovery needs an explicit `recoverqueue`; it does not automatically reconnect after restart. New automatic notices, birthday policies, streak bonuses, monthly seasons and snapshots require configuration. Owo Undo controls default on while automatic transformation itself remains disabled until enabled. Features have documented record/time limits and user-data hooks. Configuration snapshots cover selected source-cog settings; they preserve member and operational records rather than replacing them.

## Data

Settings and persistent records use Red's Config system. CommunityPlus records activity, sticky roles, temporary-room ownership, opted-in birthday dates and rules acceptance. LevelPlus retains XP, display names, achievements/streaks and bounded season archives. OwoPlus stores personal preferences and explicitly submitted haiku/contest entries/votes for up to 90 days; author Undo text stays only in memory for two minutes. SettingsHub stores themes and up to ten selected configuration snapshots.

AudioPlus preserves legacy Lavalink connection settings, including the old node password, for rollback. Native playback ignores them. The optional daily monitor stores recipient/test/schedule/result records. Personal/shared playlists retain public metadata and proposer/requester attribution. Opt-in listening history retains 100 public playback starts for 30 days, capped at 512 KiB, with requester export/deletion. Opt-in queue recovery retains up to 100 public records and playback state for seven days; ordinary queues remain transient when recovery is off. Extracted stream URLs, downloaded audio and yt-dlp disk caches are not persisted.

LogPlus posts edited/deleted text to configured Discord channels. Opt-in history retains bounded event details for 1 to 90 days; staff incident cases retain selected log excerpts/notes and attribution for up to 90 days. Daily digests store aggregate counts, and owner error policies store the configuring recipient. OwoPlus reposts transformed messages through webhooks and attempts to delete originals. Each guide and `info.json` describes its records and limits. All six cogs implement Red's user-data hooks. Deletion removes or anonymizes associated records; messages already posted to Discord remain managed there.

## Development and support

- [Contributing and local checks](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)
- [Report an issue](https://github.com/kevinwaynekelly/kevin-cogs/issues)
- [Red Downloader documentation](https://docs.discord.red/en/stable/cog_guides/downloader.html)

Each cog has a small `__init__.py` entry point, `cog.py` command/event implementation, `constants.py` defaults, metadata, and a README. Level calculations and haiku detection have separate modules. The regression suite checks real Red Config storage and command registration with mocked Discord calls, plus local yt-dlp extraction and FFmpeg decoding; CI covers Python 3.10/3.11 with Red 3.5.24 and native voice dependencies. See [CONTRIBUTING.md](CONTRIBUTING.md) for test and benchmark commands.
