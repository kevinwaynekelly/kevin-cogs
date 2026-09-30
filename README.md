# Kevin Cogs

Five custom cogs for [Red Discord Bot](https://docs.discord.red/en/stable/), maintained by [Kevin Kelly](https://github.com/kevinwaynekelly).

Music, community tools, leveling, event logging, and message transformations. Each cog can be installed separately.

## Cogs

| Cog | What it does | Command group | Guide |
| --- | --- | --- | --- |
| AudioPlus | Lavalink music playback, queues, and voice diagnostics | `[p]audio` | [Setup and commands](audioplus/README.md) |
| CommunityPlus | Autoroles, sticky roles, welcome/goodbye messages, activity tracking, and solo voice cleanup | `[p]com` | [Setup and commands](communityplus/README.md) |
| LevelPlus | Message, reaction, and voice XP with configurable level curves and import/export tools | `[p]level` | [Setup and commands](levelplus/README.md) |
| LogPlus | Server event logs with a default destination and per-channel routing | `[p]logplus` | [Setup and commands](logplus/README.md) |
| OwoPlus | Webhook message transformations and automatic haiku formatting | `[p]owoplus` | [Setup and commands](owoplus/README.md) |

`[p]` means your bot's command prefix. For example, `[p]level show` becomes `!level show` when your prefix is `!`. Angle brackets mark required arguments; do not type the brackets.

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

AudioPlus needs a reachable Lavalink node before it loads. Follow the [AudioPlus setup guide](audioplus/README.md), then install it separately:

```text
[p]cog install kevin-cogs audioplus
[p]load audioplus
```

Cog metadata declares Red **3.5.0 or newer**. AudioPlus requires **Lavalink v4**, **Wavelink >=3.4.1,<4.0.0**, and **aiohttp >=3.8**; Downloader installs the declared Python dependencies. The other cogs have no additional required Python packages. OwoPlus can use optional syllable-counting packages, described in its guide.

These guides describe the current source. Compatibility metadata is not a record of live testing on every Red, Discord, or Lavalink version.

## First setup

Read the cog's guide before loading it on an existing server. Some features start working immediately:

| Cog | Initial behavior |
| --- | --- |
| AudioPlus | Tries to connect to the configured Lavalink node during loading; uses the `audio` command name, also used by Red's bundled Audio cog. |
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

Server management commands generally require Red's admin access or the **Manage Server** permission. LevelPlus also exposes member commands. AudioPlus node configuration is owner-only; ordinary playback controls are server commands. Each guide lists the permissions required by its features.

## Updates

```text
[p]cog update communityplus levelplus logplus owoplus
[p]reload communityplus levelplus logplus owoplus
```

For AudioPlus, use `[p]cog update audioplus` followed by `[p]reload audioplus`.

`[p]repo update kevin-cogs` updates the downloaded repository; use `cog update` to update installed cogs. If you named the repository differently when adding it, use that name in repository and installation commands.

## Data

Settings and persistent records use Red's Config system. CommunityPlus records member activity and sticky roles, LevelPlus retains XP and display names, and OwoPlus stores per-user probability overrides. AudioPlus stores Lavalink connection settings, including the node password.

LogPlus does not persist message contents in its Config, but it can post edited or deleted message text to Discord log channels. OwoPlus reposts transformed messages through webhooks and attempts to delete the originals. Each cog's guide and `info.json` describe its stored data.

## Development and support

- [Contributing and local checks](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)
- [Report an issue](https://github.com/kevinwaynekelly/kevin-cogs/issues)
- [Red Downloader documentation](https://docs.discord.red/en/stable/cog_guides/downloader.html)

Each cog lives in its own folder with `__init__.py`, `info.json`, and a README.
