# Kevin Cogs

Five custom cogs for [Red Discord Bot](https://docs.discord.red/en/stable/), maintained by [Kevin Kelly](https://github.com/kevinwaynekelly).

Music, community tools, leveling, event logging, and message transformations. Each cog can be installed separately.

## Cogs

| Cog | What it does | Commands | Guide |
| --- | --- | --- | --- |
| AudioPlus | Native Discord music playback, YouTube/SoundCloud search, queues, and voice diagnostics | `[p]play`, `/play`, `[p]audio` | [Setup and commands](audioplus/README.md) |
| CommunityPlus | Autoroles, sticky roles, welcome/goodbye messages, activity tracking, and solo voice cleanup | `[p]community`, `[p]seen`, `/activity` | [Setup and commands](communityplus/README.md) |
| LevelPlus | Message, reaction, and voice XP with configurable level curves and import/export tools | `[p]level`, `[p]rank`, `/leaderboard` | [Setup and commands](levelplus/README.md) |
| LogPlus | Server event logs with a default destination and per-channel routing | `[p]log`, `[p]logchannel`, `/log event` | [Setup and commands](logplus/README.md) |
| OwoPlus | Webhook message transformations and automatic haiku formatting | `[p]owo`, `/owo preview` | [Setup and commands](owoplus/README.md) |

`[p]` means your bot's command prefix. For example, `[p]level show` becomes `!level show` when your prefix is `!`. Angle brackets mark required arguments; do not type the brackets.

## Discord presentation

All five cogs share an indigo theme, consistent headings and footers, readable settings, and matching success, warning, and error colors. Long results are paginated, and replies fall back to text when embeds are unavailable. See the [design and visual preview](docs/PRESENTATION.md).

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

The other cogs declare Red **3.5.0 or newer**. AudioPlus requires **Red 3.5.24 or newer**, native Discord voice, **yt-dlp**, **PyNaCl**, and **davey**. Downloader installs yt-dlp and its matching EJS package. Install **PyNaCl>=1.5.0,<1.6** and **davey>=0.1.6** once in Red's Python environment; working voice libraries previously installed by Downloader remain supported and are kept during cog updates. Install **FFmpeg**, **libopus**, and **Deno 2.3+ or Node.js 22+** inside the Red container. AudioPlus no longer needs Lavalink, Wavelink, or Java. Restart Red when changing voice libraries. Its guide includes voice-library and Deno installation commands and an optional persistent container image recipe. The other cogs have no additional required Python packages. OwoPlus can use optional syllable-counting packages, described in its guide.

These guides describe the current source. Compatibility metadata is not a record of live testing on every Red, Discord, or media-provider version.

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
| `[p]seen [@member]` | `/seen` | Last-seen information. |
| `[p]seendetail [@member]` | `/seendetail` | Detailed activity timestamps. |
| `[p]activity [@member]` | `/activity` | Activity counters and games. Text alias: `stats`. |
| `[p]seenlist [limit]` | `/seenlist` | Recently active members. |
| `[p]logstatus` | `/logstatus` | Logging settings. |
| `[p]logchannel [#channel]` | `/logchannel` | Show or set the log destination. |
| `[p]lograte [seconds]` | `/lograte` | Show or set duplicate suppression. |

Community and logging shortcuts retain administrator checks. Grouped text commands remain available under `community`, `level`, `log`, and `owo`. Slash groups use a `status` subcommand for their settings panel, such as `/community status`. Across these four cogs there are 124 slash actions for member reports, role/welcome settings, XP controls, log routing/switches, and transformation tools. Each guide lists the few deeper or ID-based paths that remain text-only.

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

## Data

Settings and persistent records use Red's Config system. CommunityPlus records member activity and sticky roles, LevelPlus retains XP and display names, and OwoPlus stores per-user probability overrides. AudioPlus preserves legacy Lavalink connection settings, including the old node password, for rollback. Its optional daily monitor stores the recipient ID, test server/channel/video, schedule, latest safe result, and pending alert. Native playback ignores legacy node settings; track metadata and queues are transient, and audio downloads and yt-dlp disk caching are disabled.

LogPlus does not persist message contents in its Config, but it can post edited or deleted message text to Discord log channels. OwoPlus reposts transformed messages through webhooks and attempts to delete the originals. Each cog's guide and `info.json` describe its stored data. AudioPlus, CommunityPlus, LevelPlus, and OwoPlus implement Red's user-data export/deletion hooks. Deletion removes their associated Config records; it does not delete messages already posted to Discord.

## Development and support

- [Contributing and local checks](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)
- [Report an issue](https://github.com/kevinwaynekelly/kevin-cogs/issues)
- [Red Downloader documentation](https://docs.discord.red/en/stable/cog_guides/downloader.html)

Each cog has a small `__init__.py` entry point, `cog.py` command/event implementation, `constants.py` defaults, metadata, and a README. Level calculations and haiku detection have separate modules. The regression suite checks real Red Config storage and command registration with mocked Discord calls, plus local yt-dlp extraction and FFmpeg decoding; CI covers Python 3.10/3.11 with Red 3.5.24 and native voice dependencies. See [CONTRIBUTING.md](CONTRIBUTING.md) for test and benchmark commands.
