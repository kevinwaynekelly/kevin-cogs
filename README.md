# Kevin Cogs

Five custom cogs for [Red Discord Bot](https://docs.discord.red/en/stable/), maintained by [Kevin Kelly](https://github.com/kevinwaynekelly).

Music, community tools, leveling, event logging, and message transformations. Each cog can be installed separately.

## Cogs

| Cog | What it does | Command group | Guide |
| --- | --- | --- | --- |
| AudioPlus | Native Discord music playback, YouTube/SoundCloud search, queues, and voice diagnostics | `[p]audio` | [Setup and commands](audioplus/README.md) |
| CommunityPlus | Autoroles, sticky roles, welcome/goodbye messages, activity tracking, and solo voice cleanup | `[p]com` | [Setup and commands](communityplus/README.md) |
| LevelPlus | Message, reaction, and voice XP with configurable level curves and import/export tools | `[p]level` | [Setup and commands](levelplus/README.md) |
| LogPlus | Server event logs with a default destination and per-channel routing | `[p]logplus` | [Setup and commands](logplus/README.md) |
| OwoPlus | Webhook message transformations and automatic haiku formatting | `[p]owoplus` | [Setup and commands](owoplus/README.md) |

`[p]` means your bot's command prefix. For example, `[p]level show` becomes `!level show` when your prefix is `!`. Angle brackets mark required arguments; do not type the brackets.

## Discord presentation

All five cogs share an indigo theme, consistent headings and footers, readable settings, and matching success, warning, and error colors. Long results are paginated, and replies fall back to text when embeds are unavailable. See the [design and visual preview](docs/PRESENTATION.md).

Red's native `[p]help` lists descriptions for every cog command. Use `[p]help com`, `[p]help level`, or `[p]help logplus` to see their subcommands, and append a subcommand for its arguments and details. Cog names such as `[p]help CommunityPlus` also show a category overview.

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

The other cogs declare Red **3.5.0 or newer**. AudioPlus requires **Red 3.5.24 or newer**, native Discord voice, **yt-dlp**, **PyNaCl**, and **davey**; Downloader installs its declared Python dependencies. Install **FFmpeg**, **libopus**, and **Deno 2.3+ or Node.js 22+** inside the Red container. AudioPlus no longer needs Lavalink, Wavelink, or Java. Restart Red after installing voice dependencies. Its guide includes an optional persistent container image recipe. The other cogs have no additional required Python packages. OwoPlus can use optional syllable-counting packages, described in its guide.

These guides describe the current source. Compatibility metadata is not a record of live testing on every Red, Discord, or media-provider version.

## First setup

Read the cog's guide before loading it on an existing server. Some features start working immediately:

| Cog | Initial behavior |
| --- | --- |
| AudioPlus | Runs music search and playback locally when a member queues music; uses the `audio` command name, also used by Red's bundled Audio cog. Check dependencies with `[p]audio pingnode`. |
| CommunityPlus | Sticky roles, activity tracking, and solo voice cleanup are enabled. Solo voice cleanup defaults to 900 seconds. Autorole and welcome/goodbye targets need to be configured. |
| LevelPlus | Message, reaction, and voice XP are enabled, along with level-up announcements. |
| LogPlus | Needs a destination channel or route before it can post logs. |
| OwoPlus | Disabled until `[p]owoplus enable`. Haiku formatting is enabled within the cog's settings. |

Common starting points:

```text
[p]com help
[p]level help
[p]logplus help
[p]owoplus help
```

Server management commands generally require Red's admin access or the **Manage Server** permission. LevelPlus also exposes member commands. AudioPlus legacy node commands remain owner-only for compatibility; ordinary native playback controls are server commands. Each guide lists the permissions required by its features.

## Updates

```text
[p]cog update communityplus levelplus logplus owoplus
[p]reload communityplus levelplus logplus owoplus
```

For the AudioPlus native-backend upgrade, use `[p]cog update False audioplus`, install the local dependencies, and restart Red. Later source updates can use `[p]cog update True audioplus`. See its [upgrade instructions](audioplus/README.md#upgrading-from-the-lavalink-backend).

`[p]repo update kevin-cogs` updates the downloaded repository; use `cog update` to update installed cogs. If you named the repository differently when adding it, use that name in repository and installation commands.

## Data

Settings and persistent records use Red's Config system. CommunityPlus records member activity and sticky roles, LevelPlus retains XP and display names, and OwoPlus stores per-user probability overrides. AudioPlus preserves legacy Lavalink connection settings, including the old node password, for rollback. Native playback ignores them; track metadata and queues are transient, and audio downloads and yt-dlp disk caching are disabled.

LogPlus does not persist message contents in its Config, but it can post edited or deleted message text to Discord log channels. OwoPlus reposts transformed messages through webhooks and attempts to delete the originals. Each cog's guide and `info.json` describe its stored data. CommunityPlus, LevelPlus, and OwoPlus implement Red's user-data export/deletion hooks. Deletion removes their Config records; it does not delete messages already posted to Discord.

## Development and support

- [Contributing and local checks](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)
- [Report an issue](https://github.com/kevinwaynekelly/kevin-cogs/issues)
- [Red Downloader documentation](https://docs.discord.red/en/stable/cog_guides/downloader.html)

Each cog has a small `__init__.py` entry point, `cog.py` command/event implementation, `constants.py` defaults, metadata, and a README. Level calculations and haiku detection have separate modules. The regression suite checks real Red Config storage and command registration with mocked Discord calls, plus local yt-dlp extraction and FFmpeg decoding; CI covers Python 3.10/3.11 with Red 3.5.24 and native voice dependencies. See [CONTRIBUTING.md](CONTRIBUTING.md) for test and benchmark commands.
